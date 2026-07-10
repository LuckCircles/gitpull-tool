@echo off
chcp 65001 > nul
title Python Checkup

echo ==============================
echo Activating virtual environment
echo ==============================

call .venv\Scripts\activate

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)

echo.
echo ==============================
echo Running python-checkup
echo ==============================

python-checkup . --verbose

echo.
echo ==============================
echo Checkup finished
echo ==============================

pause