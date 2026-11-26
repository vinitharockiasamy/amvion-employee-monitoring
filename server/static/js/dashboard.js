// =====================================================================
// Employee Monitoring System — Dashboard Controller  v1.2.2
// =====================================================================

// ── State ─────────────────────────────────────────────────────────────
let devicesMap        = new Map();   // device_id → device object
let currentFilter     = 'all';       // 'all' | 'online' | 'offline'
let currentDepartment = 'All';       // department name | 'All'
let selectedDeviceIds = new Set();   // multi-selected device ids
let currentGridSize   = 4;           // 2 | 4 | 6 | 8
let activeStreamDeviceId = null;

let ws              = null;
let pc              = null;
let reconnectTimer  = null;
let lastFrameTime   = performance.now();
let frameCount      = 0;
let fpsInterval     = null;
let currentMainTab  = 'wall';

// ── Auth helpers ───────────────────────────────────────────────────────
function getAdminHeaders() {
    const tok = window.ADMIN_AUTH_TOKEN || '';
    return tok ? { 'Authorization': `Bearer ${tok}`, 'X-Admin-Token': tok } : {};
}

function appendAdminTokenQuery(url) {
    const tok = window.ADMIN_AUTH_TOKEN || '';
    if (!tok || !url) return url;
    const sep = url.includes('?') ? '&' : '?';
    return `${url}${sep}token=${encodeURIComponent(tok)}`;
}

// ── Init ───────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    setGrid(4, true);           // default grid 4
    fetchDevices();
    fetchPublicUrl();
    connectWebSocket();
    startRelativeTimeTicker();
    switchMainTab('wall');

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && activeStreamDeviceId) closeDetailModal();
    });
});

// ── Logout ─────────────────────────────────────────────────────────────
async function logoutAdmin() {
    try { await fetch('/api/v1/auth/admin/logout', { method: 'POST' }); } catch {}
    window.location.href = '/';
}

// ── Public URL badge ───────────────────────────────────────────────────
async function fetchPublicUrl() {
    try {
        const res = await fetch('/api/v1/admin/public-url', { headers: getAdminHeaders() });
        if (!res.ok) return;
        const data = await res.json();
        if (data.public_url) {
            const badge = document.getElementById('publicUrlBadge');
            const text  = document.getElementById('publicUrlText');
            if (badge && text) {
                badge.href = data.public_url;
                text.textContent = data.public_url.replace('https://', '');
                badge.classList.remove('hidden');
                badge.classList.add('flex');
            }
        }
    } catch {}
}

// ── Main Tab Switcher ──────────────────────────────────────────────────
function switchMainTab(tab) {
    currentMainTab = tab;
    const tabs = { wall: 'navTabWall', employees: 'navTabEmployees' };
    const sections = { wall: 'sectionScreenWall', employees: 'sectionEmployees' };

    Object.entries(tabs).forEach(([key, id]) => {
        const btn = document.getElementById(id);
        if (!btn) return;
        if (key === tab) {
            btn.className = btn.className
                .replace('text-slate-400', 'text-slate-950')
                .replace('hover:bg-slate-700', '');
            btn.classList.add('bg-emerald-500', 'text-slate-950');
            btn.classList.remove('text-slate-400', 'hover:bg-slate-700', 'hover:text-white');
        } else {
            btn.classList.remove('bg-emerald-500', 'text-slate-950');
            btn.classList.add('text-slate-400', 'hover:bg-slate-700', 'hover:text-white');
        }
    });

    Object.entries(sections).forEach(([key, id]) => {
        const el = document.getElementById(id);
        if (!el) return;
        if (key === tab) {
            el.classList.remove('hidden');
            el.classList.add('flex');
        } else {
            el.classList.add('hidden');
            el.classList.remove('flex');
        }
    });

    if (tab === 'employees') renderEmployeeTable();
    if (tab === 'wall')      renderScreenWall();
}

