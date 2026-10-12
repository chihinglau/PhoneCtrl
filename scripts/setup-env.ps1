#requires -Version 5.1
<#
  PhoneCtrl dev-environment setup (zero system pollution, everything under .tools).
  Downloads adb (platform-tools) and scrcpy if missing. Reuses an existing
  shared toolchain on the same drive via junctions (default ..\MyLangLean\.tools).

  Usage:
    powershell -ExecutionPolicy Bypass -File scripts\setup-env.ps1
    powershell -ExecutionPolicy Bypass -File scripts\setup-env.ps1 -SharedTools D:\path\.tools
#>
[CmdletBinding()]
param(
  [string]$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path,
  [string]$SharedTools = '',
  [switch]$NoShared
)
$ErrorActionPreference = 'Stop'
$Tools = Join-Path $Root '.tools'
$Dl    = Join-Path $Tools '.downloads'
New-Item -ItemType Directory -Force $Tools, $Dl | Out-Null

function Write-Step($m) { Write-Host "`n========== $m ==========" -ForegroundColor Cyan }
function Get-RemoteFile($url, $dest) {
  if (Test-Path $dest) { Write-Host "exists: $(Split-Path $dest -Leaf)"; return }
  for ($i = 1; $i -le 3; $i++) {
    try { Write-Host "[$i/3] $url"; Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing -TimeoutSec 1800; return }
    catch { Write-Warning $_.Exception.Message; Start-Sleep -Seconds 3 }
  }
  throw "download failed: $url"
}
function Link-Or-Reuse($name, $target) {
  $dst = Join-Path $Tools $name
  if (Test-Path $dst) { Write-Host "exists: .tools\$name"; return $true }
  $src = Join-Path $target $name
  if (Test-Path $src) { New-Item -ItemType Junction -Path $dst -Target $src | Out-Null; Write-Host "junction: .tools\$name -> $src" -ForegroundColor Green; return $true }
  return $false
}

# ---------- 0. reuse shared toolchain ----------
if (-not $SharedTools -and -not $NoShared) {
  $cand = Join-Path (Split-Path $Root -Parent) 'MyLangLean\.tools'
  if (Test-Path $cand) { $SharedTools = $cand }
}
if ($SharedTools) {
  if (-not (Test-Path $SharedTools)) { throw "SharedTools not found: $SharedTools" }
  Write-Step "Reuse shared toolchain: $SharedTools"
  foreach ($n in 'android-sdk', 'flutter', 'scrcpy', 'caches') {
    [void](Link-Or-Reuse $n $SharedTools)
  }
}

# ---------- 1. Android platform-tools (adb) ----------
$sdk = Join-Path $Tools 'android-sdk'
if (-not (Test-Path (Join-Path $sdk 'platform-tools\adb.exe'))) {
  Write-Step 'Android platform-tools (adb)'
  $name = 'platform-tools-latest-windows.zip'
  $zip  = Join-Path $Dl $name
  $ok = $false
  foreach ($u in @("https://dl.google.com/android/repository/$name",
                   "https://mirrors.cloud.tencent.com/AndroidSDK/$name")) {
    try { Get-RemoteFile $u $zip; $ok = $true; break } catch { Remove-Item $zip -Force -ErrorAction SilentlyContinue }
  }
  if (-not $ok) { throw 'platform-tools download failed on all mirrors' }
  $tmp = Join-Path $Dl ('pt-' + [guid]::NewGuid().ToString('N'))
  Expand-Archive -Path $zip -DestinationPath $tmp -Force
  New-Item -ItemType Directory -Force $sdk | Out-Null
  Move-Item (Join-Path $tmp 'platform-tools') (Join-Path $sdk 'platform-tools')
  Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
$env:ANDROID_HOME = $sdk
$env:ANDROID_SDK_ROOT = $sdk
$env:Path = "$(Join-Path $sdk 'platform-tools');$env:Path"

# ---------- 2. scrcpy (live mirroring, optional) ----------
$scr = Join-Path $Tools 'scrcpy'
if (-not (Test-Path (Join-Path $scr 'scrcpy.exe'))) {
  Write-Step 'scrcpy (live mirroring)'
  $name = 'scrcpy-win64-v2.7.zip'
  $zip  = Join-Path $Dl $name
  $ok = $false
  foreach ($u in @("https://github.com/Genymobile/scrcpy/releases/download/v2.7/$name")) {
    try { Get-RemoteFile $u $zip; $ok = $true; break } catch { Remove-Item $zip -Force -ErrorAction SilentlyContinue }
  }
  if ($ok) {
    $tmp = Join-Path $Dl ('scr-' + [guid]::NewGuid().ToString('N'))
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    Move-Item (Join-Path $tmp 'scrcpy-win64-v2.7') $scr
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host '[setup] scrcpy installed.' -ForegroundColor Green
  } else {
    Write-Warning '[setup] scrcpy download failed; live mirror disabled (screencap still works).'
  }
}

# ---------- 3. verify ----------
Write-Step 'Verify'
& adb version 2>$null | Select-Object -First 1
if (Get-Command scrcpy.exe -ErrorAction SilentlyContinue) { Write-Host '[setup] scrcpy ready.' -ForegroundColor Green }
Write-Host "`n[DONE] In every new terminal run first: . .\scripts\use-env.ps1" -ForegroundColor Green
