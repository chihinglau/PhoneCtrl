// PhoneCtrl — HarmonyOS / OpenHarmony device link via `hdc`.
//
// Mirrors the reference project GamePouch's pura70 workflow:
//   hdc list targets            -> device discovery
//   hdc -t <sn> shell <cmd>     -> shell
//   hdc -t <sn> shell "uitest uiInput click x y"     -> tap
//   hdc -t <sn> shell "uitest uiInput swipe x1 y1 x2 y2 ms" -> swipe
//   hdc -t <sn> shell "uitest uiInput keyEvent <code>"      -> key
//   hdc -t <sn> shell "aa start -a <ability> -b <bundle>"   -> launch app
//   hdc -t <sn> shell snapshot_display -f <dev> + hdc file recv -> screenshot
//   hdc -t <sn> shell "hidumper -s WindowManager -a display" -> resolution (best-effort)
//
// HarmonyOS has NO `input` command (unlike Android), so UI input MUST go through
// `uitest`. The `hdc` binary ships with the OHOS SDK (toolchains/hdc.exe).

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'device.dart';
import 'link_base.dart';
import 'models.dart';

class Hdc extends Link {
  @override
  String get transport => 'hdc';

  /// Resolve the hdc executable: OHOS SDK toolchains first, then PATH.
  String get exe {
    final sdk = Platform.environment['OHOS_SDK_HOME'] ??
        Platform.environment['DEVECO_SDK_HOME'];
    final roots = <String>[
      if (sdk != null) sdk,
      r'D:\HuaWei\Sdk',
      r'C:\Program Files\Huawei\Sdk',
      if (Platform.environment['LOCALAPPDATA'] != null)
        '${Platform.environment['LOCALAPPDATA']}\Huawei\Sdk',
    ];
    for (final root in roots) {
      for (final tail in const [
        'toolchains/hdc.exe',
        '26.0.0/toolchains/hdc.exe',
        '12/toolchains/hdc.exe',
        'OpenHarmony/toolchains/hdc.exe',
      ]) {
        final p = File('$root/$tail');
        if (p.existsSync()) return p.path;
      }
    }
    return 'hdc'; // fall back to PATH
  }

  Future<ProcessResult> run(List<String> args) =>
      Process.run(exe, args, stdoutEncoding: systemEncoding);

  @override
  Future<String> shell(String serial, List<String> cmd) async {
    final args = ['-t', serial, 'shell', ...cmd];
    final r = await Process.run(exe, args, stdoutEncoding: systemEncoding);
    if (r.exitCode != 0) {
      throw Exception('hdc ${args.join(' ')} -> ${r.stderr}');
    }
    return (r.stdout as String).trim();
  }

  @override
  Future<String> shellOr(String serial, List<String> cmd, [String fallback = '']) async {
    try {
      return await shell(serial, cmd);
    } catch (_) {
      return fallback;
    }
  }

  @override
  Future<List<Device>> devices() async {
    final r = await run(['list', 'targets']);
    if (r.exitCode != 0) return [];
    final out = (r.stdout as String);
    final devs = <Device>[];
    for (final raw in out.split('\n')) {
      final t = raw.trim();
      if (t.isEmpty) continue;
      // hdc list targets lines look like:
      //   2MN0224730027764        device
      //   192.168.1.20:5555       connect
      final parts = t.split(RegExp(r'\s+'));
      final serial = parts[0];
      final state = parts.length > 1 ? parts[1] : 'device';
      // best-effort model name
      String model = 'HarmonyOS';
      try {
        final m = await shellOr(serial, ['param', 'get', 'const.product.model']);
        if (m.isNotEmpty) model = m;
      } catch (_) {}
      devs.add(Device(serial, state, model, 'hdc'));
    }
    return devs;
  }

  @override
  Future<(int, int)> resolution(String serial) async {
    // Try `wm size` first (OHOS exposes it on most builds) ...
    String s = await shellOr(serial, ['wm', 'size']);
    var m = RegExp(r'(\d+)x(\d+)').firstMatch(s);
    if (m != null) {
      return (int.parse(m.group(1)!), int.parse(m.group(2)!));
    }
    // ... else ask the window manager via hidumper.
    s = await shellOr(serial,
        ['hidumper', '-s', 'WindowManager', '-a', 'display']);
    m = RegExp(r'(\d+)\s*[x×]\s*(\d+)').firstMatch(s);
    if (m != null) {
      return (int.parse(m.group(1)!), int.parse(m.group(2)!));
    }
    return (1080, 2400); // safe default
  }

