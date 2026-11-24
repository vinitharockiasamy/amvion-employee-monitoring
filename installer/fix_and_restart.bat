@echo off
title Reconnect Employee Monitoring Agent
cd /d "%~dp0"

set "INSTALL_DIR=%LOCALAPPDATA%\Amvion\EmployeeMonitoringAgent"
set "SERVER_URL=https://amvion-employee-monitoring.onrender.com"

echo ==============================================================
echo   RECONNECTING AGENT TO: %SERVER_URL%
echo ==============================================================
echo.

call "%~dp0install.bat"
