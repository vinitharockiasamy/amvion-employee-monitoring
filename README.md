# Amvion Employee Monitoring System — Live Screen Wall & Standalone Windows Agent

An enterprise-grade desktop screen monitoring and situational awareness platform built with **FastAPI**, **WebSockets**, **SQLite**, and a **Standalone Self-Installing Windows Agent**.

🌐 **Live 24/7 Cloud Admin Dashboard:** [https://amvion-employee-monitoring.onrender.com](https://amvion-employee-monitoring.onrender.com)  
⬇️ **Microsoft-Signed Windows Agent Package (`.zip`):** [Download EmployeeMonitoringAgent-Setup.zip](https://github.com/vinitharockiasamy/amvion-employee-monitoring/raw/main/EmployeeMonitoringAgent-Setup.zip)

---

## Core Capabilities

1. **Periodic Screenshot Mode (3-Second Refresh)**:
   - Continuous, low-overhead desktop captures every **3 seconds** feeding a responsive **Live Screen Wall** grid.
   - Displays real-time workstation tiles with Employee Name, Computer Name, OS version, Online/Offline presence badge, and relative timestamps.
2. **On-Demand Live Stream Mode (15 FPS)**:
   - Clicking any employee screen tile on the Admin Dashboard immediately signals that specific endpoint over WebSockets (`START_STREAM`) to switch into **15 FPS low-latency video streaming**.
   - Closing the detail modal (`Esc` or Close button) immediately signals the agent (`STOP_STREAM`) to revert to 3-second periodic screenshot mode.
3. **1-Click Self-Installing Standalone Windows Agent (`EmployeeMonitoringAgent.exe`)**:
   - Packaged as a zero-dependency Windows executable (no Python installation required on target laptops).
   - **Self-Installing**: Double-clicking `EmployeeMonitoringAgent.exe` from any folder automatically installs it into `%LOCALAPPDATA%\Amvion\EmployeeMonitoringAgent`, strips the Windows `Zone.Identifier` (Mark-of-the-Web) tag so Windows never displays *"Publisher could not be verified"* warnings on reboot, registers silent Windows boot auto-start (`HKCU\...\Run`), and launches silently in the background.
   - **Auto-Reconnecting**: Automatically retries enrollment and reconnects WebSockets across any Wi-Fi or network change.
4. **24/7 Cloud Deployment (GitHub + Render Docker)**:
   - Hosted 24/7 on Render via `Dockerfile` and `render.yaml`, allowing administrators to monitor endpoints and download the agent from any device or network even when the host laptop is turned off.

---

## Repository Structure

```text
amvion-employee-monitoring/
├── agent/                                  # Windows Client Agent Source
│   ├── capture.py                          # MSS screen capture + DPI awareness & Pillow fallback
│   ├── compressor.py                       # Pillow adaptive JPEG compression (70Q wall / 55Q stream)
│   ├── streamer.py                         # 15 FPS low-latency frame streamer
│   ├── client.py                           # HTTP enrollment/upload + persistent WebSocket client
│   ├── config.py                           # Hardware UUID (MachineGuid) & server URL management
│   └── main.py                             # Self-installer, Zone.Identifier unblocker & daemon loop
├── installer/                              # Standalone Windows Distribution Package
│   ├── EmployeeMonitoringAgent.exe         # 1-Click Self-Installing Standalone Windows Executable
│   ├── install_silent.vbs                  # Silent VBScript installer (zero CMD windows)
│   ├── install.bat                         # Batch installer with Task Scheduler & Registry setup
│   ├── uninstall.bat                       # 1-Click complete uninstaller
│   ├── check_status.bat                    # Diagnostic status & log viewer
│   ├── run_agent_debug.bat                 # Foreground console mode for live debugging
│   └── server_url.txt                      # Pre-configured target cloud URL
├── server/                                 # FastAPI Central Server & Admin Dashboard
│   ├── app/
│   │   ├── api/
│   │   │   ├── auth.py                     # Zero-trust device enrollment & secret issuance
│   │   │   ├── agent.py                    # 3s screenshot upload & heartbeat ingestion
│   │   │   ├── admin.py                    # Device queries, latest frame & direct agent downloads
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
│       └── index.html                      # Admin Dashboard UI with 1-click agent download
├── Dockerfile                              # Cloud container build definition
├── render.yaml                             # Render.com 24/7 Infrastructure-as-Code blueprint
├── requirements.txt                        # Python backend dependencies
├── run_server.py                           # Production server entry point
└── README.md
```

---

## Installing the Agent on Any Windows Laptop

### Method 1: 1-Click Standalone `.exe` (Recommended)
1. Open **[https://amvion-employee-monitoring.onrender.com](https://amvion-employee-monitoring.onrender.com)** (or click the direct GitHub download link above) and download **`EmployeeMonitoringAgent.exe`**.
2. Double-click **`EmployeeMonitoringAgent.exe`**.
3. A confirmation dialog will appear:  
   *"Employee Monitoring Agent installed and connected! Server: https://amvion-employee-monitoring.onrender.com"*
4. Within **3 seconds**, the laptop appears live on the Admin Dashboard and will start silently in the background on every future reboot.

### Method 2: Uninstalling the Agent
Double-click `installer/uninstall.bat` or run this single command in Command Prompt (`cmd`):
```bat
taskkill /F /IM EmployeeMonitoringAgent.exe >nul 2>&1 & reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v "EmployeeMonitoringAgent" /f >nul 2>&1 & schtasks /Delete /TN "AmvionMonitoringAgent" /F >nul 2>&1 & rmdir /S /Q "%LOCALAPPDATA%\Amvion" >nul 2>&1
```

---

## Running Locally for Development

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt mss pillow
   ```
2. **Start the Central Server:**
   ```bash
   python run_server.py
   ```
3. **Run the Agent from source:**
   ```bash
   python agent/main.py
   ```
4. **Run the automated verification suite:**
   ```bash
   python -u test_direct.py
   ```
