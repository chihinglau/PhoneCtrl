#requires -Version 5.1
<#
  PhoneCtrl one-click launcher (mirrors MyGameSim scripts/library-start.ps1).
    Usage:   powershell -File scripts\phone-ctrl.ps1
             powershell -File scripts\phone-ctrl.ps1 -Port 8858 -NoBrowser
  Behaviour:
    - Loads scripts\use-env.ps1
    - Kills any leftover dart process holding the target port
    - Verifies an Android device is connected over adb (warns if not)
    - Optionally opens scrcpy live mirror window(s)
    - Boots tools\control\server.dart in a detached console
    - Waits for http://127.0.0.1:<port>/ then opens it (skip with -NoBrowser)
#>
[CmdletBinding()]
param(
  [int]$Port = 8858,
  [switch]$NoBrowser,
  [switch]$NoMirror
)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

. (Join-Path $PSScriptRoot 'use-env.ps1') | Out-Null
$dartExe  = Join-Path $Root '.tools\flutter\bin\cache\dart-sdk\bin\dart.exe'
if (-not (Test-Path $dartExe)) { $dartExe = 'dart' }   # fall back to global dart
$entry   = Join-Path $Root 'tools/control/server.dart'
$logFile = Join-Path $Root '.tools\phonectrl.log'
$pidFile = Join-Path $Root '.tools\phonectrl.pid'

if (-not (Test-Path $entry)) { throw "server entry not found at $entry." }
Write-Host '[start] PhoneCtrl' -ForegroundColor Cyan
Write-Host "        port = $Port`n        root = $Root"

# ---- port reuse protection ----
function Test-PortOpen([int]$p) {
  try {
    $c = New-Object System.Net.Sockets.TcpClient
    $iar = $c.BeginConnect('127.0.0.1', $p, $null, $null)
    $ok = $iar.AsyncWaitHandle.WaitOne(200, $false)
    if ($ok) { $c.EndConnect($iar); $c.Close(); return $true }
    $c.Close(); return $false
  } catch { return $false }
}
if (Test-PortOpen $Port) {
  Write-Warning "[start] port $Port busy. Kill previous PhoneCtrl server..."
  Get-Process -Name dart -ErrorAction SilentlyContinue | Where-Object {
    $_.CommandLine -like "*control/server.dart*" } | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
}

# ---- adb sanity ----
$adbReady = $false
try {
  & adb start-server | Out-Null
  $out = (& adb devices) -join "`n"
  if ($out -match 'device$') { $adbReady = $true }
} catch {}
if ($adbReady) { Write-Host '[start] adb device detected.' -ForegroundColor Green }
else { Write-Warning '[start] no adb device detected. Connect + unlock a phone first.' }

# ---- live mirror (scrcpy) ----
if (-not $NoMirror -and (Get-Command scrcpy.exe -ErrorAction SilentlyContinue)) {
  $serials = (& adb devices | Select-String 'device$') -replace '\s*device$',''
  foreach ($s in $serials) {
    if ($s.Trim()) {
      Write-Host "[start] launch scrcpy for $s" -ForegroundColor Green
      Start-Process scrcpy.exe -ArgumentList @('--serial', $s.Trim(), '--window-title', "PhoneCtrl $s") -WindowStyle Normal
    }
  }
}

# ---- spawn detached dart ----
$env:PC_PORT = "$Port"
Remove-Item -Force $logFile, $pidFile -ErrorAction SilentlyContinue
$proc = Start-Process -FilePath $dartExe `
  -ArgumentList @('run', '--no-pause-isolates-on-exit', 'tools/control/server.dart') `
  -WorkingDirectory $Root `
  -RedirectStandardOutput $logFile `
  -RedirectStandardError "$logFile.err" `
  -WindowStyle Hidden -PassThru
Set-Content -Path $pidFile -Value $proc.Id -Encoding ASCII
Write-Host "[start] dart pid = $($proc.Id)   log = $logFile"

# ---- wait for health ----
$deadline = (Get-Date).AddSeconds(30); $up = $false
while ((Get-Date) -lt $deadline) {
  if (Test-PortOpen $Port) {
    try {
      $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/status" -UseBasicParsing -TimeoutSec 2
      if ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) { $up = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
  } else { Start-Sleep -Milliseconds 500 }
}
if (-not $up) {
  Write-Warning "[start] server did not answer within 30s. Tail of log:"
  if (Test-Path $logFile) { Get-Content $logFile -Tail 20 }
  throw '[start] abort.'
}
Write-Host "[start] server up at http://127.0.0.1:$Port/" -ForegroundColor Green

if (-not $NoBrowser) {
  try { Start-Process "http://127.0.0.1:$Port/" | Out-Null } catch { Write-Warning "[start] browser launch failed: $_" }
}
Write-Host "`nCommands:`n  stop : Get-Process dart | Where-Object {`$_.CommandLine -like '*control/server.dart*'} | Stop-Process -Force`n  tail : Get-Content $logFile -Wait`n" -ForegroundColor Cyan
