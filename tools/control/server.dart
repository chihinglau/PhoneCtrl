// PhoneCtrl — PC controller entry (zero-dependency Dart, dart:io only).
//
// HTTP monitor console (port 8858, mirrors GamePouch's 8848) + device task
// bus. Run:  dart run tools/control/server.dart
// Then open http://127.0.0.1:8858/

import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'lib/adb.dart';
import 'lib/config.dart';
import 'lib/device.dart';
import 'lib/detector.dart';
import 'lib/monitor.dart';
import 'lib/scheduler.dart';
import 'lib/screen.dart';
import 'lib/swipe.dart';
import 'lib/models.dart';

const appName = 'PhoneCtrl';
final httpPort = int.tryParse(Platform.environment['PC_PORT'] ?? '') ?? 8858;

final rootDir = File(Platform.script.toFilePath()).parent.parent.parent;
final frameDir = Directory('${rootDir.path}/.tools/frames');
final stateFile = File('${rootDir.path}/.tools/phonectrl.state.json');
final swipeCfgFile = File('${rootDir.path}/.tools/phonectrl.swipe.json');
final autoSwipeFile = File('${rootDir.path}/.tools/phonectrl.autoswipe.json');
AutoSwipeConfig _autoSwipe = AutoSwipeConfig();

final adb = Adb();
final screen = ScreenManager(adb, frameDir: frameDir);
final monitor = Monitor(stateFile: stateFile);
final ocr = TesseractOcrProvider();
final scheduler = Scheduler(adb, screen, monitor, ocr);

Future<void> main() async {
  frameDir.createSync(recursive: true);
  monitor.restore();
  _autoSwipe = _loadAutoSwipe();

  // discover devices + start live mirrors
  try {
    final devs = await adb.devices();
    for (final d in devs.where((d) => d.ready)) {
      monitor.status(d.serial, d.model);
      screen.startLive(d.serial); // integrated live preview (投屏) shown in console
    }
    stderr.writeln('[server] ${devs.where((d) => d.ready).length} ready device(s).');
  } catch (e) {
    stderr.writeln('[server] device discovery failed: $e');
  }

  // background device telemetry + auto-swipe reconciliation (guarded so a
  // transient adb failure never tears down the server). Also discovers
  // hot-plugged devices so the UI and auto-swipe react to connect/disconnect.
  await _reconcileAll();
  Timer.periodic(const Duration(seconds: 5), (_) async {
    await _reconcileAll();
  });

  final server = await HttpServer.bind(InternetAddress.loopbackIPv4, httpPort);
  stderr.writeln('[server] $appName up at http://127.0.0.1:$httpPort/');
  await for (final req in server) {
    _handle(req);
  }
}

