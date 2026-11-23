@echo off
title Open Firewall for Employee Monitoring Server
cd /d "%~dp0"

:: Check for administrative rights
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges to open port 8000 in Windows Firewall...
    powershell -Command "Start-Process cmd -ArgumentList '/c `"%~f0`"' -Verb RunAs"
    exit /b
)

echo ==============================================================
echo   ALLOWING PORT 8000 IN WINDOWS DEFENDER FIREWALL
echo ==============================================================
echo.
netsh advfirewall firewall delete rule name="Employee Monitoring Server (Port 8000)" >nul 2>&1
netsh advfirewall firewall add rule name="Employee Monitoring Server (Port 8000)" dir=in action=allow protocol=TCP localport=8000
echo.
if %errorlevel% equ 0 (
    echo [SUCCESS] Port 8000 is now open!
    echo Client laptops on your Wi-Fi can now connect to http://192.168.0.103:8000.
) else (
    echo [ERROR] Failed to add firewall rule.
)
echo.
pause
