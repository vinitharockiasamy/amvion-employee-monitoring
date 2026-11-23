@echo off
title Employee Monitoring System - Central Server
cd /d "%~dp0"
echo ==============================================================
echo   STARTING CENTRAL SERVER (FastAPI + Uvicorn)
echo ==============================================================
echo Dashboard URL: http://127.0.0.1:8000
echo.
python run_server.py
if %errorlevel% neq 0 (
    echo.
    echo Server exited with error code %errorlevel%.
    pause
)
