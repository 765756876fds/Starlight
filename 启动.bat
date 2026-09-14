@echo off
chcp 65001 >nul
title LoL Music Agent

echo ============================================================
echo   LoL x 本地音乐 x 语音 Agent
echo ============================================================
echo.

REM 设置 Python 路径
set PYTHON=C:\Program Files\Python311\python.exe
set PYTHONPATH=C:\Users\1\AppData\Roaming\Python\Python311\site-packages

REM 切换到项目目录
cd /d "%~dp0"

REM 检查 Python
if not exist "%PYTHON%" (
    echo [错误] 未找到 Python 3.11: %PYTHON%
    pause
    exit /b 1
)

REM 启动应用
echo 正在启动...
echo.
"%PYTHON%" main.py

echo.
echo 程序已退出。
pause
