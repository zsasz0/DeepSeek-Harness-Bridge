@echo off
chcp 65001 >nul
title DeepSeek Web2API

:: Activate virtualenv if present
if exist "venv\Scripts\activate.bat" call venv\Scripts\activate.bat

python main.py
if errorlevel 1 (
    echo.
    echo An error occurred. Press any key to exit.
    pause >nul
)
