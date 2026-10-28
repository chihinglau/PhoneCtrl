// PhoneCtrl — automatic screen-swipe task (lightweight, app-agnostic).
//
// Unlike TaskScript (which drives a full app-specific automation round), a
// "swipe job" simply repeats a directional swipe at a *random* interval inside
// a user-selected range. It runs per device (keyed by serial) inside the
// Scheduler, so multiple phones swipe concurrently without interfering.

import 'dart:async';
import 'dart:math';

import 'automation.dart';
import 'models.dart';
import 'scheduler.dart';

/// Direction of a single swipe.
const _dirLabels = {'up': '上', 'down': '下', 'left': '左', 'right': '右'};
const _validDirs = {'up', 'down', 'left', 'right'};

/// Configuration of one swipe job.
class SwipeJob {
  SwipeJob(this.direction, this.minMs, this.maxMs);

  final String direction; // 'up' | 'down' | 'left' | 'right'
  final int minMs;
  final int maxMs;

  bool get isValidDirection => _validDirs.contains(direction);
  String get dirLabel => _dirLabels[direction] ?? direction;

  /// A random delay (ms) uniformly sampled inside [minMs, maxMs].
  int get randomDelayMs {
    if (maxMs <= minMs) return minMs;
    return minMs + (Random().nextDouble() * (maxMs - minMs)).round();
  }

  String get rangeLabel =>
      '${(minMs / 1000).toStringAsFixed(0)}-${(maxMs / 1000).toStringAsFixed(0)} 秒';

  /// Relative from/to points + duration (ms) for the chosen direction.
  /// Margins (0.16 / 0.82) keep the gesture off the status bar / nav bar.
  (RPoint, RPoint, int) get geometry {
    const ms = 420;
    switch (direction) {
      case 'down':
        return (RPoint(0.5, 0.16), RPoint(0.5, 0.82), ms); // 向下滑（内容往下）
      case 'left':
        return (RPoint(0.82, 0.5), RPoint(0.16, 0.5), ms); // 向左滑
      case 'right':
        return (RPoint(0.16, 0.5), RPoint(0.82, 0.5), ms); // 向右滑
      case 'up':
      default:
        return (RPoint(0.5, 0.82), RPoint(0.5, 0.16), ms); // 向上滑（默认）
    }
  }
}

/// Per-device swipe job. Runs a self-rescheduling loop:
///   sleep(randomDelay) -> one swipe -> repeat until cancelled.
class DeviceSwipeTask implements Cancelable {
  DeviceSwipeTask(this.serial, this.model, this.config, this.sched);
  final String serial;
  final String model;
  final SwipeJob config;
  final Scheduler sched;

  bool _cancel = false;
  Timer? _timer;
  late final Automation _auto = Automation(sched.link, sched.screen);

  @override
  String get kind => 'swipe';

  @override
  void cancel() {
    _cancel = true;
    _timer?.cancel();
    // The wait timer may be cancelled before it fires, so its callback (which
    // would normally call _finish) never runs. Clean up the live status here so
    // the device doesn't keep showing a stale "swipe" task after stopping.
    _finish();
  }

  void run() {
    final st = sched.monitor.status(serial, model);
    st.taskKind = 'swipe';
    st.swipeDir = config.direction;
    st.swipeCount = 0;
    st.nextSwipeAt = null;
    sched.monitor.log(serial,
        '滑屏任务启动 · 方向=${config.dirLabel} · 间隔=${config.rangeLabel}');
    _scheduleNext();
  }

  void _scheduleNext() {
    if (_cancel) return _finish();
    final st = sched.monitor.status(serial, model);
    final wait = config.randomDelayMs;
    st.nextSwipeMs = wait;
    st.nextSwipeAt = DateTime.now().add(Duration(milliseconds: wait)).millisecondsSinceEpoch;
    _timer = Timer(Duration(milliseconds: wait), () async {
      if (_cancel) return _finish();
      await _doSwipe();
      if (_cancel) return _finish();
      _scheduleNext();
    });
  }

