New-NetFirewallRule -DisplayName "Employee Monitoring Server" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -Force
Write-Host "Port 8000 successfully opened in Windows Firewall!"
Start-Sleep -Seconds 2
