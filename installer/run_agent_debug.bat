@echo off
title Employee Monitoring Agent - Live Debug Console
cd /d "%~dp0"

echo ==========================================================
echo   EMPLOYEE MONITORING AGENT - LIVE DEBUG MODE
echo ==========================================================
echo.

set "SERVER_URL=https://amvion-employee-monitoring.onrender.com"
if exist "%~dp0server_url.txt" (
    set /p SERVER_URL=<"%~dp0server_url.txt"
)

echo Target Server : %SERVER_URL%
echo.
echo Stopping any background agent instance...
taskkill /F /IM EmployeeMonitoringAgent.exe >nul 2>&1
timeout /t 1 /nobreak >nul

echo Starting agent in foreground...
echo You will see enrollment and screenshot events below:
echo.

"%~dp0EmployeeMonitoringAgent.exe"

pause
