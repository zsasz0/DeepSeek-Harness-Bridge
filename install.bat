@echo off
chcp 65001 >nul
title Install DeepSeek + Gemini Web2API Dependencies

echo ===================================================
echo        DeepSeek + Gemini Web2API - Dependency Installer
echo ===================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in PATH!
    echo Please install Python 3.11+ and try again.
    pause
    exit /b 1
)

echo [*] Installing required packages...
python -m pip install -r requirements.txt

if errorlevel 1 (
    echo.
    echo [ERROR] Installation failed.
    pause
    exit /b 1
)

echo.
echo [✓] Installation complete!
echo You can now run start.bat to launch DeepSeek + Gemini Web2API.
echo.
pause