void _handle(HttpRequest req) async {
  final p = req.uri.path;
  try {
    if (req.method == 'GET' && (p == '/' || p == '/index.html')) {
      await _serveHtml(req);
      return;
    }
    if (req.method == 'GET' && p.startsWith('/stream/')) {
      await _serveStream(req, p);
      return;
    }
    if (req.method == 'GET' && p.startsWith('/frame/')) {
      await _serveFrame(req, p);
      return;
    }
    if (req.method == 'GET' && p == '/api/status') {
      await _json(req, monitor.snapshot());
      return;
    }
    if (req.method == 'GET' && p == '/api/devices') {
      final devs = await adb.devices();
      await _json(req, devs.map((d) => d.toJson()).toList());
      return;
    }
    if (req.method == 'GET' && p == '/api/swipe/config') {
      await _json(req, _loadPresets().map((p) => p.toJson()).toList());
      return;
    }
    if (req.method == 'POST' && p == '/api/swipe/config') {
      final body = await utf8.decoder.bind(req).join();
      try {
        final list = jsonDecode(body) as List;
        final ps = list.map((e) => SwipePreset.fromJson(e as Map<String, dynamic>)).toList();
        swipeCfgFile.writeAsStringSync(jsonEncode(ps.map((e) => e.toJson()).toList()));
        await _json(req, {'ok': true, 'presets': ps.length});
      } catch (e) {
        await _json(req, {'error': 'bad config: $e'}, status: 400);
      }
      return;
    }
    if (req.method == 'POST' && p == '/api/swipe/start') {
      await _startSwipe(req);
      return;
    }
    if (req.method == 'GET' && p == '/api/autoswipe') {
      await _json(req, _autoSwipe.toJson());
      return;
    }
    if (req.method == 'POST' && p == '/api/autoswipe') {
      final body = await utf8.decoder.bind(req).join();
      try {
        _autoSwipe = AutoSwipeConfig.fromJson(jsonDecode(body) as Map<String, dynamic>);
        autoSwipeFile.writeAsStringSync(jsonEncode(_autoSwipe.toJson()));
        await _reconcileAll();
        await _json(req, {'ok': true, 'config': _autoSwipe.toJson()});
      } catch (e) {
        await _json(req, {'error': 'bad auto config: $e'}, status: 400);
      }
      return;
    }
    if (req.method == 'GET' && p == '/api/task_sample') {
      final f = File('${rootDir.path}/tasks/sample_app.json');
      req.response
        ..headers.contentType = ContentType.json
        ..write(f.existsSync() ? f.readAsStringSync() : '{}')
        ..close();
      return;
    }
    if (req.method == 'POST' && p == '/api/task') {
      await _startTask(req);
      return;
    }
    if (req.method == 'POST' && p.startsWith('/api/device/') && p.endsWith('/stop')) {
      final serial = p.split('/')[3];
      scheduler.stop(serial);
      await _json(req, {'ok': true, 'stopped': serial});
      return;
    }
    if (req.method == 'POST' && p.startsWith('/api/device/') && p.endsWith('/tap')) {
      await _deviceTap(req, p.split('/')[3]);
      return;
    }
    if (req.method == 'POST' && p.startsWith('/api/device/') && p.endsWith('/key')) {
      await _deviceKey(req, p.split('/')[3]);
      return;
    }
    if (req.method == 'POST' && p.startsWith('/api/device/') && p.endsWith('/unlock')) {
      await _deviceUnlock(req, p.split('/')[3]);
      return;
    }
    if (req.method == 'POST' && p.startsWith('/api/device/') && p.endsWith('/mirror')) {
      final serial = p.split('/')[3];
      screen.startMirror(serial);
      await _json(req, {'ok': true, 'mirror': serial});
      return;
    }
    await _json(req, {'error': 'not found'}, status: 404);
  } catch (e) {
    await _json(req, {'error': e.toString()}, status: 500);
  }
}

Future<void> _serveHtml(HttpRequest req) async {
  final html = File('${File(Platform.script.toFilePath()).parent.path}/web/index.html');
  if (!html.existsSync()) {
    req.response
      ..statusCode = 404
      ..write('web console missing')
      ..close();
    return;
  }
  req.response
    ..headers.contentType = ContentType.html
    ..write(html.readAsStringSync())
    ..close();
}

Future<void> _serveFrame(HttpRequest req, String path) async {
  final serial = path.substring('/frame/'.length).replaceAll('.png', '');
  final bytes = screen.latestPng(serial);
  if (bytes != null && bytes.isNotEmpty) {
    req.response
      ..headers.contentType = ContentType.parse('image/png')
      ..add(bytes)
      ..close();
    return;
  }
  final f = File('${frameDir.path}/$serial.png');
  if (!f.existsSync()) {
    req.response.statusCode = 404;
    await req.response.close();
    return;
  }
  req.response
    ..headers.contentType = ContentType.parse('image/png')
    ..add(f.readAsBytesSync())
    ..close();
}

/// Live "投屏" stream: multipart/x-mixed-replace MJPEG built from the cached
/// preview frames. The web console shows this inside its device card so the
/// mirror and the automation controls live in one surface.
Future<void> _serveStream(HttpRequest req, String path) async {
  final serial = path.substring('/stream/'.length);
  final boundary = 'PHONECTRLFRAME';
  req.response.headers
    ..contentType = ContentType.parse('multipart/x-mixed-replace; boundary=$boundary')
    ..chunkedTransferEncoding = true;
  var running = true;
  req.response.done
      .then((_) => running = false)
      .catchError((_) => running = false);

  // Refcount the transcoder so it only runs while someone is watching.
  screen.startStream(serial);

  // Send the most recent frame immediately so the card paints at once.
  final first = screen.latestJpeg(serial) ?? screen.latestPng(serial);
  if (first != null) _writeStreamPart(req, boundary, first);

  late final StreamSubscription<Uint8List> sub;
  sub = screen.frameStream(serial).listen((frame) {
    if (!running) {
      sub.cancel();
      return;
    }
    _writeStreamPart(req, boundary, frame);
  });

  try {
    while (running) {
      await Future.delayed(const Duration(milliseconds: 250));
    }
  } finally {
    sub.cancel();
    screen.stopStream(serial);
    try {
      await req.response.close();
    } catch (_) {}
  }
}

