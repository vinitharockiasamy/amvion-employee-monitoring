// Employee Monitoring System — Dashboard Controller

let devicesMap = new Map();
let currentFilter = 'all';
let activeStreamDeviceId = null;
let ws = null;
let pc = null;
let reconnectTimer = null;
let lastFrameTime = performance.now();
let frameCount = 0;
let fpsInterval = null;

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    fetchDevices();
    fetchPublicUrl();
    connectWebSocket();
    startRelativeTimeTicker();

    // Keyboard shortcut: Esc to exit detail view
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && activeStreamDeviceId) {
            closeDetailModal();
        }
    });
});

async function fetchPublicUrl() {
    try {
        const res = await fetch('/api/v1/admin/public-url');
        const data = await res.json();
        if (data.public_url) {
            const badge = document.getElementById('publicUrlBadge');
            const text = document.getElementById('publicUrlText');
            if (badge && text) {
                badge.href = data.public_url;
                text.textContent = data.public_url.replace('https://', '');
                badge.classList.remove('hidden');
                badge.classList.add('flex');
            }
        }
    } catch (e) {
        // ignore
    }
}

async function deleteDevice(event, deviceId, computerName) {
    event.stopPropagation();
    try {
        const res = await fetch(`/api/v1/admin/devices/${deviceId}`, { method: 'DELETE' });
        if (res.ok) {
            devicesMap.delete(deviceId);
            updateStats();
            renderScreenWall();
        }
    } catch (err) {
        console.error('Failed to remove device:', err);
    }
}

// Fetch devices list from REST API
async function fetchDevices() {
    try {
        const res = await fetch('/api/v1/admin/devices');
        const data = await res.json();
        
        devicesMap.clear();
        data.devices.forEach(d => devicesMap.set(d.device_id, d));
        
        updateStats();
        renderScreenWall();
    } catch (err) {
        console.error('Failed to fetch devices:', err);
    }
}

