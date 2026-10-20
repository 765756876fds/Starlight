@echo off
REM Starlight lite installer build script (ASCII only - safe on any codepage)
REM Prereq: run build.ps1 first to produce dist\Starlight
setlocal
cd /d "%~dp0"

if not exist "dist\Starlight\Starlight.exe" (
    echo [ERROR] dist\Starlight\Starlight.exe not found. Run build.ps1 first.
    exit /b 1
)

REM [1/2] Prepare bundled llama-server.exe (~30MB, copied from latest LM Studio backend).
REM       The 1.2G ASR model is NOT bundled - app/voice/asr_bootstrap.py downloads it
REM       from hf-mirror.com on first run.
if not exist "dist\Starlight\third_party\llama\llama-server.exe" (
    for /f "delims=" %%d in ('dir /b /ad "%USERPROFILE%\.lmstudio\extensions\backends\llama.cpp-win-*" 2^>nul ^| sort /r') do (
        if exist "%USERPROFILE%\.lmstudio\extensions\backends\%%d\llama-server.exe" (
            mkdir "dist\Starlight\third_party\llama" 2>nul
            copy /y "%USERPROFILE%\.lmstudio\extensions\backends\%%d\llama-server.exe" "dist\Starlight\third_party\llama\llama-server.exe" >nul
            echo [1/2] llama-server.exe copied from LM Studio backend %%d
            goto :copied
        )
    )
    echo [WARN] No LM Studio llama-server.exe found; lookup chain will fall back to PATH.
)
:copied

REM [2/2] Compile installer with Inno Setup
set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo [ERROR] Inno Setup not found. Download from https://jrsoftware.org/ and install.
    exit /b 1
)
echo [2/2] Compiling Starlight_Setup_Lite.exe ...
"%ISCC%" "setup_lite.iss"
if errorlevel 1 (
    echo [ERROR] Inno Setup compile failed.
    exit /b 1
)
echo DONE: Starlight_Setup_Lite.exe (lite package, ASR model auto-downloaded on first run)
