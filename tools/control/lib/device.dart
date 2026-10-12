// PhoneCtrl — device model + discovery.
//
// Mirrors the reference project's `Device` / `Adb.devices()` pattern.

class Device {
  Device(this.serial, this.state, this.model);
  final String serial;
  final String state; // "device" | "offline" | "unauthorized" | ...
  final String model;

  /// ADB reports `device` only when authorized + reachable.
  bool get ready => state == 'device';

  Map<String, dynamic> toJson() => {
        'serial': serial,
        'state': state,
        'model': model,
        'ready': ready,
      };

  @override
  String toString() => 'Device($serial $state $model)';
}