// ── REST: Fetch devices ─────────────────────────────────────────────────
async function fetchDevices() {
    try {
        const res = await fetch('/api/v1/admin/devices', { headers: getAdminHeaders() });
        if (res.status === 401) { window.location.href = '/'; return; }
        if (!res.ok) return;
        const data = await res.json();

        devicesMap.clear();
        (data.devices || []).forEach(d => devicesMap.set(d.device_id, d));

        rebuildDepartmentFilter();
        updateStats();
        renderScreenWall();
        if (currentMainTab === 'employees') renderEmployeeTable();
    } catch (err) {
        console.error('[API] fetchDevices failed:', err);
    }
}

// ── REST: Delete device ─────────────────────────────────────────────────
async function deleteDevice(event, deviceId, computerName) {
    event.stopPropagation();
    if (!confirm(`Remove "${computerName}" from the dashboard?\nThe agent on that machine will not be uninstalled.`)) return;
    try {
        const res = await fetch(`/api/v1/admin/devices/${deviceId}`, { method: 'DELETE', headers: getAdminHeaders() });
        if (res.status === 401) { window.location.href = '/'; return; }
        if (res.ok) {
            devicesMap.delete(deviceId);
            selectedDeviceIds.delete(deviceId);
            updateStats();
            rebuildDepartmentFilter();
            renderScreenWall();
            if (currentMainTab === 'employees') renderEmployeeTable();
        }
    } catch (err) {
        console.error('[API] deleteDevice failed:', err);
    }
}

// ── WebSocket ───────────────────────────────────────────────────────────
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const tok = window.ADMIN_AUTH_TOKEN || '';
    const qs  = tok ? `?token=${encodeURIComponent(tok)}` : '';
    const wsUrl = `${protocol}//${window.location.host}/ws/dashboard${qs}`;

    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        clearTimeout(reconnectTimer);
        setWsStatus('connected');
    };

    ws.onmessage = (event) => {
        try { handleWsMessage(JSON.parse(event.data)); } catch (err) { console.error('[WS] parse error:', err); }
    };

    ws.onclose = () => {
        setWsStatus('reconnecting');
        reconnectTimer = setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = () => ws.close();
}