void _writeStreamPart(HttpRequest req, String boundary, List<int> bytes) {
  final isJpeg = bytes.length >= 2 && bytes[0] == 0xFF && bytes[1] == 0xD8;
  req.response
    ..write('--$boundary\r\n')
    ..write('Content-Type: ${isJpeg ? "image/jpeg" : "image/png"}\r\n')
    ..write('Content-Length: ${bytes.length}\r\n\r\n')
    ..add(bytes)
    ..write('\r\n');
  req.response.flush();
}

/// Tap the phone screen at normalized coordinates (nx, ny in 0..1).
Future<void> _deviceTap(HttpRequest req, String serial) async {
  final body = await utf8.decoder.bind(req).join();
  final j = jsonDecode(body) as Map<String, dynamic>;
  final nx = (j['nx'] ?? 0.5) as num;
  final ny = (j['ny'] ?? 0.5) as num;
  final (w, h) = await adb.resolution(serial);
  final px = (nx * w).round();
  final py = (ny * h).round();
  final r = await adb.shellOr(serial, ['input', 'tap', '$px', '$py']);
  await _json(req, {'ok': true, 'px': px, 'py': py, 'detail': r});
}

/// Inject a key event (code: 3=HOME, 4=BACK, ...).
Future<void> _deviceKey(HttpRequest req, String serial) async {
  final body = await utf8.decoder.bind(req).join();
  final j = jsonDecode(body) as Map<String, dynamic>;
  final code = (j['code'] ?? '3').toString();
  final r = await adb.keyEvent(serial, code);
  await _json(req, {'ok': r.ok, 'detail': r.detail});
}

/// 一键解锁：唤醒 -> (可选)上滑进入密码页 -> 依次输入 PIN 数字。
/// body: { password: "1234", swipeUp: true }
Future<void> _deviceUnlock(HttpRequest req, String serial) async {
  final body = await utf8.decoder.bind(req).join();
  final j = jsonDecode(body) as Map<String, dynamic>;
  final pwd = (j['password'] ?? '').toString();
  final swipeUp = (j['swipeUp'] ?? true) as bool;
  final entered = <String>[];

  // 1) 唤醒（KEYCODE_WAKEUP=224，不会在亮屏时熄灭）
  await adb.keyEvent(serial, '224');
  await Future.delayed(const Duration(milliseconds: 350));

  // 2) 上滑跳过锁屏滑动页，露出密码键盘
  if (swipeUp) {
    final (w, h) = await adb.resolution(serial);
    await adb.swipe(serial, RPoint(0.5, 0.82), RPoint(0.5, 0.16), w, h, 300);
    await Future.delayed(const Duration(milliseconds: 350));
  }

  // 3) 逐位输入数字（KEYCODE_0=7 ... KEYCODE_9=16）
  for (final ch in pwd.split('')) {
    final c = ch.codeUnitAt(0);
    if (c >= 0x30 && c <= 0x39) {
      final code = 7 + (c - 0x30);
      await adb.keyEvent(serial, '$code');
      entered.add(ch);
      await Future.delayed(const Duration(milliseconds: 120));
    }
  }

  await _json(req, {'ok': true, 'digits': entered.length, 'detail': entered.join('')});
}

Future<void> _startTask(HttpRequest req) async {
  final body = await utf8.decoder.bind(req).join();
  final j = jsonDecode(body) as Map<String, dynamic>;
  final script = TaskScript.fromJson(j['script'] ?? j);
  String? serial = j['serial'];
  if (serial == null || serial.isEmpty) {
    final devs = await adb.devices();
    serial = devs.where((d) => d.ready).map((d) => d.serial).firstOrNull;
  }
  if (serial == null) {
    return _json(req, {'error': 'no ready device and no serial given'}, status: 400);
  }
  final devs = await adb.devices();
  final dev = devs.firstWhere((d) => d.serial == serial,
      orElse: () => Device(serial!, 'unknown', ''));
  scheduler.submit(serial, dev.model, script);
  monitor.log(serial, 'task submitted: ${script.appName}');
  await _json(req, {'ok': true, 'serial': serial, 'app': script.appName});
}

int _toInt(dynamic v, int fallback) => v is num ? v.toInt() : fallback;

/// Load swipe interval presets: saved overrides win, else built-in defaults.
List<SwipePreset> _loadPresets() {
  if (swipeCfgFile.existsSync()) {
    try {
      final list = jsonDecode(swipeCfgFile.readAsStringSync()) as List;
      final ps = list.map((e) => SwipePreset.fromJson(e as Map<String, dynamic>)).toList();
      if (ps.isNotEmpty) return ps;
    } catch (_) {}
  }
  return defaultSwipePresets();
}

