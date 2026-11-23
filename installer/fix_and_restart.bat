@echo off
title Reconnect Employee Monitoring Agent
cd /d "%~dp0"

set "INSTALL_DIR=%LOCALAPPDATA%\Amvion\EmployeeMonitoringAgent"
set "SERVER_URL=https://amvion-employee-monitoring.onrender.com"

echo ==============================================================
echo   RECONNECTING AGENT TO: %SERVER_URL%
echo ==============================================================
echo.

echo [1/4] Stopping any existing agent process...
taskkill /F /IM EmployeeMonitoringAgent.exe >nul 2>&1
timeout /t 1 /nobreak >nul

echo [2/4] Updating server URL to %SERVER_URL%...
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
echo %SERVER_URL%> "%INSTALL_DIR%\server_url.txt"

echo [3/4] Resetting previous connection state...
del /F /Q "%INSTALL_DIR%\agent_state.json" >nul 2>&1

echo [4/4] Starting EmployeeMonitoringAgent.exe...
if exist "%INSTALL_DIR%\EmployeeMonitoringAgent.exe" (
    start "" "%INSTALL_DIR%\EmployeeMonitoringAgent.exe"
) else (
    copy /Y "%~dp0EmployeeMonitoringAgent.exe" "%INSTALL_DIR%\EmployeeMonitoringAgent.exe" >nul 2>&1
    start "" "%INSTALL_DIR%\EmployeeMonitoringAgent.exe"
)

echo.
echo ==============================================================
echo   [SUCCESS] Connected! Check the Admin Dashboard now.
echo ==============================================================
echo.
timeout /t 3 >nul
