# 启动 Dart 服务（供桌面控制台 win_console/app.py 在检测不到后端时自动拉起）
# 使用相对路径，任意机器 clone 后可直接运行。
$root = $PSScriptRoot
$useEnv = Join-Path $root "scripts\use-env.ps1"
if (Test-Path $useEnv) { . $useEnv | Out-Null }
Set-Location $root
& dart run tools/control/server.dart
