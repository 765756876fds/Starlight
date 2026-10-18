@echo off
REM LoL_Music_Agent FULL (offline) installer build script
REM Prereq: run build.ps1 first to produce dist\LoL_Music_Agent
REM Bundles local ASR models -> {app}\models, works fully offline (no download)
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "dist\LoL_Music_Agent\LoL_Music_Agent.exe" (
    echo [ERROR] dist\LoL_Music_Agent\LoL_Music_Agent.exe not found. Run build.ps1 first.
    exit /b 1
)

REM ASR model directory (override via: set MODELS_DIR=D:\path before running)
if "%MODELS_DIR%"=="" set "MODELS_DIR=D:\迅雷下载\ggml-org\Qwen3-ASR-0.6B-GGUF"
if not exist "%MODELS_DIR%\Qwen3-ASR-0.6B-Q8_0.gguf" (
    echo [ERROR] Model not found: %MODELS_DIR%\Qwen3-ASR-0.6B-Q8_0.gguf
    echo Set MODELS_DIR to the folder containing the ASR gguf files and re-run.
    exit /b 1
)
if not exist "%MODELS_DIR%\mmproj-Qwen3-ASR-0.6B-bf16.gguf" (
    echo [ERROR] Model not found: %MODELS_DIR%\mmproj-Qwen3-ASR-0.6B-bf16.gguf
    echo Set MODELS_DIR to the folder containing the ASR gguf files and re-run.
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

REM [2/2] Compile full installer (bundles ~1.13GB ASR models, works offline)
set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if not exist "%ISCC%" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo [ERROR] Inno Setup not found. Download from https://jrsoftware.org/ and install.
    exit /b 1
)
echo [2/2] Compiling Setup_Full.exe (models from %MODELS_DIR%) ...
"%ISCC%" /DModelsDir="%MODELS_DIR%" "setup_full.iss"
if errorlevel 1 (
    echo [ERROR] Inno Setup compile failed.
    exit /b 1
)
echo DONE: Setup_Full.exe (offline full package, ASR models bundled)