function setWsStatus(state) {
    const el = document.getElementById('wsConnectionStatus');
    if (!el) return;
    const configs = {
        connected:    { dot: 'bg-emerald-400',  text: 'Live',         cls: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300' },
        reconnecting: { dot: 'bg-rose-400 animate-pulse', text: 'Reconnecting', cls: 'bg-rose-500/10 border-rose-500/30 text-rose-300' },
        connecting:   { dot: 'bg-amber-400 animate-pulse', text: 'Connecting',  cls: 'bg-amber-500/10 border-amber-500/30 text-amber-300' }
    };
    const cfg = configs[state] || configs.connecting;
    el.className = `flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium border ${cfg.cls}`;
    el.innerHTML = `<span class="w-2 h-2 rounded-full ${cfg.dot}"></span><span>${cfg.text}</span>`;
}

// ── WebSocket message handler ───────────────────────────────────────────
function handleWsMessage(msg) {
    switch (msg.type) {
        case 'SCREENSHOT_UPDATED':  handleScreenshotUpdate(msg); break;
        case 'DEVICE_STATUS':       handleDeviceStatusChange(msg); break;
        case 'LIVE_FRAME':          handleLiveFrame(msg); break;
        case 'SIGNAL_OFFER':        handleWebRtcOffer(msg); break;
        case 'SIGNAL_ICE':          handleWebRtcIce(msg); break;
        case 'STREAM_STOPPED':
            if (activeStreamDeviceId === msg.device_id) {
                showStreamError(`Live stream stopped: ${msg.reason || 'Closed by server'}`);
                closeDetailModal();
            }
            break;
        case 'STREAM_AGENT_OFFLINE':
            if (activeStreamDeviceId === msg.device_id) {
                showStreamError('Agent went offline');
                closeDetailModal();
            }
            break;
        case 'DEVICE_REMOVED':
            devicesMap.delete(msg.device_id);
            selectedDeviceIds.delete(msg.device_id);
            updateStats();
            rebuildDepartmentFilter();
            renderScreenWall();
            if (currentMainTab === 'employees') renderEmployeeTable();
            break;
    }
}

function showStreamError(msg) {
    const overlay = document.getElementById('streamLoadingOverlay');
    if (overlay) {
        overlay.innerHTML = `
            <div class="text-rose-400 text-4xl mb-2">⚠</div>
            <p class="text-sm font-semibold text-rose-300">${escapeHtml(msg)}</p>
        `;
        overlay.classList.remove('hidden');
        overlay.classList.add('flex');
    }
}

function handleScreenshotUpdate(msg) {
    const dev = devicesMap.get(msg.device_id);
    if (dev) {
        dev.latest_screenshot_url = msg.screenshot_url;
        dev.last_seen_at = msg.last_seen;
        dev.status = 'online';
    } else {
        devicesMap.set(msg.device_id, {
            device_id: msg.device_id,
            employee_name: msg.employee_name || 'Unknown',
            computer_name: msg.computer_name || 'Unknown',
            status: 'online',
            current_mode: 'screenshot',
            last_seen_at: msg.last_seen,
            latest_screenshot_url: msg.screenshot_url
        });
        rebuildDepartmentFilter();
        renderScreenWall();
    }
    updateStats();
    updateTileImage(msg.device_id, msg.screenshot_url, msg.last_seen);
    if (currentMainTab === 'employees') renderEmployeeTable();
}

function handleDeviceStatusChange(msg) {
    const dev = devicesMap.get(msg.device_id);
    if (dev) {
        dev.status = msg.status;
        if (msg.status === 'offline') dev.current_mode = 'screenshot';
        updateStats();
        renderScreenWall();
        if (currentMainTab === 'employees') renderEmployeeTable();
    }
}

function handleLiveFrame(msg) {
    if (msg.device_id !== activeStreamDeviceId) return;
    const imgEl   = document.getElementById('detailScreenshotImg');
    const overlay = document.getElementById('streamLoadingOverlay');
    const hudLat  = document.getElementById('hudLatency');
    const hudTs   = document.getElementById('hudTimestamp');

    if (overlay) { overlay.classList.add('hidden'); overlay.classList.remove('flex'); }
    if (imgEl && msg.frame) {
        imgEl.src = `data:image/jpeg;base64,${msg.frame}`;
        frameCount++;
        if (msg.timestamp) {
            const lat = Math.max(10, Math.round(performance.now() - msg.timestamp));
            if (hudLat) hudLat.textContent = `${lat}ms`;
        }
        if (hudTs) hudTs.textContent = new Date().toLocaleTimeString();
    }
}

// ── WebRTC ─────────────────────────────────────────────────────────────
async function handleWebRtcOffer(msg) {
    if (!activeStreamDeviceId || msg.device_id !== activeStreamDeviceId) return;
    try {
        if (!pc) {
            pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
            pc.ontrack = (e) => {
                const video   = document.getElementById('liveVideoPlayer');
                const imgEl   = document.getElementById('detailScreenshotImg');
                const overlay = document.getElementById('streamLoadingOverlay');
                if (video && e.streams[0]) {
                    video.srcObject = e.streams[0];
                    video.classList.remove('hidden');
                    if (imgEl) imgEl.classList.add('hidden');
                    if (overlay) { overlay.classList.add('hidden'); overlay.classList.remove('flex'); }
                }
            };
            pc.onicecandidate = (e) => {
                if (e.candidate && ws && ws.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: 'SIGNAL_ICE', device_id: activeStreamDeviceId, candidate: e.candidate }));
                }
            };
        }
        await pc.setRemoteDescription(new RTCSessionDescription({ type: 'offer', sdp: msg.sdp }));
        const answer = await pc.createAnswer();
        await pc.setLocalDescription(answer);
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'SIGNAL_ANSWER', device_id: activeStreamDeviceId, sdp: answer.sdp }));
        }
    } catch (err) {
        console.warn('[WebRTC] Offer handling failed, falling back to frame stream:', err);
    }
}