  @override
  Future<int?> battery(String serial) async {
    final s = await shellOr(serial,
        ['hidumper', '-s', 'Battery', '-a', 'info']);
    final m = RegExp(r'level[: ]+(\d+)').firstMatch(s) ??
        RegExp(r'(\d+)%').firstMatch(s);
    return m != null ? int.parse(m.group(1)!) : null;
  }

  @override
  Future<String> currentActivity(String serial) async {
    // Best-effort: top ability reported by the ability manager.
    final s = await shellOr(serial,
        ['hidumper', '-s', 'AbilityManager', '-a', 'top']);
    final m = RegExp(r'(com\.[\w.]+)').firstMatch(s);
    return m?.group(1) ?? '';
  }

  @override
  Future<OpResult> openApp(String serial, String pkg,
      [String? activity]) async {
    final sw = Stopwatch()..start();
    try {
      // HarmonyOS launches by bundle + ability (default EntryAbility).
      final ability = (activity != null && activity.contains('/'))
          ? activity.split('/').last
          : (activity?.isNotEmpty == true ? activity! : 'EntryAbility');
      await shell(serial, ['aa', 'start', '-a', ability, '-b', pkg]);
      return OpResult(ok: true, attempts: 1, costMs: sw.elapsedMilliseconds, detail: pkg);
    } catch (e) {
      return OpResult(ok: false, attempts: 1, costMs: sw.elapsedMilliseconds, detail: e.toString());
    }
  }

  @override
  Future<OpResult> tap(String serial, RPoint p, int w, int h) async {
    final sw = Stopwatch()..start();
    final (px, py) = p.toPixels(w, h);
    try {
      await shell(serial, ['uitest', 'uiInput', 'click', '$px', '$py']);
      return OpResult(ok: true, attempts: 1, costMs: sw.elapsedMilliseconds);
    } catch (e) {
      return OpResult(ok: false, attempts: 1, costMs: sw.elapsedMilliseconds, detail: e.toString());
    }
  }

  @override
  Future<OpResult> swipe(String serial, RPoint a, RPoint b, int w, int h, int ms) async {
    final sw = Stopwatch()..start();
    final (x1, y1) = a.toPixels(w, h);
    final (x2, y2) = b.toPixels(w, h);
    try {
      await shell(serial,
          ['uitest', 'uiInput', 'swipe', '$x1', '$y1', '$x2', '$y2', '$ms']);
      return OpResult(ok: true, attempts: 1, costMs: sw.elapsedMilliseconds);
    } catch (e) {
      return OpResult(ok: false, attempts: 1, costMs: sw.elapsedMilliseconds, detail: e.toString());
    }
  }

  @override
  Future<OpResult> keyEvent(String serial, String code) async {
    final sw = Stopwatch()..start();
    try {
      await shell(serial, ['uitest', 'uiInput', 'keyEvent', code]);
      return OpResult(ok: true, attempts: 1, costMs: sw.elapsedMilliseconds);
    } catch (e) {
      return OpResult(ok: false, attempts: 1, costMs: sw.elapsedMilliseconds, detail: e.toString());
    }
  }

  @override
  Future<Uint8List?> screenshot(String serial) async {
    try {
      final devPath = '/data/local/tmp/phonectrl_shot.jpeg';
      final tmp = File(
          '${Directory.systemTemp.path}/phonectrl_hdc_$serial.jpeg');
      await run(['-t', serial, 'shell', 'snapshot_display', '-f', devPath]);
      final recv = await run(['-t', serial, 'file', 'recv', devPath, tmp.path]);
      if (recv.exitCode != 0 || !tmp.existsSync()) return null;
      final bytes = await tmp.readAsBytes();
      try {
        await tmp.delete();
      } catch (_) {}
      return bytes;
    } catch (_) {
      return null;
    }
  }

  @override
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

  @override
  Future<bool> waitApp(String serial, String pkg,
      {Duration timeout = const Duration(seconds: 10)}) {
    return waitFor(serial, () async {
      final a = await currentActivity(serial);
      return a.contains(pkg);
    }, timeout: timeout);
  }
}
