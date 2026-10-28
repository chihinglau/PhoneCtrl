// PhoneCtrl — phone-side automation operations.
//
// Wraps Adb primitives with retry (RetryPolicy) + stillness waiting, and
// provides high-level flows (openApp / swipe loop / safe back) used by the
// scheduler. Every action is idempotent so retries are safe.

import 'dart:async';
import 'dart:io';

import 'link_base.dart';
import 'models.dart';
import 'screen.dart';

class Automation {
  Automation(this.link, this.screen);
  final Link link;
  final ScreenManager screen;

  final Map<String, (int, int)> _resCache = {};

  Future<(int, int)> _res(String serial) async {
    _resCache[serial] ??= await link.resolution(serial);
    return _resCache[serial]!;
  }

  /// Run [action] with exponential backoff; returns the last OpResult.
  Future<OpResult> _withRetry(
    String serial,
    Future<OpResult> Function() action,
    RetryPolicy rp, {
    String what = 'op',
  }) async {
    OpResult last = OpResult(ok: false, attempts: 0, costMs: 0);
    for (int i = 1; i <= rp.maxAttempts; i++) {
      last = await action();
      last = OpResult(ok: last.ok, attempts: i, costMs: last.costMs, detail: last.detail);
      if (last.ok) return last;
      stderr.writeln('[auto] $what failed (try $i/${rp.maxAttempts}): ${last.detail}');
      if (i < rp.maxAttempts) await Future.delayed(rp.backoff(i));
    }
    return last;
  }

  Future<OpResult> openApp(String serial, String pkg, [String? activity,
      RetryPolicy rp = const RetryPolicy()]) {
    return _withRetry(serial, () => link.openApp(serial, pkg, activity), rp, what: 'openApp($pkg)');
  }

  Future<OpResult> tap(String serial, RPoint p, [RetryPolicy rp = const RetryPolicy()]) async {
    final (w, h) = await _res(serial);
    return _withRetry(serial, () => link.tap(serial, p, w, h), rp, what: 'tap($p)');
  }

  Future<OpResult> swipe(String serial, RPoint a, RPoint b, int ms,
      [RetryPolicy rp = const RetryPolicy()]) async {
    final (w, h) = await _res(serial);
    return _withRetry(serial, () => link.swipe(serial, a, b, w, h, ms), rp, what: 'swipe');
  }

  Future<OpResult> keyEvent(String serial, String code,
      [RetryPolicy rp = const RetryPolicy()]) {
    return _withRetry(serial, () => link.keyEvent(serial, code), rp, what: 'key($code)');
  }

  /// Perform a burst of swipes (e.g. scroll a feed). Returns how many succeeded.
  Future<int> swipeBurst(String serial, RPoint from, RPoint to, int ms, int times) async {
    int ok = 0;
    for (int i = 0; i < times; i++) {
      final r = await swipe(serial, from, to, ms);
      if (r.ok) ok++;
      await Future.delayed(const Duration(milliseconds: 250));
    }
    return ok;
  }

  /// Wait until two consecutive frames are visually stable.
  Future<bool> waitStable(String serial,
      {Duration timeout = const Duration(seconds: 8), int stableFrames = 2}) async {
    List<int>? prev;
    int stable = 0;
    final end = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(end)) {
      final f = await screen.capture(serial, persist: false);
      if (f == null) {
        await Future.delayed(const Duration(milliseconds: 300));
        continue;
      }
      if (prev != null && ScreenManager.isStable(prev, f)) {
        stable++;
        if (stable >= stableFrames) return true;
      } else {
        stable = 0;
      }
      prev = f;
      await Future.delayed(const Duration(milliseconds: 300));
    }
    return false;
  }

  /// Logic-anomaly recovery: press BACK once (or go HOME+reopen) to escape an
  /// unexpected page (e.g. mis-tapped a payment/store dialog).
  Future<void> recover(String serial, String pkg) async {
    stderr.writeln('[auto] recover: press BACK to escape unexpected page');
    await link.keyEvent(serial, '4'); // KEYCODE_BACK
    await Future.delayed(const Duration(milliseconds: 800));
    final act = await link.currentActivity(serial);
    if (!act.contains(pkg)) {
      stderr.writeln('[auto] recover: re-open app $pkg');
      await link.openApp(serial, pkg);
      await waitStable(serial);
    }
  }
}
