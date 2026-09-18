@echo off
chcp 65001 >nul
powershell -ExecutionPolicy Bypass -File "%~dp0tools\create_desktop_shortcut.ps1"
pause
