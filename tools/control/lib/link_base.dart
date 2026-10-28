// PhoneCtrl — transport abstraction over a connected device.
//
// A `Link` is the common interface implemented by both `Adb` (Android / adb) and
// `Hdc` (HarmonyOS / OpenHarmony, via the `hdc` tool). Device operations are
// always keyed by `serial`; the active transport is discovered per device so a
// single server can drive a mix of Android and HarmonyOS phones at once — the
// same way GamePouch talks to a pura70 over `hdc` instead of `adb`.
//
// This file is intentionally import-light (no Adb/Hdc) to avoid an import cycle;
// `LinkRouter` lives in `link.dart` and wires the concrete backends together.

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'device.dart';
import 'models.dart';

/// Common device-control surface. Both `Adb` and `Hdc` implement this so the
/// rest of the app (automation / screen / scheduler / HTTP handlers) is
/// transport-agnostic.
abstract class Link {
  /// Transport tag: 'adb' or 'hdc'.
  String get transport;

  /// Transport for a specific serial (defaults to this link's own transport).
  String transportOf(String serial) => transport;

  Future<ProcessResult> run(List<String> args);

  /// `<tool> -s/-t <serial> shell <cmd...>`; non-zero exit throws.
  Future<String> shell(String serial, List<String> cmd);

  /// Run a shell command but never throw (returns stdout, or '' on failure).
  Future<String> shellOr(String serial, List<String> cmd, [String fallback = '']);

  /// Enumerate connected devices of this transport.
  Future<List<Device>> devices();

  /// Physical screen resolution [w, h].
  Future<(int, int)> resolution(String serial);

  /// Battery percentage, or null if unavailable.
  Future<int?> battery(String serial);

  /// Currently focused activity / ability (best-effort on HarmonyOS).
  Future<String> currentActivity(String serial);

  /// Launch an app.
  Future<OpResult> openApp(String serial, String pkg, [String? activity]);

  /// Tap at relative coordinates.
  Future<OpResult> tap(String serial, RPoint p, int w, int h);

  /// Swipe from relative a -> b over [ms] milliseconds.
  Future<OpResult> swipe(String serial, RPoint a, RPoint b, int w, int h, int ms);

  /// Inject a key event (transport-specific code semantics).
  Future<OpResult> keyEvent(String serial, String code);

  /// Capture a screenshot to PNG/JPEG bytes (binary-safe), or null on failure.
  Future<Uint8List?> screenshot(String serial);

  /// Poll until [predicate] returns true or [timeout] elapses.
  Future<bool> waitFor(
    String serial,
    Future<bool> Function() predicate, {
    Duration timeout = const Duration(seconds: 10),
    Duration interval = const Duration(milliseconds: 300),
  });

  /// Wait until [pkg] is the focused app.
  Future<bool> waitApp(String serial, String pkg,
      {Duration timeout = const Duration(seconds: 10)});
}
