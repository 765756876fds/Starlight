@echo off
chcp 65001 >nul
cd /d "%~dp0"
taskkill /F /IM mpv.exe >nul 2>nul
python main.py
pause
