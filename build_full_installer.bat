@echo off
REM LoL_Music_Agent FULL (offline) installer build script (ASCII only - safe on any codepage)
REM Prereq: run build.ps1 first to produce dist\LoL_Music_Agent
REM Bundles local ASR models -> {app}\models, works fully offline (no download)
REM NOTE: the ASR model folder is defined in setup_full.iss via #define ModelsDir
REM       (change it there if your models live elsewhere; cmd cannot carry the
REM       unicode path safely, ISCC reads the .iss directly).
setlocal
cd /d "%~dp0"

if not exist "dist\LoL_Music_Agent\LoL_Music_Agent.exe" (
    echo [ERROR] dist\LoL_Music_Agent\LoL_Music_Agent.exe not found. Run build.ps1 first.
    exit /b 1
)

REM [1/2] Prepare bundled llama-server.exe (~30MB, copied from latest LM Studio backend)
if not exist "dist\LoL_Music_Agent\third_party\llama\llama-server.exe" (
    for /f "delims=" %%d in ('dir /b /ad "%USERPROFILE%\.lmstudio\extensions\backends\llama.cpp-win-*" 2^>nul ^| sort /r') do (
        if exist "%USERPROFILE%\.lmstudio\extensions\backends\%%d\llama-server.exe" (
            mkdir "dist\LoL_Music_Agent\third_party\llama" 2>nul
            copy /y "%USERPROFILE%\.lmstudio\extensions\backends\%%d\llama-server.exe" "dist\LoL_Music_Agent\third_party\llama\llama-server.exe" >nul
            echo [1/2] llama-server.exe copied from LM Studio backend %%d
            goto :copied
        )
    )
    echo [WARN] No LM Studio llama-server.exe found; lookup chain will fall back to PATH.
)
:copied

REM [2/2] Compile full installer. ModelsDir comes from setup_full.iss (#define),
REM       so ISCC handles the unicode path (cmd cannot carry non-ASCII safely).
set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo [ERROR] Inno Setup not found. Download from https://jrsoftware.org/ and install.
    exit /b 1
)
echo [2/2] Compiling Setup_Full.exe (models bundled per setup_full.iss ModelsDir) ...
"%ISCC%" "setup_full.iss"
if errorlevel 1 (
    echo [ERROR] Inno Setup compile failed.
    exit /b 1
)
echo DONE: Setup_Full.exe (offline full package, ASR models bundled)
