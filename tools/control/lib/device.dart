// PhoneCtrl — device model + discovery.
//
// Mirrors the reference project's `Device` / `Adb.devices()` pattern.

class Device {
  Device(this.serial, this.state, this.model, [this.transport = 'adb']);
  final String serial;
  final String state; // "device" | "offline" | "unauthorized" | ...
  final String model;
  final String transport; // 'adb' (Android) | 'hdc' (HarmonyOS / OpenHarmony)

  /// ADB reports `device` only when authorized + reachable; hdc's `list targets`
  /// only returns connected targets, so treat any non-empty state as ready.
  bool get ready => state == 'device' || transport == 'hdc';

  Map<String, dynamic> toJson() => {
        'serial': serial,
        'state': state,
        'model': model,
        'transport': transport,
        'ready': ready,
      };

  @override
  String toString() => 'Device($serial $state $model [$transport])';
}
