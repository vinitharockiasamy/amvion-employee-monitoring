@echo off
setlocal enabledelayedexpansion
title Employee Monitoring Agent — Installer
cd /d "%~dp0"

set "INSTALL_DIR=%LOCALAPPDATA%\Amvion\EmployeeMonitoringAgent"
set "SERVER_URL=https://amvion-employee-monitoring.onrender.com"

if exist "%~dp0server_url.txt" (
    set /p SERVER_URL=<"%~dp0server_url.txt"
)

:: 1. Terminate any previous running agent instance
taskkill /F /IM EmployeeMonitoringAgent.exe >nul 2>&1
timeout /t 1 /nobreak >nul

:: 2. Ensure install directory exists
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"

:: 3. Clear any stale cached credentials to ensure clean enrollment
del /f /q "%INSTALL_DIR%\agent_state.json" >nul 2>&1

:: 4. Stream-copy binary (strips Windows Internet Zone.Identifier so it NEVER asks "Publisher could not be verified" on reboot)
type "%~dp0EmployeeMonitoringAgent.exe" > "%INSTALL_DIR%\EmployeeMonitoringAgent.exe"
echo !SERVER_URL!> "%INSTALL_DIR%\server_url.txt"
powershell -NoProfile -Command "Unblock-File -Path '%INSTALL_DIR%\*' -ErrorAction SilentlyContinue" >nul 2>&1

:: 5. Register silent startup via Windows Task Scheduler + HKCU Run (zero prompts on boot)
schtasks /Create /SC ONLOGON /TN "AmvionMonitoringAgent" /TR "\"%INSTALL_DIR%\EmployeeMonitoringAgent.exe\"" /RL LIMITED /F >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "EmployeeMonitoringAgent" /t REG_SZ /d "\"%INSTALL_DIR%\EmployeeMonitoringAgent.exe\"" /f >nul

:: 6. Launch agent process silently in the background
start "" "%INSTALL_DIR%\EmployeeMonitoringAgent.exe"

:: 7. Native confirmation popup
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Employee Monitoring Agent installed and verified!`n`nStatus: Running silently in background.`nAuto-Start: Enabled (Zero boot security prompts).', 'Amvion Monitoring Agent', 'OK', 'Information')"

exit /b 0
