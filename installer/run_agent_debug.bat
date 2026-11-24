@echo off
title Employee Monitoring Agent - Live Debug Console
cd /d "%~dp0"

echo ==========================================================
echo   EMPLOYEE MONITORING AGENT - LIVE DEBUG MODE (SIGNED)
echo ==========================================================
echo.

"%~dp0runtime\python.exe" "%~dp0agent\main.py"

pause
