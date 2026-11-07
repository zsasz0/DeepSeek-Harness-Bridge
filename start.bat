@echo off
chcp 65001 >nul
title DeepSeek Web2API

:: Activate virtualenv if present
if exist "venv\Scripts\activate.bat" call venv\Scripts\activate.bat

:: Account pool lives in .\accounts (one token per file).
if not defined DEEPSEEK_AUTH_DIR if exist "%~dp0accounts" set "DEEPSEEK_AUTH_DIR=%~dp0accounts"

python main.py
if errorlevel 1 (
    echo.
    echo An error occurred. Press any key to exit.
    pause >nul
)
