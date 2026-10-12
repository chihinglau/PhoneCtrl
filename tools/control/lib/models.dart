// PhoneCtrl — shared data models.
//
// Zero-dependency (dart:core only). Coordinates are expressed in RELATIVE
// units (0.0 ~ 1.0) against the device resolution, so the same task script
// adapts to any phone / orientation.

/// A normalized rectangle in relative screen coordinates (0..1).
class Rect {
  const Rect(this.x, this.y, this.w, this.h);
  final double x, y, w, h;

  /// Convert to absolute pixel coordinates given a [w]x[h] resolution.
  List<int> toPixels(int width, int height) => [
        (x * width).round(),
        (y * height).round(),
        (w * width).round(),
        (h * height).round(),
      ];

  /// Center point in relative coords.
  (double, double) get center => (x + w / 2, y + h / 2);

  Rect operator *(double s) => Rect(x, y, w * s, h * s);

  @override
  String toString() => 'Rect(${x.toStringAsFixed(3)},${y.toStringAsFixed(3)},'
      '${w.toStringAsFixed(3)},${h.toStringAsFixed(3)})';
}

/// A single point in relative coords.
class RPoint {
  const RPoint(this.x, this.y);
  final double x, y;
  (int, int) toPixels(int width, int height) =>
      ((x * width).round(), (y * height).round());
}

/// Result of one atomic operation (tap / swipe / openApp ...).
class OpResult {
  OpResult({
    required this.ok,
    required this.attempts,
    required this.costMs,
    this.detail = '',
  });
  final bool ok;
  final int attempts;
  final int costMs;
  final String detail;

  @override
  String toString() =>
      'OpResult(ok=$ok, attempts=$attempts, ${costMs}ms, "$detail")';
}

/// Phases of a single automation round (state machine, mirrors SyncJob stages).
enum TaskPhase {
  idle,
  opening,
  swiping,
  adClicking,
  rewarding,
  videoAd,
  verifying,
  done,
  failed,
}

extension TaskPhaseLabel on TaskPhase {
  String get label => switch (this) {
        TaskPhase.idle => '空闲',
        TaskPhase.opening => '启动 App',
        TaskPhase.swiping => '滑屏',
        TaskPhase.adClicking => '点击广告',
        TaskPhase.rewarding => '领取奖励',
        TaskPhase.videoAd => '看视频领奖',
        TaskPhase.verifying => '校验',
        TaskPhase.done => '完成',
        TaskPhase.failed => '失败',
      };
}

/// Exponential-backoff retry policy (mirrors GamePouch SyncJob fail-counting).
class RetryPolicy {
  const RetryPolicy({
    this.maxAttempts = 3,
    this.base = const Duration(milliseconds: 500),
    this.factor = 2.0,
    this.maxBackoff = const Duration(seconds: 8),
  });

  final int maxAttempts;
  final Duration base;
  final double factor;
  final Duration maxBackoff;

  /// Backoff for the given (1-based) attempt, with +/-20% jitter.
  Duration backoff(int attempt, [double jitter = 0.2]) {
    final raw = base.inMilliseconds * (factor.pow(attempt - 1));
    final j = (raw * jitter) * (0.5 - (DateTime.now().microsecond % 1000) / 1000);
    final v = (raw + j).clamp(0, maxBackoff.inMilliseconds).round();
    return Duration(milliseconds: v);
  }
}

extension _Pow on double {
  double pow(int n) => n <= 0 ? 1 : List.filled(n, this).reduce((a, b) => a * b);
}

/// Live status of one device, surfaced by Monitor over HTTP.
class DeviceStatus {
  DeviceStatus({
    required this.serial,
    required this.model,
    required this.online,
    this.batteryPct,
    this.currentActivity = '',
    this.phase = TaskPhase.idle,
    this.round = 0,
    this.totalRounds = 0,
    this.success = 0,
    this.failed = 0,
    this.lastError = '',
    this.lastFramePath = '',
    this.taskKind = '',
    this.swipeDir = '',
    this.swipeCount = 0,
    this.nextSwipeMs,
    this.nextSwipeAt,
    this.swipeAuto = false,
  });
  final String serial;
  final String model;
  bool online;
  int? batteryPct;
  String currentActivity;
  TaskPhase phase;
  int round;
  int totalRounds;
  int success;
  int failed;
  String lastError;
  String lastFramePath;

  // ---- swipe-job state (appended) ----
  String taskKind; // '' | 'script' | 'swipe'
  String swipeDir; // '' | 'up' | 'down' | 'left' | 'right'
  int swipeCount;
  int? nextSwipeMs; // 上次调度写入的剩余毫秒（快照用）
  int? nextSwipeAt; // 下次滑屏绝对时间戳(ms)，前端倒计时
  bool swipeAuto; // 该滑屏任务是否由“上线自动滑屏”自动触发

  Map<String, dynamic> toJson() => {
        'serial': serial,
        'model': model,
        'online': online,
        'batteryPct': batteryPct,
        'currentActivity': currentActivity,
        'phase': phase.name,
        'phaseLabel': phase.label,
        'round': round,
        'totalRounds': totalRounds,
        'success': success,
        'failed': failed,
        'lastError': lastError,
        'lastFramePath': lastFramePath,
        'taskKind': taskKind,
        'swipeDir': swipeDir,
        'swipeCount': swipeCount,
        'nextSwipeMs': nextSwipeMs,
        'nextSwipeAt': nextSwipeAt,
        'swipeAuto': swipeAuto,
      };
}