async function handleWebRtcIce(msg) {
    if (pc && msg.candidate) {
        try { await pc.addIceCandidate(new RTCIceCandidate(msg.candidate)); } catch (err) { console.error('[WebRTC] ICE error:', err); }
    }
}

// ── Department filter ───────────────────────────────────────────────────
function rebuildDepartmentFilter() {
    const bar = document.getElementById('deptFilterBar');
    if (!bar) return;

    const depts = new Set(['All']);
    devicesMap.forEach(d => {
        const dept = d.department || d.department_name || null;
        if (dept) depts.add(dept);
    });

    // Keep existing All button, add/update dept buttons
    bar.innerHTML = '';

    depts.forEach(dept => {
        const isActive = dept === currentDepartment;
        const btn = document.createElement('button');
        btn.id = `dept-${dept.replace(/\s+/g, '_')}`;
        btn.textContent = dept === 'All' ? 'All Departments' : dept;
        btn.onclick = () => setDepartment(dept);
        btn.className = `dept-btn px-3 py-1 rounded-lg text-xs font-medium border border-transparent transition ${isActive ? 'dept-active' : 'text-slate-400 hover:text-white hover:bg-slate-800'}`;
        bar.appendChild(btn);
    });
}

function setDepartment(dept) {
    currentDepartment = dept;
    // Update dept button styles
    document.querySelectorAll('.dept-btn').forEach(btn => {
        btn.classList.remove('dept-active');
        btn.classList.add('text-slate-400');
        btn.classList.remove('text-white');
    });
    const activeId = `dept-${dept.replace(/\s+/g, '_')}`;
    const activeBtn = document.getElementById(activeId);
    if (activeBtn) {
        activeBtn.classList.add('dept-active');
        activeBtn.classList.remove('text-slate-400');
    }
    renderScreenWall();
}

// ── Status filter ───────────────────────────────────────────────────────
function setFilter(filter) {
    currentFilter = filter;
    document.querySelectorAll('.filter-chip').forEach(btn => {
        btn.classList.remove('chip-active');
        btn.classList.add('text-slate-400');
        btn.classList.remove('bg-emerald-500', 'text-slate-950', 'border-transparent');
    });
    const activeChip = document.getElementById(
        filter === 'all' ? 'filterAll' : (filter === 'online' ? 'filterOnline' : 'filterOffline')
    );
    if (activeChip) {
        activeChip.classList.add('chip-active');
        activeChip.classList.remove('text-slate-400');
    }
    renderScreenWall();
}

// ── Grid size ───────────────────────────────────────────────────────────
function setGrid(size, silent = false) {
    currentGridSize = size;
    const colMap = { 2: 'grid-cols-2', 4: 'grid-cols-2 md:grid-cols-4', 6: 'grid-cols-2 md:grid-cols-3 xl:grid-cols-6', 8: 'grid-cols-2 md:grid-cols-4 xl:grid-cols-8' };
    const grid = document.getElementById('screenWallGrid');
    if (grid) {
        grid.className = `grid gap-4 ${colMap[size] || 'grid-cols-4'}`;
    }
    document.querySelectorAll('.grid-btn').forEach(btn => {
        btn.classList.remove('bg-indigo-500/20', 'text-indigo-300', 'border', 'border-indigo-500/40');
        btn.classList.add('text-slate-500');
    });
    const activeGridBtn = document.getElementById(`grid-${size}`);
    if (activeGridBtn) {
        activeGridBtn.classList.add('bg-indigo-500/20', 'text-indigo-300', 'border', 'border-indigo-500/40');
        activeGridBtn.classList.remove('text-slate-500');
    }
    if (!silent) renderScreenWall();
}

