@echo off
title Employee Monitoring System - Windows Agent
cd /d "%~dp0"
echo ==============================================================
echo   STARTING WINDOWS MONITORING AGENT
echo ==============================================================
echo Connecting to Central Server...
echo.
python agent\main.py
if %errorlevel% neq 0 (
    echo.
    echo Agent exited with error code %errorlevel%.
    pause
)
