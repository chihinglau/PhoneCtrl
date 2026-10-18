#requires -Version 5.1
<#
  PhoneCtrl — Windows 桌面控制台 一键启动
  1) 注入环境(adb / scrcpy / dart)
  2) 启动 PyQt5 桌面控制台(自动探测并拉起 Dart 后端 8858)
#>
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$Root = Resolve-Path (Join-Path $PSScriptRoot '.')
$useEnv = Join-Path $Root 'scripts\use-env.ps1'
if (Test-Path $useEnv) { . $useEnv -Root $Root | Out-Null }

Set-Location $Root

# 简易 Python 可用性自检
if (-not (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 python.exe，请先安装 Python 3.10+ 并加入 PATH。"
    Read-Host "按回车退出"
    exit 1
}

Write-Host "[launch] 启动桌面控制台 win_console\app.py ..." -ForegroundColor Green
& python.exe (Join-Path $Root 'win_console\app.py')
