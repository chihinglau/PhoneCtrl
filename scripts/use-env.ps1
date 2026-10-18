#requires -Version 5.1
<#
  PhoneCtrl environment injection (mirrors MyGameSim scripts/use-env.ps1).
  Resolves adb / scrcpy from a local .tools dir or ANDROID_HOME / PATH.

  Usage (every new terminal):
    . .\scripts\use-env.ps1
#>
[CmdletBinding()]
param(
  [string]$Root
)
# 注意：被 dot-source 时 $PSScriptRoot 指向“调用者”目录而非本文件目录，
# 因此必须用 $MyInvocation.MyCommand.Path 取本脚本真实位置来推算根目录。
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Root) { $Root = (Resolve-Path (Join-Path $ScriptDir '..')).Path }
$ErrorActionPreference = 'Stop'
$Tools = Join-Path $Root '.tools'

# adb: prefer ANDROID_HOME, then .tools/android-sdk, else global adb
if ($env:ANDROID_HOME) {
  $adb = Join-Path $env:ANDROID_HOME 'platform-tools\adb.exe'
  if (Test-Path $adb) { $env:Path = "$($env:ANDROID_HOME)\platform-tools;$env:Path" }
} elseif (Test-Path (Join-Path $Tools 'android-sdk\platform-tools\adb.exe')) {
  $env:ANDROID_HOME = Join-Path $Tools 'android-sdk'
  $env:Path = "$($env:ANDROID_HOME)\platform-tools;$env:Path"
}

# scrcpy (optional, for live mirroring)
$scr = Join-Path $Tools 'scrcpy\scrcpy.exe'
if (Test-Path $scr) { $env:Path = "$(Join-Path $Tools 'scrcpy');$env:Path" }

# dart (for running the PC controller)
$dart = Join-Path $Tools 'flutter\bin\cache\dart-sdk\bin\dart.exe'
if (Test-Path $dart) { $env:Path = "$(Split-Path $dart);$env:Path" }

Write-Host "[env] ANDROID_HOME = $env:ANDROID_HOME" -ForegroundColor Cyan
if (Get-Command adb.exe -ErrorAction SilentlyContinue) {
  & adb.exe version 2>$null | Select-Object -First 1
}
if (Get-Command scrcpy.exe -ErrorAction SilentlyContinue) {
  Write-Host '[env] scrcpy ready (live mirroring enabled).' -ForegroundColor Green
} else {
  Write-Warning '[env] scrcpy not found; live mirror disabled (screencap analysis still works).'
}
