@echo off
REM PhoneCtrl — Windows 桌面控制台 一键启动（双击即可）
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "%~dp0launch_console.ps1"
