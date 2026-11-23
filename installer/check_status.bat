@echo off
title Employee Monitoring Agent — Status Check

echo =====================================================================
echo    EMPLOYEE MONITORING AGENT — STATUS CHECK
echo =====================================================================
echo.

tasklist /FI "IMAGENAME eq EmployeeMonitoringAgent.exe" 2>NUL | find /I /N "EmployeeMonitoringAgent.exe">NUL
if "%ERRORLEVEL%"=="0" (
    echo [STATUS] EmployeeMonitoringAgent.exe is RUNNING in the background.
) else (
    echo [STATUS] EmployeeMonitoringAgent.exe is NOT currently running.
)

echo.
set "LOG_FILE=%LOCALAPPDATA%\Amvion\EmployeeMonitoringAgent\agent.log"
if exist "%LOG_FILE%" (
    echo --- Last 15 Log Entries (%LOG_FILE%) ---
    powershell -Command "Get-Content -Path '%LOG_FILE%' -Tail 15 -ErrorAction SilentlyContinue"
) else (
    echo (No log file generated yet)
)

echo.
pause
