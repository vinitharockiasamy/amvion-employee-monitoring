@echo off
title Employee Monitoring Agent — Uninstaller

echo =====================================================================
echo    EMPLOYEE MONITORING SYSTEM — UNINSTALL AGENT
echo =====================================================================
echo.

set "INSTALL_DIR=%LOCALAPPDATA%\Amvion\EmployeeMonitoringAgent"

echo [1/4] Terminating background agent process...
taskkill /F /IM EmployeeMonitoringAgent.exe >nul 2>&1

echo [2/4] Removing Windows Task Scheduler auto-start entry...
schtasks /Delete /TN "AmvionMonitoringAgent" /F >nul 2>&1

echo [3/4] Removing Windows Registry Run entry...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "EmployeeMonitoringAgent" /f >nul 2>&1

echo [4/4] Removing installed files...
if exist "%INSTALL_DIR%" (
    rmdir /S /Q "%INSTALL_DIR%" >nul 2>&1
)

echo.
echo =====================================================================
echo   [SUCCESS] AGENT HAS BEEN COMPLETELY REMOVED FROM THIS SYSTEM.
echo =====================================================================
echo.
pause