// WebSocket Connection & Event Handler
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws/dashboard`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        console.log('[WS] Connected to Central Server');
        const statusEl = document.getElementById('wsConnectionStatus');
        if (statusEl) {
            statusEl.innerHTML = `
                <span class="w-2 h-2 rounded-full bg-emerald-400"></span>
                <span class="text-emerald-300">Live Connected</span>
            `;
            statusEl.className = 'flex items-center space-x-2 px-3 py-1.5 rounded-full text-xs font-medium bg-emerald-500/10 border border-emerald-500/30';
        }
        clearTimeout(reconnectTimer);
    };

    ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            handleWsMessage(msg);
        } catch (err) {
            console.error('[WS] Parse error:', err);
        }
    };

    ws.onclose = () => {
        console.warn('[WS] Connection closed, retrying in 3s...');
        const statusEl = document.getElementById('wsConnectionStatus');
        if (statusEl) {
            statusEl.innerHTML = `
                <span class="w-2 h-2 rounded-full bg-rose-400 animate-pulse"></span>
                <span class="text-rose-300">Reconnecting...</span>
            `;
            statusEl.className = 'flex items-center space-x-2 px-3 py-1.5 rounded-full text-xs font-medium bg-rose-500/10 border border-rose-500/30';
        }
        reconnectTimer = setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = (err) => {
        console.error('[WS] Error:', err);
        ws.close();
    };
}

// Process Incoming Real-time Messages
function handleWsMessage(msg) {
    switch (msg.type) {
        case 'SCREENSHOT_UPDATED':
            handleScreenshotUpdate(msg);
            break;
        case 'DEVICE_STATUS':
            handleDeviceStatusChange(msg);
            break;
        case 'LIVE_FRAME':
            handleLiveFrame(msg);
            break;
        case 'SIGNAL_OFFER':
            handleWebRtcOffer(msg);
            break;
        case 'SIGNAL_ICE':
            handleWebRtcIce(msg);
            break;
        case 'STREAM_STOPPED':
            if (activeStreamDeviceId === msg.device_id) {
                alert(`Live stream stopped: ${msg.reason || 'Closed'}`);
                closeDetailModal();
            }
            break;
        case 'STREAM_AGENT_OFFLINE':
            handleStreamAgentOffline(msg);
            break;
    }
}

// Handle Periodic Screenshot Updates from Server
function handleScreenshotUpdate(msg) {
    const dev = devicesMap.get(msg.device_id);
    if (dev) {
        dev.latest_screenshot_url = msg.screenshot_url;
        dev.last_seen_at = msg.last_seen;
        dev.status = 'online';
    } else {
        // New device discovered
        devicesMap.set(msg.device_id, {
            device_id: msg.device_id,
            employee_name: msg.employee_name,
            computer_name: msg.computer_name,
            status: 'online',
            current_mode: 'screenshot',
            last_seen_at: msg.last_seen,
            latest_screenshot_url: msg.screenshot_url
        });
        renderScreenWall();
    }

    updateStats();
    updateTileImage(msg.device_id, msg.screenshot_url, msg.last_seen);
}

// Handle Device Online/Offline status transition
function handleDeviceStatusChange(msg) {
    const dev = devicesMap.get(msg.device_id);
    if (dev) {
        dev.status = msg.status;
        if (msg.status === 'offline') {
            dev.current_mode = 'screenshot';
        }
        updateStats();
        renderScreenWall();
    }
}

// Handle High-Speed Live Video Frames in Detail View
function handleLiveFrame(msg) {
    if (msg.device_id !== activeStreamDeviceId) return;

    const imgEl = document.getElementById('detailScreenshotImg');
    const loadingOverlay = document.getElementById('streamLoadingOverlay');
    const hudLatency = document.getElementById('hudLatency');
    const hudTimestamp = document.getElementById('hudTimestamp');

    if (loadingOverlay) loadingOverlay.classList.add('hidden');
    
    if (imgEl && msg.frame) {
        imgEl.src = `data:image/jpeg;base64,${msg.frame}`;
        frameCount++;

        if (msg.timestamp) {
            const lat = Math.max(20, Math.round(performance.now() - msg.timestamp));
            if (hudLatency) hudLatency.textContent = `${lat}ms`;
        }
        if (hudTimestamp) {
            hudTimestamp.textContent = new Date().toLocaleTimeString();
        }
    }
}

function handleStreamAgentOffline(msg) {
    if (msg.device_id !== activeStreamDeviceId) return;
    const loadingOverlay = document.getElementById('streamLoadingOverlay');
    if (loadingOverlay) {
        loadingOverlay.classList.remove('hidden');
        loadingOverlay.innerHTML = `
            <div class="w-12 h-12 rounded-full bg-amber-500/20 border border-amber-500/40 flex items-center justify-center text-amber-400 mb-3">
                <svg class="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"></path></svg>
            </div>
            <p class="text-sm font-semibold text-white">Workstation Agent Not Running</p>
            <p class="text-xs text-slate-400 max-w-sm text-center mt-1">Start <code class="bg-slate-800 px-1.5 py-0.5 rounded text-emerald-300 font-mono">python agent/main.py</code> in a terminal to enable real-time 15 FPS streaming.</p>
        `;
    }
}

// WebRTC Signaling Handlers
async function handleWebRtcOffer(msg) {
    if (!activeStreamDeviceId || msg.device_id !== activeStreamDeviceId) return;

    try {
        if (!pc) {
            pc = new RTCPeerConnection({
                iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
            });

            pc.ontrack = (event) => {
                const video = document.getElementById('liveVideoPlayer');
                const imgEl = document.getElementById('detailScreenshotImg');
                const loadingOverlay = document.getElementById('streamLoadingOverlay');

                if (video && event.streams[0]) {
                    video.srcObject = event.streams[0];
                    video.classList.remove('hidden');
                    if (imgEl) imgEl.classList.add('hidden');
                    if (loadingOverlay) loadingOverlay.classList.add('hidden');
                }
            };

            pc.onicecandidate = (event) => {
                if (event.candidate && ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({
                        type: 'SIGNAL_ICE',
                        device_id: activeStreamDeviceId,
                        candidate: event.candidate
                    }));
                }
            };
        }

        await pc.setRemoteDescription(new RTCSessionDescription({ type: 'offer', sdp: msg.sdp }));
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);

        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({
                type: 'SIGNAL_ANSWER',
                device_id: activeStreamDeviceId,
                sdp: answer.sdp
            }));
        }
    } catch (err) {
        console.warn('[WebRTC] Native peer negotiation fell back to fast frame streaming:', err);
    }
}

async function handleWebRtcIce(msg) {
    if (pc && msg.candidate) {
        try {
            await pc.addIceCandidate(new RTCIceCandidate(msg.candidate));
        } catch (err) {
            console.error('[WebRTC] Error adding ICE candidate:', err);
        }
    }
}

// Render the Live Screen Wall Grid
function renderScreenWall() {
    const grid = document.getElementById('screenWallGrid');
    const emptyState = document.getElementById('emptyState');
    if (!grid) return;

    let devices = Array.from(devicesMap.values());

    if (currentFilter === 'online') {
        devices = devices.filter(d => d.status === 'online');
    } else if (currentFilter === 'offline') {
        devices = devices.filter(d => d.status === 'offline');
    }

    const badgeCount = document.getElementById('wallBadgeCount');
    if (badgeCount) badgeCount.textContent = devices.length;

    if (devices.length === 0) {
        grid.innerHTML = '';
        if (emptyState) emptyState.classList.remove('hidden');
        return;
    } else {
        if (emptyState) emptyState.classList.add('hidden');
    }

    grid.innerHTML = devices.map(dev => {
        const isOnline = dev.status === 'online';
        const isStreaming = dev.current_mode === 'streaming';
        const statusDot = isOnline 
            ? `<span class="w-2.5 h-2.5 rounded-full bg-emerald-400 ${isStreaming ? 'animate-ping' : ''}"></span>` 
            : `<span class="w-2.5 h-2.5 rounded-full bg-slate-500"></span>`;
        const statusText = isOnline 
            ? (isStreaming ? 'LIVE STREAM' : 'ONLINE') 
            : 'OFFLINE';
        const statusColor = isOnline 
            ? (isStreaming ? 'bg-sky-500/20 text-sky-300 border-sky-500/40' : 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40')
            : 'bg-slate-700/60 text-slate-400 border-slate-600';

        const screenshotSrc = dev.latest_screenshot_url 
            ? dev.latest_screenshot_url 
            : 'data:image/svg+xml;charset=UTF-8,%3Csvg%20width%3D%22400%22%20height%3D%22225%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Crect%20width%3D%22100%25%22%20height%3D%22100%25%22%20fill%3D%22%231e293b%22%2F%3E%3Ctext%20x%3D%2250%25%22%20y%3D%2250%25%22%20fill%3D%22%2364748b%22%20font-size%3D%2214%22%20text-anchor%3D%22middle%22%20dy%3D%22.3em%22%3ENo%20Screenshot%20Yet%3C%2Ftext%3E%3C%2Fsvg%3E';

        const lastSeenText = formatRelativeTime(dev.last_seen_at);

        return `
            <div id="tile-${dev.device_id}" onclick="openDetailModal('${dev.device_id}')" 
                 class="screen-tile group bg-slate-800/80 border border-slate-700/80 hover:border-emerald-500/60 rounded-xl overflow-hidden cursor-pointer flex flex-col shadow-lg backdrop-blur">
                
                <!-- Screen Preview Container (16:9 Aspect) -->
                <div class="aspect-16-9 bg-slate-950 relative overflow-hidden flex items-center justify-center">
                    <img id="img-${dev.device_id}" 
                         src="${screenshotSrc}" 
                         alt="${dev.employee_name}" 
                         loading="lazy"
                         class="w-full h-full object-cover transition-transform duration-300 group-hover:scale-105 ${!isOnline ? 'opacity-40 grayscale' : ''}">
                    
                    <!-- Top Overlay: Status Badge -->
                    <div class="absolute top-2.5 left-2.5 flex items-center space-x-1.5 px-2.5 py-1 rounded-full text-[11px] font-semibold border backdrop-blur-md ${statusColor}">
                        ${statusDot}
                        <span>${statusText}</span>
                    </div>

                    <!-- Top Right: Remove Uninstalled Device Button -->
                    <button onclick="deleteDevice(event, '${dev.device_id}', '${escapeHtml(dev.computer_name)}')"
                            title="Remove device from dashboard"
                            class="absolute top-2.5 right-2.5 z-20 p-1.5 rounded-full bg-slate-900/70 hover:bg-rose-500 text-slate-300 hover:text-white border border-slate-700/80 transition opacity-0 group-hover:opacity-100">
                        <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"></path>
                        </svg>
                    </button>

                    <!-- Bottom Overlay on Hover -->
                    <div class="absolute inset-0 bg-emerald-950/40 opacity-0 group-hover:opacity-100 transition-opacity flex items-center justify-center">
                        <div class="px-3 py-1.5 rounded-lg bg-emerald-500 text-slate-950 text-xs font-bold tracking-wide shadow-md flex items-center space-x-1.5">
                            <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"></path><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"></path></svg>
                            <span>OPEN LIVE STREAM</span>
                        </div>
                    </div>
                </div>

                <!-- Computer & Employee Meta Info -->
                <div class="p-3.5 flex flex-col justify-between flex-1 bg-slate-800/90 border-t border-slate-700/50">
                    <div class="flex items-start justify-between">
                        <div>
                            <h4 class="text-sm font-semibold text-white group-hover:text-emerald-400 transition-colors truncate max-w-[170px]">${escapeHtml(dev.employee_name)}</h4>
                            <p class="text-xs font-mono text-slate-400 mt-0.5 truncate max-w-[170px]">${escapeHtml(dev.computer_name)}</p>
                        </div>
                        <div class="text-right">
                            <span class="text-[10px] text-slate-400 block">Last update</span>
                            <span id="time-${dev.device_id}" class="text-[11px] font-medium text-slate-300 font-mono">${lastSeenText}</span>
                        </div>
                    </div>
                </div>

            </div>
        `;
    }).join('');
}

// Fast partial tile update for incoming screenshot without DOM rebuild
function updateTileImage(deviceId, screenshotUrl, lastSeen) {
    const imgEl = document.getElementById(`img-${deviceId}`);
    const timeEl = document.getElementById(`time-${deviceId}`);
    
    if (imgEl) {
        imgEl.src = screenshotUrl;
        imgEl.classList.remove('opacity-40', 'grayscale');
    }
    if (timeEl) {
        timeEl.textContent = formatRelativeTime(lastSeen);
    }
}

// Open Detail Modal & Switch Endpoint to Live Stream Mode
function openDetailModal(deviceId) {
    const dev = devicesMap.get(deviceId);
    if (!dev) return;

    activeStreamDeviceId = deviceId;
    dev.current_mode = 'streaming';

    const modal = document.getElementById('detailModal');
    const modalEmployeeName = document.getElementById('modalEmployeeName');
    const modalComputerName = document.getElementById('modalComputerName');
    const modalIp = document.getElementById('modalIp');
    const modalOs = document.getElementById('modalOs');
    const detailImg = document.getElementById('detailScreenshotImg');
    const loadingOverlay = document.getElementById('streamLoadingOverlay');
    const video = document.getElementById('liveVideoPlayer');

    if (modalEmployeeName) modalEmployeeName.textContent = dev.employee_name;
    if (modalComputerName) modalComputerName.textContent = dev.computer_name;
    if (modalIp) modalIp.textContent = `IP: ${dev.ip_address || 'Local'}`;
    if (modalOs) modalOs.textContent = dev.os_version || 'Windows';

    // Show initial latest screenshot while live stream connects
    if (detailImg) {
        detailImg.classList.remove('hidden');
        detailImg.src = dev.latest_screenshot_url || '';
    }
    if (video) {
        video.classList.add('hidden');
        video.srcObject = null;
    }
    if (loadingOverlay) {
        loadingOverlay.classList.remove('hidden');
        loadingOverlay.innerHTML = `
            <div class="w-12 h-12 border-4 border-sky-400 border-t-transparent rounded-full animate-spin mb-3"></div>
            <p class="text-sm font-medium text-sky-200">Switching endpoint to Live Stream Mode...</p>
            <p class="text-xs text-slate-400 mt-1">Establishing low-latency video channel</p>
        `;
    }

    modal.classList.remove('hidden');

    // Send Live Stream activation request to Server
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'REQUEST_STREAM',
            device_id: deviceId
        }));
    }

    // Start FPS Counter
    frameCount = 0;
    lastFrameTime = performance.now();
    clearInterval(fpsInterval);
    fpsInterval = setInterval(() => {
        const hudFps = document.getElementById('hudFps');
        if (hudFps) {
            hudFps.textContent = frameCount;
        }
        frameCount = 0;
    }, 1000);

    renderScreenWall();
}

// Close Detail Modal & Revert Endpoint to Periodic Screenshot Mode
function closeDetailModal() {
    if (!activeStreamDeviceId) return;

    const deviceId = activeStreamDeviceId;
    activeStreamDeviceId = null;

    const dev = devicesMap.get(deviceId);
    if (dev) {
        dev.current_mode = 'screenshot';
    }

    // Command server to stop live stream
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({
            type: 'STOP_STREAM',
            device_id: deviceId
        }));
    }

    // Clean up WebRTC peer connection
    if (pc) {
        pc.close();
        pc = null;
    }

    clearInterval(fpsInterval);

    const modal = document.getElementById('detailModal');
    const video = document.getElementById('liveVideoPlayer');
    if (video) {
        video.srcObject = null;
        video.classList.add('hidden');
    }
    if (modal) modal.classList.add('hidden');

    renderScreenWall();
    updateStats();
}

// Update Top Metric Counters
function updateStats() {
    const devices = Array.from(devicesMap.values());
    const total = devices.length;
    const online = devices.filter(d => d.status === 'online').length;
    const offline = total - online;
    const streams = devices.filter(d => d.current_mode === 'streaming').length;

    const elTotal = document.getElementById('statTotalDevices');
    const elOnline = document.getElementById('statOnlineDevices');
    const elOffline = document.getElementById('statOfflineDevices');
    const elStreams = document.getElementById('statActiveStreams');

    if (elTotal) elTotal.textContent = total;
    if (elOnline) elOnline.textContent = online;
    if (elOffline) elOffline.textContent = offline;
    if (elStreams) elStreams.textContent = streams;
}

// Filter wall by online/offline
function setFilter(filter) {
    currentFilter = filter;
    
    ['filterAll', 'filterOnline', 'filterOffline'].forEach(id => {
        const btn = document.getElementById(id);
        if (btn) {
            btn.className = 'px-3 py-1.5 rounded-lg text-xs font-medium bg-slate-800 text-slate-300 hover:bg-slate-700 transition';
        }
    });

    const activeBtnId = filter === 'all' ? 'filterAll' : (filter === 'online' ? 'filterOnline' : 'filterOffline');
    const activeBtn = document.getElementById(activeBtnId);
    if (activeBtn) {
        activeBtn.className = 'px-3 py-1.5 rounded-lg text-xs font-medium bg-emerald-500 text-white transition';
    }

    renderScreenWall();
}

// Format ISO date to human relative time ("5s ago", "Just now")
function formatRelativeTime(dateString) {
    if (!dateString) return 'Never';
    let ds = String(dateString).trim();
    if (ds.includes(' ') && !ds.includes('T')) {
        ds = ds.replace(' ', 'T');
    }
    if (!ds.endsWith('Z') && !ds.includes('+')) {
        ds += 'Z';
    }
    const date = new Date(ds);
    if (isNaN(date.getTime())) return 'Just now';
    const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));

    if (seconds < 5) return 'Just now';
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ago`;
}

function startRelativeTimeTicker() {
    setInterval(() => {
        devicesMap.forEach((dev, deviceId) => {
            const timeEl = document.getElementById(`time-${deviceId}`);
            if (timeEl && dev.last_seen_at) {
                timeEl.textContent = formatRelativeTime(dev.last_seen_at);
            }
        });
    }, 2000);
}

function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}