// ── Selection ───────────────────────────────────────────────────────────
function toggleTileSelection(event, deviceId) {
    event.stopPropagation();
    if (selectedDeviceIds.has(deviceId)) {
        selectedDeviceIds.delete(deviceId);
    } else {
        selectedDeviceIds.add(deviceId);
    }
    updateSelectionBar();
    // Update just this tile's visual state without full re-render
    const tile = document.getElementById(`tile-${deviceId}`);
    if (tile) {
        if (selectedDeviceIds.has(deviceId)) {
            tile.classList.add('tile-selected');
        } else {
            tile.classList.remove('tile-selected');
        }
        const cb = tile.querySelector('.tile-checkbox');
        if (cb) {
            if (selectedDeviceIds.has(deviceId)) {
                cb.classList.add('bg-emerald-500', '!border-emerald-400');
                cb.classList.remove('border-slate-600');
            } else {
                cb.classList.remove('bg-emerald-500', '!border-emerald-400');
                cb.classList.add('border-slate-600');
            }
        }
    }
}

function clearSelection() {
    selectedDeviceIds.clear();
    updateSelectionBar();
    renderScreenWall();
}

function selectAllVisible() {
    getFilteredDevices().forEach(d => selectedDeviceIds.add(d.device_id));
    updateSelectionBar();
    renderScreenWall();
}

function updateSelectionBar() {
    const bar   = document.getElementById('selectionBar');
    const count = document.getElementById('selectionCount');
    if (!bar || !count) return;
    if (selectedDeviceIds.size > 0) {
        bar.classList.remove('hidden');
        bar.classList.add('flex');
        count.textContent = `${selectedDeviceIds.size} selected`;
    } else {
        bar.classList.add('hidden');
        bar.classList.remove('flex');
    }
}

// ── Filtered device list ────────────────────────────────────────────────
function getFilteredDevices() {
    let devices = Array.from(devicesMap.values());

    // Status filter
    if (currentFilter === 'online')  devices = devices.filter(d => d.status === 'online');
    if (currentFilter === 'offline') devices = devices.filter(d => d.status !== 'online');

    // Department filter
    if (currentDepartment !== 'All') {
        devices = devices.filter(d => {
            const dept = d.department || d.department_name || null;
            return dept === currentDepartment;
        });
    }

    // If any selected, show only selected (when in wall view)
    if (selectedDeviceIds.size > 0) {
        devices = devices.filter(d => selectedDeviceIds.has(d.device_id));
    }

    return devices;
}

// ── Render Screen Wall ──────────────────────────────────────────────────
function renderScreenWall() {
    const grid       = document.getElementById('screenWallGrid');
    const emptyState = document.getElementById('emptyState');
    const badge      = document.getElementById('wallBadgeCount');
    if (!grid) return;

    const devices = getFilteredDevices();
    if (badge) badge.textContent = devices.length;

    if (devices.length === 0) {
        grid.innerHTML = '';
        if (emptyState) { emptyState.classList.remove('hidden'); emptyState.classList.add('flex'); }
        return;
    }
    if (emptyState) { emptyState.classList.add('hidden'); emptyState.classList.remove('flex'); }

    grid.innerHTML = devices.map(dev => buildTileHtml(dev)).join('');
}

