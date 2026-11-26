# Windows Employee Live Monitoring — Live Screen Wall & Standalone Agent

An enterprise-grade desktop screen monitoring and situational awareness platform built with **FastAPI**, **WebSockets**, **SQLite**, and a **Signed Windows Installer (`.msi`) Agent**.

🌐 **Live 24/7 Cloud Admin Dashboard:** [https://em-productivity-system.onrender.com](https://em-productivity-system.onrender.com)  
⬇️ **Windows MSI Installer (`EMP-SA-enterprise-1.0.0.msi`):** [Download EMP-SA-enterprise-1.0.0.msi](https://github.com/vinitharockiasamy/employee-monitoring-system/raw/main/EMP-SA-enterprise-1.0.0.msi)

---

## Core Capabilities

1. **Periodic Screenshot Mode (3-Second Refresh)**:
   - Continuous, low-overhead desktop captures every **3 seconds** feeding a responsive **Live Screen Wall** grid.
   - Displays real-time workstation tiles with Employee Name, Computer Name, OS version, Online/Offline presence badge, and relative timestamps.
2. **On-Demand Live Stream Mode (15 FPS)**:
   - Clicking any employee screen tile on the Admin Dashboard immediately signals that specific endpoint over WebSockets (`START_STREAM`) to switch into **15 FPS low-latency video streaming**.
   - Closing the detail modal (`Esc` or Close button) immediately signals the agent (`STOP_STREAM`) to revert to 3-second periodic screenshot mode.
3. **Signed Windows Installer Package (`EMP-SA-enterprise-1.0.0.msi`)**:
   - Native Microsoft Windows Installer (`.msi`) package with publisher signature and Microsoft-verified Python Software Foundation portable runtime (`pythonw.exe`).
   - Target laptops do not need Python pre-installed.
   - Automatically installs into `%LOCALAPPDATA%\EmployeeMonitoring\Agent`, registers silent Windows boot auto-start (`HKCU\...\Run`), and launches silently in the background.
4. **24/7 Cloud Deployment (GitHub + Render Docker)**:
   - Ready for 24/7 cloud deployment via `Dockerfile` and `render.yaml`, allowing administrators to monitor endpoints and download the `.msi` agent from any device or network.

---

## Repository Structure

```text
employee-monitoring-system/
├── agent/                                  # Windows Client Agent Source
│   ├── capture.py                          # MSS screen capture + DPI awareness & Pillow fallback
│   ├── compressor.py                       # Pillow adaptive JPEG compression (70Q wall / 55Q stream)
│   ├── streamer.py                         # 15 FPS low-latency frame streamer
│   ├── client.py                           # HTTP enrollment/upload + persistent WebSocket client
│   ├── config.py                           # Hardware UUID (MachineGuid) & server URL management
│   └── main.py                             # Zone.Identifier unblocker & daemon loop
├── installer/                              # Standalone Windows Distribution Scripts
│   ├── install_silent.vbs                  # Silent VBScript installer (zero CMD windows)
│   ├── install.bat                         # Batch installer with Task Scheduler & Registry setup
│   ├── uninstall.bat                       # 1-Click complete uninstaller
│   ├── check_status.bat                    # Diagnostic status & log viewer
│   └── run_agent_debug.bat                 # Foreground console mode for live debugging
├── server/                                 # FastAPI Central Server & Admin Dashboard
│   ├── app/
│   │   ├── api/
│   │   │   ├── auth.py                     # Deterministic device enrollment & secret issuance
│   │   │   ├── agent.py                    # 3s screenshot upload & heartbeat ingestion
│   │   │   ├── admin.py                    # Device queries, latest frame & MSI installer downloads
│   │   │   └── websocket.py                # Real-time dashboard broadcaster & stream signaling
│   │   ├── core/
│   │   │   ├── config.py                   # Server settings (3s default capture interval)
│   │   │   └── security.py                 # SHA-256 secret hashing & constant-time verification
│   │   ├── db/
│   │   │   ├── session.py                  # SQLAlchemy SQLite engine & session management
│   │   │   └── models.py                   # Device, Screenshot, and StreamSession models
│   │   └── main.py                         # FastAPI application & presence watchdog (20s timeout)
│   ├── static/
│   │   ├── css/style.css                   # Live indicators, 16:9 tiles, and modal styling
│   │   └── js/dashboard.js                 # Real-time WebSocket screen wall & 15 FPS stream player
│   └── templates/
│       └── index.html                      # Admin Dashboard UI with 1-click MSI agent download
├── EMP-SA-enterprise-1.0.0.msi             # Native Signed Windows Installer Package
├── Dockerfile                              # Cloud container build definition
├── render.yaml                             # Render.com 24/7 Infrastructure-as-Code blueprint
├── requirements.txt                        # Python backend dependencies
├── run_server.py                           # Production server entry point
└── README.md
```

---

## Installing & Uninstalling the Agent

### Install (1 Click)
Double-click **`EMP-SA-enterprise-1.0.0.msi`** (or run `msiexec /i EMP-SA-enterprise-1.0.0.msi /qn` for silent deployment).

### Uninstall (1 Click)
Right-click **`EMP-SA-enterprise-1.0.0.msi`** and select **Uninstall**, or run in Command Prompt (`cmd`):
```bat
taskkill /F /IM pythonw.exe >nul 2>&1 & reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "EmployeeMonitoringAgent" /f >nul 2>&1 & schtasks /Delete /TN "EmployeeMonitoringAgent" /F >nul 2>&1 & rmdir /S /Q "%LOCALAPPDATA%\EmployeeMonitoring" >nul 2>&1
```
