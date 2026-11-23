# Employee Monitoring System (Enterprise MVP)

An enterprise-grade administrative desktop screen monitoring utility designed for managed Windows endpoints. Provides visual situational awareness across corporate workstations with dual operational modes:
1. **Periodic Screenshot Mode**: Continuous low-overhead desktop captures (~5s interval) feeding a dynamic **Live Screen Wall** dashboard.
2. **On-Demand Live Stream Mode**: Real-time low-latency video streaming (~15 FPS) activated exclusively when an administrator inspects an individual employee computer, automatically reverting to screenshot mode upon exit.

---

## Architecture Overview

```text
EmployeeMonitoringSystem/
├── agent/                       # Managed Windows Client Agent
│   ├── capture.py               # MSS screen grabber + DPI awareness & Pillow fallback
│   ├── compressor.py            # Pillow adaptive JPEG/WebP compression
│   ├── streamer.py              # 15 FPS low-latency frame streamer
│   ├── client.py                # HTTP upload client + persistent WebSocket receiver
│   ├── config.py                # Hardware UUID & machine state management
│   └── main.py                  # Agent daemon lifecycle & state machine
├── server/                      # FastAPI Central Server & Web Dashboard
│   ├── app/
│   │   ├── api/
│   │   │   ├── auth.py          # Device enrollment & admin auth
│   │   │   ├── agent.py         # Screenshot upload & heartbeat ingestion
│   │   │   ├── admin.py         # Screen wall query & screenshot serving
│   │   │   └── websocket.py     # Real-time dashboard broadcaster & stream controller
│   │   ├── core/
│   │   │   ├── config.py        # Enterprise settings & paths
│   │   │   └── security.py      # Secret hashing & constant-time authentication
│   │   ├── db/
│   │   │   ├── session.py       # SQLAlchemy SQLite connection pooling
│   │   │   └── models.py        # Device, Screenshot, StreamSession models
│   │   └── main.py              # FastAPI application entry point
│   ├── static/
│   │   ├── css/style.css        # Live badges, 16:9 preview tiles, animations
│   │   └── js/dashboard.js      # Live wall controller & stream player
│   ├── templates/
│   │   └── index.html           # Admin Dashboard with Live Screen Wall
│   └── storage/
│       └── screenshots/         # Ingested screenshot file repository
├── run_server.py                # Server launcher script
├── test_direct.py               # End-to-end verification test suite
└── README.md
```

---

## Key Features

- **Live Screen Wall**:
  - Displays all online managed workstations in a responsive grid.
  - Automatically updates screenshot thumbnails approximately every 5 seconds without page reloading.
  - Shows Employee Name, Computer Name, IP/OS details, Online/Offline status badge, and relative last-seen times.
- **On-Demand Live Streaming**:
  - Clicking any computer tile opens a detailed view and switches the endpoint to Live Stream Mode.
  - Streams low-latency frames (15 FPS, ~100–200ms latency) with an integrated FPS/latency HUD.
  - Closing the detailed view immediately signals the agent to tear down the live stream and resume periodic Screenshot Mode.
- **Enterprise Security & Enrollment**:
  - Zero-trust enrollment: Workstations enroll using a pre-shared enterprise enrollment key (`ORG_ENROLLMENT_KEY`) combined with hardware fingerprinting (`MachineGuid`).
  - Cryptographically secure 256-bit `device_secret` issued per device.
  - All screenshot uploads and WebSocket channels require authenticated headers (`X-Device-Id`, `Bearer <secret>`).
  - Strictly non-covert: operates transparently as an enterprise background service for managed machines.

---

## Quick Start Guide

### 1. Prerequisites
- Python 3.10+ (tested on Python 3.11/3.14 on Windows)
- Required packages:
  ```bash
  pip install fastapi uvicorn mss pillow websockets httpx sqlalchemy python-multipart jinja2
  ```

### 2. Start the Central Server
From the project root:
```bash
python run_server.py
```
- Central Server runs at: `http://127.0.0.1:8000`
- Web Admin Dashboard: Open `http://127.0.0.1:8000/` in any web browser.

### 3. Start the Windows Agent
On a managed employee computer (or in a separate terminal on the same machine):
```bash
python agent/main.py
```
- The agent automatically discovers the hardware UUID and computer name.
- It performs the initial enrollment handshake with the server.
- Begins capturing desktop screenshots every 5 seconds and streaming them to the server.
- Connects to the WebSocket hub to await on-demand live streaming requests.

### 4. Open the Admin Dashboard
1. Open `http://127.0.0.1:8000/` in Chrome, Edge, or Firefox.
2. Watch the **Live Screen Wall** display the workstation tile with auto-updating screenshots every 5s.
3. Click on the workstation tile to open the **Detail View**.
4. Notice the endpoint instantly switches to **LIVE STREAM MODE** (15 FPS low latency).
5. Close the modal or press `Esc` — the endpoint immediately reverts back to 5-second **Periodic Screenshot Mode**.

---

## Verification Test Suite

Run the automated test suite verifying all 8 core functional requirements (capture, compression, DB, enrollment, screenshot ingestion, screen wall query, file serving, and dual-mode switching):
```bash
python -u test_direct.py
```
