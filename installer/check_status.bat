@echo off
title Employee Monitoring Agent — Status Check

echo =====================================================================
echo    EMPLOYEE MONITORING AGENT — STATUS CHECK (SIGNED RUNTIME)
echo =====================================================================
echo.

powershell -NoProfile -Command "if (Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*Amvion*main.py*' }) { Write-Host '[STATUS] Agent is RUNNING in the background (Microsoft-signed pythonw.exe).' } else { Write-Host '[STATUS] Agent is NOT currently running.' }"

echo.
set "LOG_FILE=%LOCALAPPDATA%\Amvion\EmployeeMonitoringAgent\agent\agent.log"
if exist "%LOG_FILE%" (
    echo --- Last 15 Log Entries (%LOG_FILE%) ---
    powershell -NoProfile -Command "Get-Content -Path '%LOG_FILE%' -Tail 15 -ErrorAction SilentlyContinue"
) else (
    echo (No log file generated yet)
)

echo.
pause