  Future<void> _doSwipe() async {
    final st = sched.monitor.status(serial, model);
    st.phase = TaskPhase.swiping;
    st.nextSwipeMs = null;
    st.nextSwipeAt = null;
    final (a, b, ms) = config.geometry;
    final r = await _auto.swipe(serial, a, b, ms);
    if (r.ok) {
      st.swipeCount++;
    } else {
      sched.monitor.log(serial, '滑屏失败: ${r.detail}', error: true);
    }
  }

  void _finish() {
    final st = sched.monitor.status(serial, model);
    st.taskKind = '';
    st.swipeDir = '';
    st.swipeCount = 0;
    st.nextSwipeMs = null;
    st.nextSwipeAt = null;
    st.swipeAuto = false;
    if (st.phase == TaskPhase.swiping) st.phase = TaskPhase.idle;
    sched.monitor.log(serial, '滑屏任务已停止');
  }
}

/// A user-configurable interval preset (a named [minMs, maxMs] range).
class SwipePreset {
  SwipePreset(this.key, this.label, this.minMs, this.maxMs);
  final String key;
  final String label;
  final int minMs;
  final int maxMs;

  Map<String, dynamic> toJson() =>
      {'key': key, 'label': label, 'minMs': minMs, 'maxMs': maxMs};

  factory SwipePreset.fromJson(Map<String, dynamic> j) => SwipePreset(
        j['key'] ?? '',
        j['label'] ?? '',
        (j['minMs'] as num?)?.toInt() ?? 1000,
        (j['maxMs'] as num?)?.toInt() ?? 3000,
      );
}

/// Factory for the built-in presets the user asked for.
List<SwipePreset> defaultSwipePresets() => [
      SwipePreset('fast', '快 · 1-3 秒', 1000, 3000),
      SwipePreset('mid', '中 · 5-10 秒', 5000, 10000),
      SwipePreset('slow', '慢 · 15-30 秒', 15000, 30000),
      SwipePreset('xslow', '极慢 · 40-60 秒', 40000, 60000),
    ];

/// Global "auto-swipe on connect" configuration (persisted to disk).
class AutoSwipeConfig {
  AutoSwipeConfig({
    this.enabled = false,
    this.direction = 'up',
    this.rangeKey = 'fast',
    this.custom = false,
    this.minMs = 1000,
    this.maxMs = 3000,
  });

  final bool enabled;
  final String direction; // 'up' | 'down' | 'left' | 'right'
  final String rangeKey; // 引用一个 SwipePreset.key，custom 时为占位
  final bool custom; // true 时使用 minMs/maxMs 自定义区间
  final int minMs;
  final int maxMs;

  Map<String, dynamic> toJson() => {
        'enabled': enabled,
        'direction': direction,
        'rangeKey': rangeKey,
        'custom': custom,
        'minMs': minMs,
        'maxMs': maxMs,
      };

  factory AutoSwipeConfig.fromJson(Map<String, dynamic> j) => AutoSwipeConfig(
        enabled: j['enabled'] == true,
        direction: (j['direction'] ?? 'up').toString(),
        rangeKey: (j['rangeKey'] ?? 'fast').toString(),
        custom: j['custom'] == true,
        minMs: (j['minMs'] as num?)?.toInt() ?? 1000,
        maxMs: (j['maxMs'] as num?)?.toInt() ?? 3000,
      );

  /// Resolve to a concrete [SwipeJob] using the given presets.
  SwipeJob toJob(List<SwipePreset> presets) {
    if (custom) return SwipeJob(direction, minMs, maxMs);
    final p = presets.firstWhere((x) => x.key == rangeKey,
        orElse: () => defaultSwipePresets().first);
    return SwipeJob(direction, p.minMs, p.maxMs);
  }
}
