// PhoneCtrl — status monitor + event bus + state persistence.
//
// Mirrors GamePouch's in-memory Store + on-disk index philosophy: keep live
// DeviceStatus in memory, persist progress to .tools/phonectrl.state.json.

import 'dart:convert';
import 'dart:io';

import 'models.dart';

class Monitor {
  Monitor({this.stateFile});
  final File? stateFile;

  final Map<String, DeviceStatus> devices = {};
  final List<Map<String, dynamic>> events = [];
  final Map<String, int> progress = {}; // serial -> last completed round

  DeviceStatus status(String serial, String model) {
    return devices.putIfAbsent(
        serial, () => DeviceStatus(serial: serial, model: model, online: true));
  }

  void log(String serial, String msg, {bool error = false}) {
    final e = {'ts': DateTime.now().toIso8601String(), 'serial': serial, 'msg': msg, 'error': error};
    events.add(e);
    if (events.length > 500) events.removeAt(0);
    stderr.writeln('[monitor] ${error ? 'ERR ' : ''}$serial: $msg');
  }

  void persist() {
    if (stateFile == null) return;
    try {
      stateFile!.writeAsStringSync(jsonEncode({
        'progress': progress,
        'events': events.take(100).toList(),
      }));
    } catch (_) {}
  }

  void restore() {
    if (stateFile == null || !stateFile!.existsSync()) return;
    try {
      final j = jsonDecode(stateFile!.readAsStringSync()) as Map<String, dynamic>;
      progress.addAll((j['progress'] as Map).cast<String, int>());
    } catch (_) {}
  }

  Map<String, dynamic> snapshot() => {
        'devices': devices.values.map((d) => d.toJson()).toList(),
        'events': events.reversed.take(50).toList(),
        'progress': progress,
      };
}
