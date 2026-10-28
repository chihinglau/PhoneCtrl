// PhoneCtrl — task scheduler + per-device automation pipeline.
//
// Runs a TaskScript as a state machine (TaskPhase) per device, with:
//  - composite ad detectors (region / ocr / template)
//  - three-tier reward verification
//  - per-step RetryPolicy + mis-tap recovery
//  - breakpoint resume via Monitor.persist()

import 'dart:async';
import 'dart:typed_data';

import 'automation.dart';
import 'config.dart';
import 'detector.dart';
import 'link_base.dart';
import 'models.dart';
import 'monitor.dart';
import 'png.dart';
import 'screen.dart';
import 'swipe.dart';

const _rp = RetryPolicy(maxAttempts: 3);

/// Anything the Scheduler can run per device and cancel on demand.
abstract class Cancelable {
  void cancel();
  String get kind;
}

class Scheduler {
  Scheduler(this.link, this.screen, this.monitor, this.ocr);
  final Link link;
  final ScreenManager screen;
  final Monitor monitor;
  final OcrProvider ocr;

  final Map<String, Cancelable> _tasks = {};

  /// Start (or restart) a full app-automation task for a device.
  void submit(String serial, String model, TaskScript script) {
    stop(serial);
    // a freshly submitted task starts from round 0; clear any stale progress
    // carried over from a previous (different) task for this serial.
    monitor.progress[serial] = 0;
    monitor.persist();
    final t = DeviceTask(serial, model, script, this);
    _tasks[serial] = t;
    t.run();
  }

  /// Start (or restart) a lightweight directional swipe job for a device.
  /// Shares the per-serial slot with [submit], so a device runs at most one
  /// job (script OR swipe) at a time — other devices are fully independent.
  void submitSwipe(String serial, String model, SwipeJob cfg) {
    stop(serial);
    final t = DeviceSwipeTask(serial, model, cfg, this);
    _tasks[serial] = t;
    t.run();
  }

  void stop(String serial) {
    _tasks.remove(serial)?.cancel();
  }

  bool get isRunning => _tasks.isNotEmpty;
}

class DeviceTask implements Cancelable {
  DeviceTask(this.serial, this.model, this.script, this.sched);
  final String serial;
  final String model;
  final TaskScript script;
  final Scheduler sched;

  late final Automation _auto = Automation(sched.link, sched.screen);
  late final RewardVerifier _verifier = RewardVerifier(sched.ocr);

  bool _cancel = false;
  Timer? _timer;

  void cancel() {
    _cancel = true;
    _timer?.cancel();
  }

  @override
  String get kind => 'script';

  Future<void> run() async {
    final st = sched.monitor.status(serial, model);
    st.taskKind = 'script';
    final start = sched.monitor.progress[serial] ?? 0;
    st.totalRounds = script.loop.rounds;
    for (int round = start; round < script.loop.rounds; round++) {
      if (_cancel) break;
      st.round = round + 1;
      st.phase = TaskPhase.opening;
      sched.monitor.log(serial, 'round ${st.round}/${st.totalRounds} start');
      try {
        await _oneRound();
        st.success++;
        sched.monitor.progress[serial] = round + 1;
        sched.monitor.persist();
      } catch (e) {
        st.failed++;
        st.lastError = e.toString();
        sched.monitor.log(serial, 'round failed: $e', error: true);
        await _auto.recover(serial, script.package); // logic-anomaly recovery
      }
      await Future.delayed(Duration(milliseconds: script.loop.intervalMs));
    }
    st.taskKind = '';
    st.phase = _cancel ? TaskPhase.idle : TaskPhase.done;
    sched.monitor.log(serial, _cancel ? 'task cancelled' : 'task done');
  }

  Future<void> _oneRound() async {
    final st = sched.monitor.status(serial, model);

    // 1) open app
    if (script.loop.openApp) {
      await _auto.openApp(serial, script.package, script.activity, _rp);
      final ok = await sched.link.waitApp(serial, script.package);
      if (!ok) throw Exception('app not in foreground: ${script.package}');
      await _auto.waitStable(serial);
    }

    // refresh the live frame shown on the web console (binary-safe persist)
    await sched.screen.capture(serial);

    // 2) swipe feed
    if (script.loop.swipe != null) {
      st.phase = TaskPhase.swiping;
      final s = script.loop.swipe!;
      await _auto.swipeBurst(serial, s.from, s.to, s.ms, s.times);
      await _auto.waitStable(serial);
    }

    // 2.5) navigate to the task/entry page (app-specific taps)
    if (script.loop.nav != null) {
      st.phase = TaskPhase.swiping;
      for (final step in script.loop.nav!) {
        sched.monitor.log(serial, 'nav tap ${step.tap}');
        await _auto.tap(serial, step.tap, _rp);
        await Future.delayed(Duration(milliseconds: step.delayMs));
        await _auto.waitStable(serial);
      }
    }

    // 3) click ad (composite detectors, guarded against mis-tap)
    if (script.loop.ad != null) {
      st.phase = TaskPhase.adClicking;
      final ad = script.loop.ad!;
      int clicks = 0;
      while (clicks < ad.maxClicks && !_cancel) {
        final det = await _detectAd(ad);
        if (det == null) {
          sched.monitor.log(serial, 'no ad found this scan');
          break;
        }
        await _auto.tap(serial, det.click, _rp);
        clicks++;
        await Future.delayed(const Duration(milliseconds: 600));
        // mis-tap guard: unexpected jump to browser/store
        final act = await sched.link.currentActivity(serial);
        if (!act.contains(script.package) && !act.contains('com.android')) {
          sched.monitor.log(serial, 'mis-tap escaped app ($act); recovering');
          await _auto.keyEvent(serial, '4');
          await Future.delayed(const Duration(milliseconds: 500));
        }
        if (clicks < ad.maxClicks) await Future.delayed(const Duration(seconds: 1));
      }
    }

    // 4) claim reward (before/after verify)
    if (script.loop.reward != null) {
      st.phase = TaskPhase.rewarding;
      final rw = script.loop.reward!;
      final before = await _frame();
      await _auto.tap(serial, rw.button, _rp);
      final claimed = await sched.link.waitFor(serial, () async {
        final after = await _frame();
        if (after == null) return false;
        return await _verifier.verify(before!, after, rw.verifyRegion,
            successKeywords: rw.successKeywords);
      }, timeout: Duration(milliseconds: rw.timeoutMs));
      st.phase = TaskPhase.verifying;
      if (!claimed) {
        sched.monitor.log(serial, 'reward not verified (tier1/2/3 all miss)');
      } else {
        sched.monitor.log(serial, 'reward claimed & verified');
      }
    }

    // 4.5) watch video ad -> wait -> claim -> verify (for 番茄免费小说 等激励视频)
    if (script.loop.videoAd != null) {
      st.phase = TaskPhase.videoAd;
      await _watchVideoAd(script.loop.videoAd!);
      st.phase = TaskPhase.verifying;
    }

    // 5) back to home anchor
    if (script.loop.backHome) {
      if (script.home != null) {
        await _auto.tap(serial, script.home!, _rp);
      } else {
        await _auto.keyEvent(serial, '3'); // HOME
      }
      await Future.delayed(const Duration(milliseconds: 400));
    }
  }

