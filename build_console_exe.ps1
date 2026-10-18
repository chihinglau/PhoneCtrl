# 将桌面控制台编译为可部署到其它电脑的 exe（含原生后端，目标机无需 Python / Dart SDK）
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File build_console_exe.ps1
#
# 产物：dist\PhoneCtrlConsole\  （把整个文件夹拷到其它 Windows 电脑即可运行）
$ErrorActionPreference = 'Continue'
$root = Resolve-Path (Join-Path $PSScriptRoot '.')
Set-Location $root

# 1) 编译 Dart 后端为原生 exe（目标机免装 Dart SDK）；已存在则跳过以提速
$serverExe = Join-Path $root ".tools\phonectrl_server.exe"
if (-not (Test-Path $serverExe)) {
    Write-Host ">> 编译 Dart 后端 (dart compile exe) ..." -ForegroundColor Cyan
    dart compile exe tools/control/server.dart -o $serverExe
} else {
    Write-Host ">> 已存在 phonectrl_server.exe，跳过编译" -ForegroundColor Cyan
}

# 2) PyInstaller 打包控制台（--onedir：exe 与 .tools 同目录，开箱即用）
#    注意：--add-data 的分隔符是 ';'，PowerShell 会把它当成语句分隔符，
#    因此用 cmd /c 调用以保证参数原样传给 pyinstaller。
Write-Host ">> PyInstaller 打包桌面控制台 ..." -ForegroundColor Cyan
$pyi = "pyinstaller --noconfirm --clean --noconsole --onedir --name PhoneCtrlConsole --add-data .tools;.tools --add-data run_server.ps1;. --add-data scripts;scripts win_console/app.py"
cmd /c "$pyi"

Write-Host ">> 完成。部署目录: $root\dist\PhoneCtrlConsole" -ForegroundColor Green
Write-Host "   将该文件夹整体拷贝到其它 Windows 电脑，双击 PhoneCtrlConsole.exe 即可。" -ForegroundColor Green
