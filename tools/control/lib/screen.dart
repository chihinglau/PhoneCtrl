// PhoneCtrl — integrated live "投屏" (mirroring) for the web console.
//
// Design (see docs §1.3):
//  - A continuous `adb exec-out screencap -p` loop captures the screen as PNG.
//    This is the single capture path; the PNG feeds both the on-demand
//    /frame/ endpoint (raw, high quality) and the live preview pipeline.
//  - The live preview is a smooth, low-bandwidth MJPEG stream: each captured
//    PNG is piped into a long-lived `ffmpeg` process that scales it down
//    (default 480px wide) and re-encodes to JPEG. The server slices ffmpeg's
//    concatenated JPEG output into multipart frames. This keeps each frame
//    ~30-50x smaller than the raw PNG so the browser decodes without jank.
//  - A separate, on-demand `scrcpy` window is available for high-performance
//    desktop mirroring (see startMirror).
//
// Why not `screenrecord`? Several vendor ROMs (e.g. Huawei) omit the
// `screenrecord` binary, so we rely on per-frame `screencap` + ffmpeg instead.

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'adb.dart';

class ScreenManager {
  ScreenManager(this.adb, {this.frameDir, this.streamWidth = 480, this.streamQuality = 5});
  final Adb adb;
  final Directory? frameDir;

  /// Width (px) the live preview is scaled to; height follows aspect ratio.
  final int streamWidth;
  /// ffmpeg MJPEG quality (2=best .. 31=worst).
  final int streamQuality;

  final Map<String, Process> _mirrors = {}; // scrcpy processes
  final Map<String, Uint8List> _latest = {}; // latest raw PNG (for /frame/)
  final Map<String, int> _seq = {}; // bumped on each fresh capture
  final Map<String, bool> _captureRunning = {};

  final Map<String, Process> _ffmpeg = {}; // long-lived transcoder per device
  final Map<String, StreamController<Uint8List>> _frameCtl = {};
  final Map<String, int> _subs = {}; // stream subscriber count per device
  final Map<String, Uint8List> _latestJpeg = {}; // latest encoded frame
  final Map<String, Timer> _ffmpegIdle = {}; // idle shutdown timer

  // --------------------------------------------------------------------------
  // scrcpy desktop mirror (high performance, on demand).
  // --------------------------------------------------------------------------
  Future<void> startMirror(String serial) async {
    if (_mirrors.containsKey(serial)) return;
    final exe = _scrcpyExe();
    if (exe == null) {
      stderr.writeln('[screen] scrcpy not found; desktop mirror skipped for $serial');
      return;
    }
    final p = await Process.start(exe, [
      '--serial', serial,
      '--window-title', 'PhoneCtrl $serial',
      '--max-fps', '30',
    ]);
    _mirrors[serial] = p;
    stderr.writeln('[screen] scrcpy mirror started for $serial (pid ${p.pid})');
  }

  void stopMirror(String serial) {
    _mirrors.remove(serial)?.kill();
  }

  // --------------------------------------------------------------------------
  // Continuous capture loop (PNG). Feeds /frame/ and (when subscribers exist)
  // the ffmpeg MJPEG pipeline.
  // --------------------------------------------------------------------------
  void startLive(String serial) {
    if (_captureRunning.containsKey(serial)) return;
    _captureRunning[serial] = true;
    _captureLoop(serial);
  }

  void stopLive(String serial) {
    _captureRunning[serial] = false;
    stopStream(serial);
  }

  Future<void> _captureLoop(String serial) async {
    while (_captureRunning[serial] == true) {
      try {
        final png = await adb.screenshot(serial);
        if (png != null && png.isNotEmpty) {
          _latest[serial] = png;
          _seq[serial] = (_seq[serial] ?? 0) + 1;
          if (frameDir != null) {
            await File('${frameDir!.path}/$serial.png').writeAsBytes(png, flush: true);
          }
          final ff = _ffmpeg[serial];
          if (ff != null) {
            // Feed the raw PNG to the transcoder. If ffmpeg is missing we fall
            // back to emitting the PNG itself (see _ensureFfmpeg).
            try {
              ff.stdin.add(png);
            } catch (_) {
              // broken pipe; will be reaped on exitCode
            }
          } else {
            // No transcoder: stream the PNG directly (heavier, but works).
            _latestJpeg[serial] = png;
            _emit(serial, png);
          }
        }
      } catch (_) {
        // transient adb hiccup; keep last frame
      }
      await Future.delayed(const Duration(milliseconds: 30));
    }
  }

  /// Latest raw PNG preview frame, or null if none yet.
  Uint8List? latestPng(String serial) => _latest[serial];

  /// Latest encoded preview frame (JPEG when ffmpeg is active, else PNG).
  Uint8List? latestJpeg(String serial) => _latestJpeg[serial];

  /// Monotonic frame counter; lets callers detect stale frames.
  int latestSeq(String serial) => _seq[serial] ?? 0;

  // --------------------------------------------------------------------------
  // ffmpeg-backed MJPEG frame stream.
  // --------------------------------------------------------------------------
  /// Subscribe to the live preview stream for [serial]. Starts the transcoder
  /// (and refcounts subscribers) on first call.
  Stream<Uint8List> frameStream(String serial) {
    _frameCtl.putIfAbsent(serial, () => StreamController<Uint8List>.broadcast());
    return _frameCtl[serial]!.stream;
  }

