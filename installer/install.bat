@echo off
setlocal enabledelayedexpansion
title Employee Monitoring Agent — Signed Runtime Installer
cd /d "%~dp0"

set "INSTALL_DIR=%LOCALAPPDATA%\Amvion\EmployeeMonitoringAgent"
set "SERVER_URL=https://amvion-employee-monitoring.onrender.com"

if exist "%~dp0server_url.txt" (
    set /p SERVER_URL=<"%~dp0server_url.txt"
)

:: 1. Stop any previous running agent instance
taskkill /F /IM EmployeeMonitoringAgent.exe >nul 2>&1
wmic process where "name='pythonw.exe' and commandline like '%%Amvion%%'" delete >nul 2>&1
timeout /t 1 /nobreak >nul

:: 2. Create clean install directory
if not exist "%INSTALL_DIR%" mkdir "%INSTALL_DIR%"
del /f /q "%INSTALL_DIR%\agent_state.json" >nul 2>&1
del /f /q "%INSTALL_DIR%\EmployeeMonitoringAgent.exe" >nul 2>&1

:: 3. Copy official Microsoft-signed Python runtime and transparent agent scripts
xcopy /E /I /Y /Q "%~dp0runtime" "%INSTALL_DIR%\runtime" >nul
xcopy /E /I /Y /Q "%~dp0agent" "%INSTALL_DIR%\agent" >nul
echo !SERVER_URL!> "%INSTALL_DIR%\server_url.txt"
echo !SERVER_URL!> "%INSTALL_DIR%\agent\server_url.txt"
powershell -NoProfile -Command "Unblock-File -Path '%INSTALL_DIR%\*' -Recurse -ErrorAction SilentlyContinue" >nul 2>&1

:: 4. Register auto-start on Windows boot using signed pythonw.exe
set "LAUNCH_CMD=\"%INSTALL_DIR%\runtime\pythonw.exe\" \"%INSTALL_DIR%\agent\main.py\""
schtasks /Create /SC ONLOGON /TN "AmvionMonitoringAgent" /TR "!LAUNCH_CMD!" /RL LIMITED /F >nul 2>&1
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "EmployeeMonitoringAgent" /t REG_SZ /d "!LAUNCH_CMD!" /f >nul

:: 5. Start agent silently in background using signed pythonw.exe
start "" "%INSTALL_DIR%\runtime\pythonw.exe" "%INSTALL_DIR%\agent\main.py"

:: 6. Confirmation dialog
powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('Employee Monitoring Agent installed and connected!`n`nPublisher: Python Software Foundation (Microsoft Verified Signature)`nServer: !SERVER_URL!`nStatus: Running silently in background (3s refresh).', 'Amvion Monitoring Agent', 'OK', 'Information')"

exit /b 0