function buildTileHtml(dev) {
    const isOnline    = dev.status === 'online';
    const isStreaming = dev.device_id === activeStreamDeviceId;
    const isSelected  = selectedDeviceIds.has(dev.device_id);

    const statusLabel = isStreaming ? 'STREAMING' : (isOnline ? 'ONLINE' : 'OFFLINE');
    const statusColor = isStreaming
        ? 'bg-sky-500/20 text-sky-300 border-sky-500/40'
        : (isOnline ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40' : 'bg-slate-800 text-slate-500 border-slate-700');
    const dotColor = isStreaming ? 'bg-sky-400 animate-pulse' : (isOnline ? 'bg-emerald-400' : 'bg-slate-600');

    const noScreenSvg = `data:image/svg+xml;charset=UTF-8,%3Csvg%20width%3D%22400%22%20height%3D%22225%22%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%3E%3Crect%20width%3D%22100%25%22%20height%3D%22100%25%22%20fill%3D%22%230f172a%22%2F%3E%3Ctext%20x%3D%2250%25%22%20y%3D%2250%25%22%20fill%3D%22%23334155%22%20font-size%3D%2213%22%20text-anchor%3D%22middle%22%20dy%3D%22.3em%22%20font-family%3D%22sans-serif%22%3ENo%20Screenshot%3C%2Ftext%3E%3C%2Fsvg%3E`;
    const screenshotSrc = dev.latest_screenshot_url ? appendAdminTokenQuery(dev.latest_screenshot_url) : noScreenSvg;
    const imgFilter = isOnline ? '' : 'opacity-30 grayscale';
    const lastSeen  = formatRelativeTime(dev.last_seen_at);
    const dept      = dev.department || dev.department_name || '';

    return `
    <div id="tile-${dev.device_id}"
         class="screen-tile group relative bg-slate-900 border ${isSelected ? 'border-emerald-500 ring-2 ring-emerald-500/40' : 'border-slate-800 hover:border-slate-600'} rounded-xl overflow-hidden cursor-pointer flex flex-col shadow-lg transition-all duration-150 ${isSelected ? 'tile-selected' : ''}"
         onclick="openDetailModal('${dev.device_id}')">

        <!-- Selection checkbox -->
        <div class="tile-checkbox ${isSelected ? 'bg-emerald-500 !border-emerald-400' : 'border-slate-600'}"
             onclick="toggleTileSelection(event, '${dev.device_id}')"
             title="Select for grouped view">
            <svg class="w-2.5 h-2.5 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24" style="width:10px;height:10px;">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="3" d="M5 13l4 4L19 7"/>
            </svg>
        </div>

        <!-- Screenshot -->
        <div class="aspect-16-9 bg-slate-950 relative overflow-hidden">
            <img id="img-${dev.device_id}"
                 src="${screenshotSrc}"
                 alt="${escapeHtml(dev.employee_name)}"
                 loading="lazy"
                 class="w-full h-full object-cover transition-transform duration-300 group-hover:scale-[1.03] ${imgFilter}">

            <!-- Status badge -->
            <div class="absolute top-2 right-2 flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold border backdrop-blur-md ${statusColor}">
                <span class="w-1.5 h-1.5 rounded-full ${dotColor}"></span>
                ${statusLabel}
            </div>

            <!-- Hover CTA -->
            <div class="tile-hover-cta absolute inset-0 flex items-center justify-center bg-slate-950/60">
                <div class="flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-emerald-500 text-slate-950 text-xs font-black shadow-xl">
                    <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
                    OPEN LIVE STREAM
                </div>
            </div>

            <!-- Delete -->
            <button onclick="deleteDevice(event, '${dev.device_id}', '${escapeHtml(dev.computer_name)}')"
                    title="Remove device"
                    class="absolute bottom-2 right-2 z-20 w-6 h-6 rounded-lg bg-slate-900/80 hover:bg-rose-500 text-slate-400 hover:text-white border border-slate-700 flex items-center justify-center transition opacity-0 group-hover:opacity-100">
                <svg class="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
            </button>
        </div>

        <!-- Info bar -->
        <div class="px-3 py-2.5 flex items-start justify-between gap-2 bg-slate-900 border-t border-slate-800/80">
            <div class="min-w-0">
                <p class="text-sm font-semibold text-white truncate leading-tight group-hover:text-emerald-400 transition-colors">${escapeHtml(dev.employee_name)}</p>
                <p class="text-[10px] font-mono text-slate-500 truncate mt-0.5">${escapeHtml(dev.computer_name)}${dept ? ` · <span class="text-indigo-400">${escapeHtml(dept)}</span>` : ''}</p>
            </div>
            <div class="text-right shrink-0">
                <span id="time-${dev.device_id}" class="text-[10px] font-mono text-slate-600">${lastSeen}</span>
            </div>
        </div>
    </div>`;
}

// ── Partial tile update (no full re-render on screenshot) ───────────────
function updateTileImage(deviceId, screenshotUrl, lastSeen) {
    const authUrl = appendAdminTokenQuery(screenshotUrl);
    const imgEl   = document.getElementById(`img-${deviceId}`);
    const timeEl  = document.getElementById(`time-${deviceId}`);
    if (imgEl) { imgEl.src = authUrl; imgEl.classList.remove('opacity-30', 'grayscale'); }
    if (timeEl) timeEl.textContent = formatRelativeTime(lastSeen);

    if (activeStreamDeviceId === deviceId) {
        const detailImg = document.getElementById('detailScreenshotImg');
        const overlay   = document.getElementById('streamLoadingOverlay');
        const hudTs     = document.getElementById('hudTimestamp');
        if (overlay) { overlay.classList.add('hidden'); overlay.classList.remove('flex'); }
        if (detailImg) detailImg.src = authUrl;
        if (hudTs) hudTs.textContent = new Date().toLocaleTimeString();
        frameCount++;
    }
}

// ── Employee Table (List view) ──────────────────────────────────────────
function renderEmployeeTable() {
    const tbody = document.getElementById('employeeTableBody');
    if (!tbody) return;

    const devices = Array.from(devicesMap.values());
    if (devices.length === 0) {
        tbody.innerHTML = `
            <tr><td colspan="6" class="px-5 py-10 text-center text-sm text-slate-500">No devices registered yet.</td></tr>`;
        return;
    }

    tbody.innerHTML = devices.map(dev => {
        const isOnline   = dev.status === 'online';
        const dept       = dev.department || dev.department_name || '—';
        const statusHtml = isOnline
            ? `<span class="flex items-center gap-1.5 text-emerald-400"><span class="w-2 h-2 rounded-full bg-emerald-400"></span>Online</span>`
            : `<span class="flex items-center gap-1.5 text-slate-500"><span class="w-2 h-2 rounded-full bg-slate-600"></span>Offline</span>`;
        return `
        <tr class="hover:bg-slate-800/50 transition cursor-pointer" onclick="switchMainTab('wall'); setTimeout(()=>openDetailModal('${dev.device_id}'),100);">
            <td class="px-5 py-3.5">
                <div>
                    <p class="text-sm font-semibold text-white">${escapeHtml(dev.employee_name)}</p>
                    <p class="text-[11px] text-indigo-400">${escapeHtml(dept)}</p>
                </div>
            </td>
            <td class="px-5 py-3.5 hidden md:table-cell">
                <p class="text-xs font-mono text-slate-400">${escapeHtml(dev.computer_name)}</p>
                <p class="text-[10px] text-slate-600">${escapeHtml(dev.os_version || '')}</p>
            </td>
            <td class="px-5 py-3.5 hidden lg:table-cell">
                <span class="text-xs font-mono text-slate-400">${escapeHtml(dev.ip_address || '—')}</span>
            </td>
            <td class="px-5 py-3.5 text-xs">${statusHtml}</td>
            <td class="px-5 py-3.5 hidden lg:table-cell">
                <span id="list-time-${dev.device_id}" class="text-xs font-mono text-slate-500">${formatRelativeTime(dev.last_seen_at)}</span>
            </td>
            <td class="px-5 py-3.5 text-right">
                <div class="flex items-center justify-end gap-2">
                    <button onclick="event.stopPropagation(); switchMainTab('wall'); setTimeout(()=>openDetailModal('${dev.device_id}'),100);"
                            class="px-2.5 py-1 rounded-lg text-xs font-semibold bg-sky-500/15 text-sky-300 border border-sky-500/30 hover:bg-sky-500/25 transition">
                        Live View
                    </button>
                    <button onclick="deleteDevice(event, '${dev.device_id}', '${escapeHtml(dev.computer_name)}')"
                            class="px-2.5 py-1 rounded-lg text-xs font-medium bg-rose-500/10 text-rose-400 border border-rose-500/20 hover:bg-rose-500/20 transition">
                        Remove
                    </button>
                </div>
            </td>
        </tr>`;
    }).join('');
}

// ── Detail Modal (Live Stream) ──────────────────────────────────────────
function openDetailModal(deviceId) {
    const dev = devicesMap.get(deviceId);
    if (!dev) return;

    activeStreamDeviceId = deviceId;
    dev.current_mode = 'streaming';

    const modal   = document.getElementById('detailModal');
    const overlay = document.getElementById('streamLoadingOverlay');
    const imgEl   = document.getElementById('detailScreenshotImg');
    const video   = document.getElementById('liveVideoPlayer');
    const dot     = document.getElementById('modalStatusDot');

    document.getElementById('modalEmployeeName').textContent = dev.employee_name || '—';
    document.getElementById('modalComputerName').textContent = dev.computer_name || '—';
    document.getElementById('modalIp').textContent = `IP: ${dev.ip_address || '—'}`;
    document.getElementById('modalOs').textContent = dev.os_version || 'Windows';

    if (dot) dot.className = `w-3 h-3 rounded-full ${dev.status === 'online' ? 'bg-emerald-400' : 'bg-slate-500'}`;

    if (imgEl) {
        imgEl.classList.remove('hidden');
        imgEl.src = dev.latest_screenshot_url ? appendAdminTokenQuery(dev.latest_screenshot_url) : '';
    }
    if (video) { video.classList.add('hidden'); video.srcObject = null; }
    if (overlay) { overlay.classList.add('hidden'); overlay.classList.remove('flex'); }

    modal.classList.remove('hidden');
    modal.classList.add('flex');

    // Send REQUEST_STREAM over WS
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'REQUEST_STREAM', device_id: deviceId }));
    }

    // FPS counter
    frameCount = 0;
    lastFrameTime = performance.now();
    clearInterval(fpsInterval);
    fpsInterval = setInterval(() => {
        const hudFps = document.getElementById('hudFps');
        if (hudFps) hudFps.textContent = frameCount;
        frameCount = 0;
    }, 1000);

    // Update tile to show streaming state
    renderScreenWall();
}

