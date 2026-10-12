// PhoneCtrl — ADB command wrapper.
//
// Inherits the reference project's `Adb` class paradigm
// (MyGameSim/tools/library/server.dart): exe resolution via ANDROID_HOME,
// `run`/`shell` primitives, device enumeration, plus automation semantics
// (tap / swipe / startActivity / screenshot / currentApp / waitFor).

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'device.dart';
import 'models.dart';

class Adb {
  /// exe resolution identical to GamePouch's Adb.exe.
  String get exe {
    final home = Platform.environment['ANDROID_HOME'];
    if (home != null) {
      final p = File('$home/platform-tools/adb.exe');
      if (p.existsSync()) return p.path;
    }
    return 'adb';
  }

  Future<ProcessResult> run(List<String> args) =>
      Process.run(exe, args, stdoutEncoding: systemEncoding);

  /// `adb -s <serial> shell <cmd...>`; non-zero exit throws.
  Future<String> shell(String serial, List<String> cmd) async {
    final args = ['-s', serial, 'shell', ...cmd];
    final r = await Process.run(exe, args, stdoutEncoding: systemEncoding);
    if (r.exitCode != 0) {
      throw Exception('adb ${args.join(' ')} -> ${r.stderr}');
    }
    return (r.stdout as String).trim();
  }

  /// Run a shell command but never throw (returns stdout, or '' on failure).
  Future<String> shellOr(String serial, List<String> cmd, [String fallback = '']) async {
    try {
      return await shell(serial, cmd);
    } catch (_) {
      return fallback;
    }
  }

  /// Enumerate connected devices: `adb devices -l`.
  Future<List<Device>> devices() async {
    final r = await run(['devices', '-l']);
    if (r.exitCode != 0) throw Exception('adb devices failed: ${r.stderr}');
    final out = (r.stdout as String);
    final lines = out.split('\n').skip(1); // drop "List of devices attached"
    final devs = <Device>[];
    for (final line in lines) {
      final t = line.trim();
      if (t.isEmpty) continue;
      final parts = t.split(RegExp(r'\s+'));
      final serial = parts[0];
      final state = parts.length > 1 ? parts[1] : 'unknown';
      String model = '';
      final m = RegExp(r'model:(\S+)').firstMatch(t);
      if (m != null) model = m.group(1)!;
      devs.add(Device(serial, state, model));
    }
    return devs;
  }

  // ---------------------------------------------------------------- semantics

  /// Physical screen resolution via `wm size` -> [w, h].
  Future<(int, int)> resolution(String serial) async {
    final s = await shellOr(serial, ['wm', 'size']);
    final m = RegExp(r'(\d+)x(\d+)').firstMatch(s);
    if (m != null) {
      return (int.parse(m.group(1)!), int.parse(m.group(2)!));
    }
    return (1080, 2400); // safe default
  }

  /// Battery percentage via `dumpsys battery`.
  Future<int?> battery(String serial) async {
    final s = await shellOr(serial, ['dumpsys', 'battery']);
    final m = RegExp(r'level:\s*(\d+)').firstMatch(s);
    return m != null ? int.parse(m.group(1)!) : null;
  }

  /// Currently focused activity via `dumpsys window`.
  Future<String> currentActivity(String serial) async {
    final s = await shellOr(serial, ['dumpsys', 'window', '|', 'grep', 'mCurrentFocus']);
    final m = RegExp(r'mCurrentFocus=.*?(\S+/\S+)').firstMatch(s);
    return m?.group(1) ?? '';
  }

  /// Launch an app. Prefer explicit activity; fall back to monkey launcher.
  Future<OpResult> openApp(String serial, String pkg, [String? activity]) async {
    final sw = Stopwatch()..start();
    try {
      if (activity != null && activity.isNotEmpty) {
        await shell(serial, ['am', 'start', '-n', '$pkg/$activity']);
      } else {
        await shell(serial, ['monkey', '-p', pkg, '-c', 'android.intent.category.LAUNCHER', '1']);
      }
      return OpResult(ok: true, attempts: 1, costMs: sw.elapsedMilliseconds, detail: pkg);
    } catch (e) {
      return OpResult(ok: false, attempts: 1, costMs: sw.elapsedMilliseconds, detail: e.toString());
    }
  }

  /// Tap at relative coordinates.
  Future<OpResult> tap(String serial, RPoint p, int w, int h) async {
    final sw = Stopwatch()..start();
    final (px, py) = p.toPixels(w, h);
    try {
      await shell(serial, ['input', 'tap', '$px', '$py']);
      return OpResult(ok: true, attempts: 1, costMs: sw.elapsedMilliseconds);
    } catch (e) {
      return OpResult(ok: false, attempts: 1, costMs: sw.elapsedMilliseconds, detail: e.toString());
    }
  }

  /// Swipe from relative a -> b over [ms] milliseconds.
  Future<OpResult> swipe(String serial, RPoint a, RPoint b, int w, int h, int ms) async {
    final sw = Stopwatch()..start();
    final (x1, y1) = a.toPixels(w, h);
    final (x2, y2) = b.toPixels(w, h);
    try {
      await shell(serial, ['input', 'swipe', '$x1', '$y1', '$x2', '$y2', '$ms']);
      return OpResult(ok: true, attempts: 1, costMs: sw.elapsedMilliseconds);
    } catch (e) {
      return OpResult(ok: false, attempts: 1, costMs: sw.elapsedMilliseconds, detail: e.toString());
    }
  }

  /// Inject a key event (e.g. KEYCODE_BACK=4, KEYCODE_HOME=3).
  Future<OpResult> keyEvent(String serial, String code) async {
    final sw = Stopwatch()..start();
    try {
      await shell(serial, ['input', 'keyevent', code]);
      return OpResult(ok: true, attempts: 1, costMs: sw.elapsedMilliseconds);
    } catch (e) {
      return OpResult(ok: false, attempts: 1, costMs: sw.elapsedMilliseconds, detail: e.toString());
    }
  }

  /// Capture a screenshot to PNG bytes (binary-safe exec-out).
  Future<Uint8List?> screenshot(String serial) async {
    final r = await Process.run(exe, ['-s', serial, 'exec-out', 'screencap', '-p'],
        stdoutEncoding: null);
    if (r.exitCode != 0 || (r.stdout as Uint8List).isEmpty) return null;
    return r.stdout as Uint8List;
  }

  /// Poll until [predicate] returns true or [timeout] elapses.
  Future<bool> waitFor(
    String serial,
    Future<bool> Function() predicate, {
    Duration timeout = const Duration(seconds: 10),
    Duration interval = const Duration(milliseconds: 300),
  }) async {
    final end = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(end)) {
      if (await predicate()) return true;
      await Future.delayed(interval);
    }
    return false;
  }

  /// Wait until [pkg] is the focused app.
  Future<bool> waitApp(String serial, String pkg,
      {Duration timeout = const Duration(seconds: 10)}) {
    return waitFor(serial, () async {
      final a = await currentActivity(serial);
      return a.contains(pkg);
    }, timeout: timeout);
  }
}
