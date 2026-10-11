@echo off
chcp 65001 >nul
cd /d "%~dp0\.."
py -3.11 tools\voice_debug.py
pause
