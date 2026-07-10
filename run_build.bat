@echo off
chcp 65001 > nul

cd /d %~dp0

title Start Python Project

echo ==============================
echo Starting application
echo ==============================

uv run build.py

echo.
echo ==============================
echo Application exited
echo ==============================

pause