function closeDetailModal() {
    if (!activeStreamDeviceId) return;
    const deviceId = activeStreamDeviceId;
    activeStreamDeviceId = null;

    const dev = devicesMap.get(deviceId);
    if (dev) dev.current_mode = 'screenshot';

    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'STOP_STREAM', device_id: deviceId }));
    }

    if (pc) { pc.close(); pc = null; }
    clearInterval(fpsInterval);

    const modal = document.getElementById('detailModal');
    const video = document.getElementById('liveVideoPlayer');
    if (video) { video.srcObject = null; video.classList.add('hidden'); }
    if (modal) { modal.classList.add('hidden'); modal.classList.remove('flex'); }

    document.getElementById('hudFps').textContent = '—';
    document.getElementById('hudLatency').textContent = '—';
    document.getElementById('hudTimestamp').textContent = '—';

    updateStats();
    renderScreenWall();
}

// ── Stats ───────────────────────────────────────────────────────────────
function updateStats() {
    const devices = Array.from(devicesMap.values());
    const total   = devices.length;
    const online  = devices.filter(d => d.status === 'online').length;
    const offline = total - online;
    const streams = devices.filter(d => d.current_mode === 'streaming').length;

    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set('statTotalDevices', total);
    set('statOnlineDevices', online);
    set('statOfflineDevices', offline);
    set('statActiveStreams', streams);
}

// ── Time helpers ────────────────────────────────────────────────────────
function formatRelativeTime(dateString) {
    if (!dateString) return 'Never';
    let ds = String(dateString).trim();
    if (ds.includes(' ') && !ds.includes('T')) ds = ds.replace(' ', 'T');
    if (!ds.endsWith('Z') && !ds.includes('+')) ds += 'Z';
    const date = new Date(ds);
    if (isNaN(date.getTime())) return 'Just now';
    const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    if (seconds < 5)  return 'Just now';
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    return `${hours}h ago`;
}

function startRelativeTimeTicker() {
    setInterval(() => {
        devicesMap.forEach((dev, deviceId) => {
            const wallTime = document.getElementById(`time-${deviceId}`);
            const listTime = document.getElementById(`list-time-${deviceId}`);
            const t = formatRelativeTime(dev.last_seen_at);
            if (wallTime) wallTime.textContent = t;
            if (listTime) listTime.textContent = t;
        });
    }, 2000);
}

// ── Escape util ─────────────────────────────────────────────────────────
function escapeHtml(str) {
    if (!str) return '';
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
