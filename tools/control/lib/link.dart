// PhoneCtrl — transport router that discovers devices over both adb and hdc and
// routes per-serial calls to the right backend. See `link_base.dart` for the
// `Link` interface contract.

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'adb.dart';
import 'device.dart';
import 'hdc.dart';
import 'link_base.dart';
import 'models.dart';

/// Discovers devices over both adb and hdc, tags each `Device` with its
/// `transport`, and routes every per-serial call to the matching backend.
/// Construct this once and pass it wherever an `Adb` was used.
class LinkRouter implements Link {
  final Map<String, Link> _bySerial = {};

  void register(Device d) {
    _bySerial[d.serial] = d.transport == 'hdc' ? Hdc() : Adb();
  }

  Link _for(String serial) => _bySerial[serial] ?? Adb();

  @override
  String get transport => 'router';

  @override
  String transportOf(String serial) => _for(serial).transport;

  @override
  Future<ProcessResult> run(List<String> args) => Adb().run(args);

  @override
  Future<String> shell(String serial, List<String> cmd) =>
      _for(serial).shell(serial, cmd);

  @override
  Future<String> shellOr(String serial, List<String> cmd, [String fallback = '']) =>
      _for(serial).shellOr(serial, cmd, fallback);

  @override
  Future<List<Device>> devices() async {
    final out = <Device>[];
    try {
      out.addAll(await Adb().devices());
    } catch (_) {
      // adb absent / no Android devices — continue to hdc
    }
    try {
      out.addAll(await Hdc().devices());
    } catch (_) {
      // hdc absent / no HarmonyOS devices
    }
    for (final d in out) register(d);
    return out;
  }

  @override
  Future<(int, int)> resolution(String serial) => _for(serial).resolution(serial);

  @override
  Future<int?> battery(String serial) => _for(serial).battery(serial);

  @override
  Future<String> currentActivity(String serial) =>
      _for(serial).currentActivity(serial);

  @override
  Future<OpResult> openApp(String serial, String pkg, [String? activity]) =>
      _for(serial).openApp(serial, pkg, activity);

  @override
  Future<OpResult> tap(String serial, RPoint p, int w, int h) =>
      _for(serial).tap(serial, p, w, h);

  @override
  Future<OpResult> swipe(String serial, RPoint a, RPoint b, int w, int h, int ms) =>
      _for(serial).swipe(serial, a, b, w, h, ms);

  @override
  Future<OpResult> keyEvent(String serial, String code) =>
      _for(serial).keyEvent(serial, code);

  @override
  Future<Uint8List?> screenshot(String serial) => _for(serial).screenshot(serial);

  @override
  Future<bool> waitFor(
    String serial,
    Future<bool> Function() predicate, {
    Duration timeout = const Duration(seconds: 10),
    Duration interval = const Duration(milliseconds: 300),
  }) =>
      _for(serial).waitFor(serial, predicate,
          timeout: timeout, interval: interval);

  @override
  Future<bool> waitApp(String serial, String pkg,
          {Duration timeout = const Duration(seconds: 10)}) =>
      _for(serial).waitApp(serial, pkg, timeout: timeout);
}
