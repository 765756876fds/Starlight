@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Starlight
set PYTHONIOENCODING=utf-8

REM --- 优先使用项目虚拟环境，缺失则回退系统 python ---
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  set "PY=python"
)

echo ============================================
echo   Starlight - starting (source mode)
echo ============================================
echo [BOOT] Python:
%PY% --version 2>&1
echo.

REM --- 依赖自检：缺库时明确报错，而不是静默退出 ---
%PY% -c "import yaml, requests, pydantic, PySide6; print('[BOOT] core deps OK')" 2>nul
if errorlevel 1 (
  echo.
  echo [BOOT][ERROR] Missing Python dependencies.
  echo Please run: %PY% -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

echo [BOOT] Qwen3-ASR endpoint check (127.0.0.1:8080)...
%PY% -c "import urllib.request,json; urllib.request.urlopen('http://127.0.0.1:8080/v1/models',timeout=2); print('[ASR] Qwen3-ASR: READY')" 2>nul
if errorlevel 1 (
  echo [ASR] Qwen3-ASR: NOT_READY - 应用会自动拉起 llama-server（模型缺失时提示准备），当前仅提示。
)
echo.

REM --- 清理残留 mpv：防止旧进程占用命名管道导致新实例无法连接 ---
taskkill /F /IM mpv.exe >nul 2>nul
%PY% main.py

echo.
echo [BOOT] main.py exited (code %errorlevel%). See error above if red.
pause