/// Load the "auto-swipe on connect" config (defaults to disabled).
AutoSwipeConfig _loadAutoSwipe() {
  if (autoSwipeFile.existsSync()) {
    try {
      return AutoSwipeConfig.fromJson(
          jsonDecode(autoSwipeFile.readAsStringSync()) as Map<String, dynamic>);
    } catch (_) {}
  }
  return AutoSwipeConfig();
}

/// Build the concrete swipe job from the current auto-swipe config.
SwipeJob _autoJob() => _autoSwipe.toJob(_loadPresets());

/// Poll devices, refresh monitor statuses (incl. hot-plugged devices), and
/// reconcile the "auto-swipe on connect" policy:
///  - enabled + device online + no running task  -> start an auto swipe
///  - device offline / auto disabled             -> stop an auto-started swipe
Future<void> _reconcileAll() async {
  try {
    final devs = await adb.devices();
    final seen = <String>{};
    for (final d in devs) {
      seen.add(d.serial);
      final st = monitor.status(d.serial, d.model);
      st.online = d.ready;
      if (d.ready) screen.startLive(d.serial);
      st.batteryPct = await adb.battery(d.serial);
      st.currentActivity = await adb.currentActivity(d.serial);
      if (_autoSwipe.enabled) {
        if (d.ready && st.taskKind.isEmpty) {
          st.swipeAuto = true;
          scheduler.submitSwipe(d.serial, d.model, _autoJob());
          monitor.log(d.serial, 'auto-swipe started (device online)');
        }
      } else if (st.swipeAuto) {
        scheduler.stop(d.serial);
        st.swipeAuto = false;
      }
    }
    for (final s in monitor.devices.keys) {
      if (!seen.contains(s)) {
        final st = monitor.devices[s]!;
        st.online = false;
        screen.stopLive(s);
        if (st.swipeAuto) {
          scheduler.stop(s);
          st.swipeAuto = false;
        }
      }
    }
  } catch (e) {
    stderr.writeln('[server] reconcile error: $e');
  }
}

Future<void> _startSwipe(HttpRequest req) async {
  final body = await utf8.decoder.bind(req).join();
  final j = jsonDecode(body) as Map<String, dynamic>;
  String direction = (j['direction'] ?? 'up').toString();
  if (!const {'up', 'down', 'left', 'right'}.contains(direction)) direction = 'up';

  // resolve target device (default: first ready device)
  String? serial = j['serial'];
  if (serial == null || serial.isEmpty) {
    final devs = await adb.devices();
    serial = devs.where((d) => d.ready).map((d) => d.serial).firstOrNull;
  }
  if (serial == null) {
    return _json(req, {'error': 'no ready device and no serial given'}, status: 400);
  }

  // resolve interval range: custom min/max, else a named preset
  int minMs, maxMs;
  final useCustom = j['custom'] == true || j['minMs'] != null;
  if (useCustom) {
    minMs = _toInt(j['minMs'], 1000);
    maxMs = _toInt(j['maxMs'], 3000);
  } else {
    final key = (j['rangeKey'] ?? 'fast').toString();
    final preset =
        _loadPresets().firstWhere((p) => p.key == key, orElse: () => defaultSwipePresets().first);
    minMs = preset.minMs;
    maxMs = preset.maxMs;
  }
  if (minMs < 100) minMs = 100;
  if (maxMs < minMs) maxMs = minMs;

  final devs = await adb.devices();
  final dev = devs.firstWhere((d) => d.serial == serial,
      orElse: () => Device(serial!, 'unknown', ''));
  monitor.status(serial, dev.model).swipeAuto = false; // 手动启动不算自动
  scheduler.submitSwipe(serial, dev.model, SwipeJob(direction, minMs, maxMs));
  monitor.log(serial, 'swipe requested: $direction [${minMs}-${maxMs}ms]');
  await _json(req, {
    'ok': true,
    'serial': serial,
    'direction': direction,
    'minMs': minMs,
    'maxMs': maxMs,
  });
}

Future<void> _json(HttpRequest req, Object data, {int status = 200}) async {
  req.response
    ..statusCode = status
    ..headers.contentType = ContentType.json
    ..write(jsonEncode(data))
    ..close();
}

extension _FirstOrNull<E> on Iterable<E> {
  E? get firstOrNull => isEmpty ? null : first;
}