  /// Composite ad detection: returns the first hit (region -> ocr).
  Future<Detection?> _detectAd(AdConfig ad) async {
    final frame = await _frame();
    if (frame == null) return null;
    if (ad.region != null) {
      final d = await RegionAdDetector(ad.region!, mode: ad.mode).detect(frame);
      if (d != null) return d;
    }
    if (ad.keywords.isNotEmpty) {
      final r = ad.search ?? const Rect(0.0, 0.0, 1.0, 1.0);
      final d = await OcrAdDetector(sched.ocr, ad.keywords, r).detect(frame);
      if (d != null) return d;
    }
    return null;
  }

  /// 看视频广告 -> 等待视频播完 -> 点击领取 -> 校验。
  Future<void> _watchVideoAd(VideoAdConfig v) async {
    // 1) 点击「看视频得金币」入口
    final entered = await _findOrTap(v.entryButton, v.entryRegion, v.entryKeywords, 'entry');
    if (!entered) {
      sched.monitor.log(serial,
          'videoAd: 入口未命中（请校准 entryButton / entryRegion / entryKeywords）');
      return;
    }
    await Future.delayed(const Duration(milliseconds: 800));

    // 2) 视频播放期间轮询「领取」是否出现（命中 claimKeywords 即认为可领）
    final appeared = await sched.link.waitFor(serial, () async {
      if (v.claimKeywords.isNotEmpty) {
        if (await _regionHasKeywords(v.claimRegion, v.claimKeywords)) return true;
        if (await _regionHasKeywords(const Rect(0.0, 0.0, 1.0, 1.0), v.claimKeywords)) {
          return true;
        }
      }
      return false;
    }, timeout: Duration(milliseconds: v.waitClaimMs));
    sched.monitor.log(serial,
        appeared ? 'videoAd: 检测到领取按钮' : 'videoAd: 未检测到领取，按坐标兜底点击');

    // 3) 点击领取（领取前抓帧用于校验）
    final before = await _frame();
    final claimed = await _findOrTap(v.claimButton, v.claimRegion, v.claimKeywords, 'claim');
    if (!claimed) {
      sched.monitor.log(serial, 'videoAd: 领取点击失败（请校准 claimButton / claimRegion）');
      return;
    }
    await Future.delayed(const Duration(milliseconds: 500));

    // 4) 领取前/后校验
    final ok = await sched.link.waitFor(serial, () async {
      if (before == null) return false;
      final after = await _frame();
      if (after == null) return false;
      return await _verifier.verify(before, after,
          v.verifyRegion ?? const Rect(0.0, 0.0, 1.0, 1.0),
          successKeywords: v.successKeywords);
    }, timeout: Duration(milliseconds: v.timeoutMs));
    if (ok) {
      sched.monitor.log(serial, 'videoAd: 奖励已领取并校验通过');
    } else {
      sched.monitor.log(serial, 'videoAd: 已点击领取但未校验成功（tier1/2/3 均未命中）');
    }
  }

  /// 命中并点击：优先固定坐标，其次「区域中心」（可先经 OCR 关键词门控）。
  Future<bool> _findOrTap(
      RPoint? button, Rect? region, List<String> keywords, String what) async {
    if (button != null) {
      await _auto.tap(serial, button, _rp);
      return true;
    }
    if (region != null) {
      if (keywords.isNotEmpty && !await _regionHasKeywords(region, keywords)) {
        return false;
      }
      await _auto.tap(serial, RPoint(region.center.$1, region.center.$2), _rp);
      return true;
    }
    return false;
  }

  /// 区域内是否含任一关键词（OCR；无 tesseract 时返回 false）。
  Future<bool> _regionHasKeywords(Rect? region, List<String> keywords) async {
    if (region == null || keywords.isEmpty) return false;
    final frame = await _frame();
    if (frame == null) return false;
    final text = await sched.ocr.text(frame.crop(region));
    return keywords.any((k) => text.contains(k));
  }

  Future<PngImage?> _frame() async {
    final bytes = await sched.screen.capture(serial);
    if (bytes == null) return null;
    try {
      return decodePng(Uint8List.fromList(bytes));
    } catch (_) {
      return null;
    }
  }
}