  /// Signal a stream viewer attached; keeps the transcoder alive.
  void startStream(String serial) {
    frameStream(serial); // ensure controller exists
    _subs[serial] = (_subs[serial] ?? 0) + 1;
    _ffmpegIdle[serial]?.cancel();
    _ffmpegIdle.remove(serial);
    _ensureFfmpeg(serial);
  }

  /// Signal a stream viewer detached; shuts the transcoder down after idle.
  void stopStream(String serial) {
    final n = (_subs[serial] ?? 1) - 1;
    if (n <= 0) {
      _subs[serial] = 0;
      _ffmpegIdle[serial]?.cancel();
      _ffmpegIdle[serial] = Timer(const Duration(seconds: 3), () => _killFfmpeg(serial));
    } else {
      _subs[serial] = n;
    }
  }

  void _ensureFfmpeg(String serial) {
    if (_ffmpeg.containsKey(serial)) return;
    final exe = _ffmpegExe();
    if (exe == null) {
      stderr.writeln('[screen] ffmpeg not found; streaming raw PNG for $serial');
      return;
    }
    Process.start(exe, [
      '-hide_banner',
      '-loglevel', 'error',
      '-fflags', '+nobuffer',
      '-flags', 'low_delay',
      '-f', 'image2pipe',
      '-i', '-',
      '-vf', 'scale=${streamWidth}:-2',
      '-f', 'image2pipe',
      '-c:v', 'mjpeg',
      '-q:v', '$streamQuality',
      '-flush_packets', '1',
      '-',
    ]).then((p) {
      _ffmpeg[serial] = p;
      p.exitCode.then((_) {
        _ffmpeg.remove(serial);
      });
      _pumpFfmpeg(serial, p);
    }).catchError((e) {
      stderr.writeln('[screen] ffmpeg start failed for $serial: $e');
    });
  }

  void _killFfmpeg(String serial) {
    _ffmpegIdle.remove(serial);
    final p = _ffmpeg.remove(serial);
    p?.kill();
  }

  /// Read ffmpeg's concatenated-JPEG stdout, split into individual JPEG frames
  /// (each begins with FFD8 and ends with FFD9), and broadcast them.
  Future<void> _pumpFfmpeg(String serial, Process p) async {
    final buf = <int>[];
    try {
      await for (final chunk in p.stdout) {
        buf.addAll(chunk);
        var i = 0;
        while (i < buf.length - 1) {
          if (buf[i] == 0xFF && buf[i + 1] == 0xD8) {
            var j = i + 2;
            var found = false;
            while (j < buf.length - 1) {
              if (buf[j] == 0xFF && buf[j + 1] == 0xD9) {
                final frame = Uint8List.fromList(buf.sublist(i, j + 2));
                _latestJpeg[serial] = frame;
                _emit(serial, frame);
                buf.removeRange(0, j + 2);
                i = 0;
                found = true;
                break;
              }
              j++;
            }
            if (!found) break; // wait for the rest of this frame
          } else {
            i++;
          }
        }
      }
    } catch (_) {
      // pipe closed
    }
  }

  void _emit(String serial, Uint8List frame) {
    final ctl = _frameCtl[serial];
    if (ctl != null && !ctl.isClosed) ctl.add(frame);
  }

  /// Capture a frame, optionally persist to <frameDir>/<serial>.png for the
  /// web console, and return the PNG bytes.
  Future<List<int>?> capture(String serial, {bool persist = true}) async {
    final bytes = await adb.screenshot(serial);
    if (bytes == null || bytes.isEmpty) return null;
    if (persist && frameDir != null) {
      final f = File('${frameDir!.path}/$serial.png');
      await f.writeAsBytes(bytes, flush: true);
    }
    return bytes;
  }

  /// A tiny mean-absolute-difference over downscaled bytes as a "stillness"
  /// proxy. Returns true when two consecutive frames are visually stable.
  static bool isStable(List<int> a, List<int> b, {double threshold = 0.02}) {
    if (a.length != b.length || a.isEmpty) return false;
    int diff = 0;
    final n = a.length;
    for (int i = 0; i < n; i += 37) {
      diff += (a[i] - b[i]).abs();
    }
    return (diff / (n / 37)) / 255.0 < threshold;
  }

  String? _scrcpyExe() {
    final home = Platform.environment['ANDROID_HOME'];
    final candidates = [
      if (home != null) '$home/../scrcpy/scrcpy.exe',
      '.tools/scrcpy/scrcpy.exe',
      'scrcpy.exe',
    ];
    for (final c in candidates) {
      if (File(c).existsSync()) return c;
    }
    return null;
  }

  String? _ffmpegExe() {
    final e = Platform.environment['FFMPEG_PATH'];
    if (e != null && File(e).existsSync()) return e;
    if (_exeExists('ffmpeg')) return 'ffmpeg';
    final home = Platform.environment['ANDROID_HOME'];
    if (home != null) {
      final c = '$home/../ffmpeg/bin/ffmpeg.exe';
      if (File(c).existsSync()) return c;
    }
    return null;
  }

  bool _exeExists(String name) {
    try {
      final r = Process.runSync(name, ['-version']);
      return r.exitCode == 0;
    } catch (_) {
      return false;
    }
  }

  void dispose() {
    for (final p in _mirrors.values) p.kill();
    for (final p in _ffmpeg.values) p.kill();
    for (final t in _ffmpegIdle.values) t.cancel();
    _captureRunning.clear(); // loops exit once their key is gone
    _mirrors.clear();
    _ffmpeg.clear();
    _ffmpegIdle.clear();
    for (final c in _frameCtl.values) {
      if (!c.isClosed) c.close();
    }
    _frameCtl.clear();
  }
}
