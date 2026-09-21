@echo off
chcp 65001 >nul
cd /d "%~dp0"
title LoL Music Agent
set "PYTHONPATH=C:\Users\1\AppData\Roaming\Python\Python311\site-packages;%PYTHONPATH%"
set PYTHONIOENCODING=utf-8

echo ============================================
echo   LoL Music Agent - starting
echo ============================================
echo [BOOT] Python:
python --version 2>&1
where python 2>&1
echo.

REM --- 依赖自检：缺库时明确报错，而不是静默退出 ---
python -c "import yaml, requests, pydantic; print('[BOOT] core deps OK')" 2>nul
if errorlevel 1 (
  echo.
  echo [BOOT][ERROR] Missing Python dependencies.
  echo Please run: python -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo [BOOT] Qwen3-ASR endpoint check (127.0.0.1:8080)...
python -c "import urllib.request,json; urllib.request.urlopen('http://127.0.0.1:8080/v1/models',timeout=2); print('[ASR] Qwen3-ASR: READY')" 2>nul
if errorlevel 1 (
  echo [ASR] Qwen3-ASR: NOT_READY - start llama-server first, agent will retry.
)
echo.

taskkill /F /IM mpv.exe >nul 2>nul
python main.py

echo.
echo [BOOT] main.py exited (code %errorlevel%). See error above if red.
pause
