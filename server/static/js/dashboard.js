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
let currentMainTab = 'wall';
let dlpEventsList = [];
let dlpSearchTimer = null;

function getAdminHeaders() {
    const tok = window.ADMIN_AUTH_TOKEN || '';
    return tok ? { 'Authorization': `Bearer ${tok}` } : {};
}

function appendAdminTokenQuery(url) {
    const tok = window.ADMIN_AUTH_TOKEN || '';
    if (!tok || !url) return url;
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}token=${encodeURIComponent(tok)}`;
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    fetchDevices();
    fetchDlpEvents();
    fetchPublicUrl();
    connectWebSocket();
    startRelativeTimeTicker();

    // Keyboard shortcut: Esc to exit modals
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            if (activeStreamDeviceId) closeDetailModal();
            closeDlpPreviewModal();
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
        populateDlpDeviceDropdown();
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
        case 'DEVICE_REMOVED':
            devicesMap.delete(msg.device_id);
            updateStats();
            renderScreenWall();
            break;
        case 'DLP_EVENT_CREATED':
            if (msg.event) {
                handleIncomingDlpEvent(msg.event);
            }
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
        loadingOverlay.classList.add('hidden');
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
    // Also refresh open live modal if viewing this device
    if (activeStreamDeviceId === deviceId) {
        const detailImg = document.getElementById('detailScreenshotImg');
        const loadingOverlay = document.getElementById('streamLoadingOverlay');
        const hudTimestamp = document.getElementById('hudTimestamp');
        if (loadingOverlay) loadingOverlay.classList.add('hidden');
        if (detailImg) detailImg.src = screenshotUrl;
        if (hudTimestamp) hudTimestamp.textContent = new Date().toLocaleTimeString();
        frameCount++;
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

    // Show live screen immediately without dark blocking overlay
    if (detailImg) {
        detailImg.classList.remove('hidden');
        detailImg.src = dev.latest_screenshot_url || '';
    }
    if (video) {
        video.classList.add('hidden');
        video.srcObject = null;
    }
    if (loadingOverlay) {
        loadingOverlay.classList.add('hidden');
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

// =====================================================================
// PHASE 1B: DLP AUDIT & IN-BROWSER FILE CONTENT VIEWER
// =====================================================================

function switchMainTab(tab) {
    currentMainTab = tab;
    const secWall = document.getElementById('sectionScreenWall');
    const secDlp = document.getElementById('sectionDlpVault');
    const btnWall = document.getElementById('navTabWall');
    const btnDlp = document.getElementById('navTabDlp');

    if (tab === 'dlp') {
        if (secWall) secWall.classList.add('hidden');
        if (secDlp) {
            secDlp.classList.remove('hidden');
            secDlp.classList.add('flex');
        }
        if (btnDlp) btnDlp.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 transition flex items-center space-x-1.5';
        if (btnWall) btnWall.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold text-slate-300 hover:text-white hover:bg-slate-800 transition flex items-center space-x-1.5';
        fetchDlpEvents();
    } else {
        if (secDlp) {
            secDlp.classList.add('hidden');
            secDlp.classList.remove('flex');
        }
        if (secWall) secWall.classList.remove('hidden');
        if (btnWall) btnWall.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold bg-emerald-500 text-slate-950 transition flex items-center space-x-1.5';
        if (btnDlp) btnDlp.className = 'px-3.5 py-1.5 rounded-lg text-xs font-semibold text-slate-300 hover:text-white hover:bg-slate-800 transition flex items-center space-x-1.5';
    }
}

function populateDlpDeviceDropdown() {
    const sel = document.getElementById('dlpDeviceFilter');
    if (!sel) return;
    const prev = sel.value || 'all';
    const opts = ['<option value="all">All Endpoints</option>'];
    devicesMap.forEach((dev) => {
        opts.push(`<option value="${escapeHtml(dev.device_id)}">${escapeHtml(dev.employee_name)} (${escapeHtml(dev.computer_name)})</option>`);
    });
    sel.innerHTML = opts.join('');
    sel.value = prev;
}

function debouncedFetchDlpEvents() {
    clearTimeout(dlpSearchTimer);
    dlpSearchTimer = setTimeout(fetchDlpEvents, 280);
}

async function fetchDlpEvents() {
    try {
        const params = new URLSearchParams();
        const ep = document.getElementById('dlpDeviceFilter')?.value || 'all';
        const act = document.getElementById('dlpActionFilter')?.value || 'all';
        const ch = document.getElementById('dlpChannelFilter')?.value || 'all';
        const risk = document.getElementById('dlpRiskFilter')?.value || 'all';
        const q = document.getElementById('dlpSearchInput')?.value?.trim() || '';
        const startDt = document.getElementById('dlpStartDate')?.value || '';
        const endDt = document.getElementById('dlpEndDate')?.value || '';

        if (ep !== 'all') params.set('endpoint', ep);
        if (act !== 'all') params.set('event_type', act);
        if (ch !== 'all') params.set('channel', ch);
        if (risk !== 'all') params.set('risk_level', risk);
        if (q) params.set('search', q);
        if (startDt) params.set('start_date', `${startDt}T00:00:00`);
        if (endDt) params.set('end_date', `${endDt}T23:59:59`);
        params.set('limit', '200');

        const res = await fetch(`/api/dlp/events?${params.toString()}`, {
            headers: getAdminHeaders()
        });
        if (!res.ok) return;
        const data = await res.json();
        dlpEventsList = data.events || [];
        updateDlpSummaryUI(data.summary || { total_events: data.total || 0 });
        renderDlpTable();
    } catch (err) {
        console.error('Failed to fetch DLP events:', err);
    }
}

function updateDlpSummaryUI(sum) {
    const setTxt = (id, val) => {
        const el = document.getElementById(id);
        if (el) el.textContent = val ?? 0;
    };
    setTxt('dlpStatTotal', sum.total_events || 0);
    setTxt('dlpStatHighRisk', sum.high_risk_count || 0);
    setTxt('dlpStatArchived', sum.archived_files_count || 0);
    setTxt('dlpStatExternal', sum.external_transfers_count || 0);
    setTxt('dlpStatDeleted', sum.deleted_files_count || 0);
    setTxt('navDlpBadge', sum.total_events || 0);
}

function handleIncomingDlpEvent(ev) {
    dlpEventsList.unshift(ev);
    if (dlpEventsList.length > 250) dlpEventsList.pop();
    const badge = document.getElementById('navDlpBadge');
    if (badge) badge.textContent = String((parseInt(badge.textContent || '0', 10) || 0) + 1);
    if (currentMainTab === 'dlp') {
        fetchDlpEvents();
    }
}

function formatBytes(bytes) {
    const n = Number(bytes) || 0;
    if (n <= 0) return '—';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

function renderDlpTable() {
    const tbody = document.getElementById('dlpEventsTableBody');
    const emptyEl = document.getElementById('dlpEmptyState');
    if (!tbody) return;

    if (!dlpEventsList.length) {
        tbody.innerHTML = '';
        if (emptyEl) emptyEl.classList.remove('hidden');
        return;
    }
    if (emptyEl) emptyEl.classList.add('hidden');

    tbody.innerHTML = dlpEventsList.map(ev => {
        const riskStyles = (ev.risk_level === 'high' || ev.risk_level === 'critical')
            ? 'bg-rose-500/20 text-rose-300 border-rose-500/40'
            : (ev.risk_level === 'medium'
                ? 'bg-amber-500/20 text-amber-300 border-amber-500/40'
                : 'bg-slate-700/70 text-slate-300 border-slate-600');

        const actionBadge = ev.event_type === 'DELETED'
            ? 'bg-rose-500/15 text-rose-300'
            : (ev.event_type === 'USB_TRANSFER' || ev.event_type === 'WEB_UPLOAD'
                ? 'bg-amber-500/15 text-amber-300'
                : 'bg-sky-500/15 text-sky-300');

        const srcText = ev.source_path ? escapeHtml(ev.source_path) : '—';
        const dstText = ev.destination_path
            ? `<div class="text-emerald-300 mt-0.5 truncate max-w-xs" title="${escapeHtml(ev.destination_path)}">→ ${escapeHtml(ev.destination_path)}</div>`
            : '';

        const metaNote = (ev.metadata && (ev.metadata.window_title || ev.metadata.drive_label || ev.metadata.details)) || '';
        const dlUrl = ev.download_url ? appendAdminTokenQuery(ev.download_url) : '#';

        const evStatusRaw = (ev.metadata && ev.metadata.evidence_status) || '';
        const evStatusLabels = {
            'exceeds_size_limit': 'Exceeds 25 MB limit',
            'locked_or_unavailable': 'File locked / unavailable',
            'disabled_by_policy': 'Disabled by policy',
            'not_applicable': 'Volume event',
            'upload_failed': 'Evidence upload failed'
        };
        const fallbackStatusLabel = evStatusLabels[evStatusRaw] || (evStatusRaw ? escapeHtml(evStatusRaw) : 'No evidence');

        const actionBtns = ev.has_content && ev.file_blob_id
            ? `<div class="inline-flex items-center space-x-1.5">
                   <button onclick="openDlpPreviewModal('${ev.file_blob_id}')"
                           class="px-2.5 py-1 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-slate-950 font-semibold text-[11px] transition">
                       View File
                   </button>
                   <a href="${dlUrl}" download
                      class="px-2 py-1 rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-200 text-[11px] transition"
                      title="Download original file">
                       ↓
                   </a>
               </div>`
            : `<span class="text-[11px] text-slate-500">${fallbackStatusLabel}</span>`;

        return `
            <tr class="hover:bg-slate-800/90 transition">
                <td class="py-3 px-4 whitespace-nowrap font-mono text-[11px] text-slate-400">
                    ${formatRelativeTime(ev.timestamp)}
                </td>
                <td class="py-3 px-4 whitespace-nowrap">
                    <div class="font-semibold text-white">${escapeHtml(ev.employee_name)}</div>
                    <div class="text-[11px] font-mono text-slate-400">${escapeHtml(ev.computer_name || ev.endpoint_id)}</div>
                </td>
                <td class="py-3 px-4 whitespace-nowrap">
                    <span class="inline-block px-2 py-0.5 rounded font-semibold text-[10px] ${actionBadge}">${escapeHtml(ev.event_type)}</span>
                    <span class="ml-1 inline-block px-2 py-0.5 rounded-full border text-[10px] uppercase font-bold ${riskStyles}">${escapeHtml(ev.risk_level)}</span>
                </td>
                <td class="py-3 px-4">
                    <div class="font-semibold text-slate-100 truncate max-w-[200px]" title="${escapeHtml(ev.file_name)}">${escapeHtml(ev.file_name)}</div>
                    <div class="text-[11px] text-slate-400 font-mono">${formatBytes(ev.file_size)}</div>
                </td>
                <td class="py-3 px-4 font-mono text-[11px] text-slate-300">
                    <div class="truncate max-w-xs" title="${srcText}">${srcText}</div>
                    ${dstText}
                </td>
                <td class="py-3 px-4">
                    <div class="font-medium text-slate-200">${escapeHtml(ev.channel)}</div>
                    ${ev.application_name ? `<div class="text-[11px] text-sky-300 font-mono">${escapeHtml(ev.application_name)}</div>` : ''}
                    ${metaNote ? `<div class="text-[11px] text-slate-400 truncate max-w-[200px]" title="${escapeHtml(metaNote)}">${escapeHtml(metaNote)}</div>` : ''}
                </td>
                <td class="py-3 px-4 text-right whitespace-nowrap">
                    ${actionBtns}
                </td>
            </tr>
        `;
    }).join('');
}

async function openDlpPreviewModal(fileId) {
    const modal = document.getElementById('dlpPreviewModal');
    const titleEl = document.getElementById('dlpModalFileName');
    const metaEl = document.getElementById('dlpModalMeta');
    const dlBtn = document.getElementById('dlpModalDownloadBtn');
    const viewport = document.getElementById('dlpModalViewport');
    if (!modal || !viewport) return;

    modal.classList.remove('hidden');
    modal.classList.add('flex');
    viewport.innerHTML = `<div class="text-slate-400 text-sm">Loading archived file content...</div>`;

    try {
        const res = await fetch(`/api/dlp/files/${fileId}/preview`, {
            headers: getAdminHeaders()
        });
        if (!res.ok) throw new Error('File content not available');
        const info = await res.json();

        const rawAuthUrl = appendAdminTokenQuery(info.raw_url);
        const dlAuthUrl = appendAdminTokenQuery(info.download_url);

        if (titleEl) titleEl.textContent = info.original_filename;
        if (metaEl) metaEl.textContent = `${formatBytes(info.file_size)} • ${info.content_type} • SHA-256: ${(info.sha256 || '').slice(0, 16)}...`;
        if (dlBtn) dlBtn.href = dlAuthUrl;

        if (info.kind === 'image') {
            viewport.innerHTML = `<img src="${rawAuthUrl}" alt="${escapeHtml(info.original_filename)}" class="max-h-[72vh] max-w-full object-contain rounded-lg shadow-lg">`;
        } else if (info.kind === 'pdf') {
            viewport.innerHTML = `<iframe src="${rawAuthUrl}" class="w-full h-[72vh] rounded-lg border border-slate-700 bg-white"></iframe>`;
        } else if (info.kind === 'video') {
            viewport.innerHTML = `<video src="${rawAuthUrl}" controls autoplay class="max-h-[72vh] max-w-full rounded-lg"></video>`;
        } else if (info.kind === 'audio') {
            viewport.innerHTML = `<audio src="${rawAuthUrl}" controls autoplay class="w-full max-w-lg"></audio>`;
        } else if (info.kind === 'document' || info.kind === 'text') {
            viewport.innerHTML = `<pre class="w-full max-h-[72vh] overflow-auto bg-slate-900 border border-slate-700 rounded-xl p-5 text-xs text-slate-200 font-mono whitespace-pre-wrap self-start">${escapeHtml(info.content || '')}</pre>`;
        } else if (info.kind === 'table') {
            const rows = info.rows || [];
            if (!rows.length) {
                viewport.innerHTML = `<div class="text-slate-400 text-sm">Empty spreadsheet / table.</div>`;
            } else {
                const tableHtml = rows.map((r, idx) => {
                    const cellTag = idx === 0 ? 'th' : 'td';
                    const cellCls = idx === 0
                        ? 'py-2 px-3 bg-slate-800 font-semibold text-emerald-300 border border-slate-700 text-xs'
                        : 'py-1.5 px-3 border border-slate-800 text-slate-200 text-xs font-mono';
                    return `<tr>${r.map(c => `<${cellTag} class="${cellCls}">${escapeHtml(c)}</${cellTag}>`).join('')}</tr>`;
                }).join('');
                viewport.innerHTML = `<div class="w-full max-h-[72vh] overflow-auto self-start"><table class="w-full border-collapse">${tableHtml}</table></div>`;
            }
        } else if (info.kind === 'archive') {
            const listHtml = (info.entries || []).map(e =>
                `<tr class="border-b border-slate-800"><td class="py-1.5 px-3 font-mono text-xs text-slate-200">${escapeHtml(e.name)}</td><td class="py-1.5 px-3 font-mono text-xs text-slate-400 text-right">${formatBytes(e.size)}</td></tr>`
            ).join('');
            viewport.innerHTML = `<div class="w-full max-h-[72vh] overflow-auto self-start"><table class="w-full text-left"><thead class="bg-slate-800 text-xs text-slate-300"><tr><th class="py-2 px-3">Archive Entry</th><th class="py-2 px-3 text-right">Size</th></tr></thead><tbody>${listHtml}</tbody></table></div>`;
        } else {
            viewport.innerHTML = `
                <div class="text-center max-w-md space-y-3">
                    <div class="text-sm font-semibold text-white">${escapeHtml(info.original_filename)}</div>
                    <p class="text-xs text-slate-400">${escapeHtml(info.note || 'Download the archived file to view it in its native CAD/design software.')}</p>
                </div>`;
        }
    } catch (err) {
        viewport.innerHTML = `<div class="text-rose-400 text-sm">Unable to preview file: ${escapeHtml(err.message)}</div>`;
    }
}

function closeDlpPreviewModal() {
    const modal = document.getElementById('dlpPreviewModal');
    const viewport = document.getElementById('dlpModalViewport');
    if (modal) {
        modal.classList.add('hidden');
        modal.classList.remove('flex');
    }
    if (viewport) viewport.innerHTML = '';
}
