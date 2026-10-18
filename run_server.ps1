# 启动 Dart 服务（供桌面控制台 win_console/app.py 在检测不到后端时自动拉起）
# 使用相对路径，任意机器 clone 后可直接运行。
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
# 向上查找包含 pubspec.yaml 的目录作为项目根（dot-source 时 $PSScriptRoot 不可靠，故用此法）
$ProjRoot = $ScriptDir
while ($ProjRoot -and -not (Test-Path (Join-Path $ProjRoot "pubspec.yaml"))) {
    $parent = Split-Path -Parent $ProjRoot
    if ($parent -eq $ProjRoot) { break }
    $ProjRoot = $parent
}
if (-not (Test-Path (Join-Path $ProjRoot "pubspec.yaml"))) { $ProjRoot = $ScriptDir }

$useEnv = Join-Path $ProjRoot "scripts\use-env.ps1"
if (Test-Path $useEnv) { . $useEnv | Out-Null }
# use-env.ps1 内部会把 ErrorActionPreference 设为 Stop，这里复位，避免其
# 内部对 adb/scrcpy 的探测失败而中断后续 dart 启动。
$ErrorActionPreference = 'Continue'
Set-Location $ProjRoot
$log = Join-Path $ProjRoot ".tools\backend.log"
$logDir = Split-Path $log
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
Write-Host "[run_server] 项目根=$ProjRoot  后端日志 -> $log"
& dart run tools/control/server.dart 2>&1 | Tee-Object -FilePath $log -Append
