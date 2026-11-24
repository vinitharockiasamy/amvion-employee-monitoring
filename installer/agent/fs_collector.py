import os
import ctypes
import string
import asyncio
import hashlib
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from agent.config import config as default_config, get_system_metadata


IGNORED_FILENAMES: Set[str] = {
    "desktop.ini",
    "thumbs.db",
    ".ds_store",
    "ntuser.dat",
    "agent_state.json",
    "agent.log",
    "server_url.txt",
    "indexervolumeguid",
    "wpSettings.dat".lower(),
}

IGNORED_EXTENSIONS: Set[str] = {
    ".tmp",
    ".temp",
    ".crdownload",
    ".part",
    ".partial",
    ".swp",
    ".lock",
}

IGNORED_DIR_NAMES: Set[str] = {
    "__pycache__",
    ".git",
    "$recycle.bin",
    "system volume information",
    ".trashes",
    ".spotlight-v100",
    ".fseventsd",
}

MAX_HASH_SIZE_BYTES = 512 * 1024 * 1024  # 512 MB safety ceiling for local hashing
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3

USB_CHANNEL_NAME = "USB/removable_storage"
LOCAL_CHANNEL_NAME = "Local Disk"


@dataclass
class RemovableVolumeInfo:
    mount_point: str
    label: str = ""
    filesystem: str = ""

    @property
    def normalized_key(self) -> str:
        p = os.path.abspath(self.mount_point)
        if not p.endswith(os.sep):
            p += os.sep
        return os.path.normcase(p)

    @property
    def display_name(self) -> str:
        drive_clean = self.mount_point.rstrip("\\/")
        if self.label:
            return f"{self.label} ({drive_clean})"
        return drive_clean


@dataclass
class FileSnapshot:
    path: str
    key: str
    name: str
    extension: str
    size: int
    mtime_ns: int
    ino: int
    dev: int
    sha256: Optional[str] = None
    blob_id: Optional[str] = None
    removable_root_key: Optional[str] = None


@dataclass
class DetectedFileChange:
    event_type: str  # CREATED, MODIFIED, RENAMED, MOVED, COPIED, DELETED
    file_name: str
    file_extension: str
    source_path: Optional[str]
    destination_path: Optional[str]
    file_size: Optional[int]
    mtime_ns: int
    target_path: Optional[str]
    known_sha256: Optional[str] = None
    known_blob_id: Optional[str] = None
    removable_volume: Optional[str] = None
    volume_label: Optional[str] = None


def _list_windows_drive_roots() -> List[str]:
    """Return currently mounted Windows drive root strings (e.g. ['C:\\', 'D:\\']) without hardcoded letters."""
    if hasattr(os, "listdrives"):
        try:
            return list(os.listdrives())
        except Exception:
            pass
    if os.name == "nt":
        try:
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            roots = []
            for idx, letter in enumerate(string.ascii_uppercase):
                if bitmask & (1 << idx):
                    roots.append(f"{letter}:\\")
            return roots
        except Exception:
            return []
    return []


def _query_windows_volume_details(root_path: str) -> Tuple[int, str, str]:
    """Return (drive_type, volume_label, filesystem_name) using standard Win32 volume APIs."""
    if os.name != "nt":
        return 0, "", ""
    try:
        kernel32 = ctypes.windll.kernel32
        drive_type = int(kernel32.GetDriveTypeW(ctypes.c_wchar_p(root_path)))
        vol_buf = ctypes.create_unicode_buffer(261)
        fs_buf = ctypes.create_unicode_buffer(261)
        ok = kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root_path),
            vol_buf,
            ctypes.sizeof(vol_buf),
            None,
            None,
            None,
            fs_buf,
            ctypes.sizeof(fs_buf),
        )
        if ok:
            return drive_type, vol_buf.value.strip(), fs_buf.value.strip()
        return drive_type, "", ""
    except Exception:
        return 0, "", ""


class WindowsRemovableVolumeDetector:
    """
    Dynamically discovers mounted USB / removable storage volumes on Windows.
    Detects:
      1. All mounted DRIVE_REMOVABLE (type 2) volumes.
      2. Hot-plugged external DRIVE_FIXED (type 3) USB HDD/SSD volumes that mount after baseline init.
    """

    def __init__(self):
        self._baseline_fixed_roots: Optional[Set[str]] = None
        self._sys_drive = (os.environ.get("SystemDrive", "C:") + "\\").lower()

    def discover(self) -> List[RemovableVolumeInfo]:
        if os.name != "nt":
            return []

        roots = _list_windows_drive_roots()
        if self._baseline_fixed_roots is None:
            fixed_init: Set[str] = {self._sys_drive}
            for r in roots:
                dtype, _, _ = _query_windows_volume_details(r)
                if dtype == DRIVE_FIXED:
                    fixed_init.add(os.path.normcase(r))
            self._baseline_fixed_roots = fixed_init

        discovered: List[RemovableVolumeInfo] = []
        for r in roots:
            norm_r = os.path.normcase(r)
            if norm_r == self._sys_drive:
                continue
            dtype, label, fs_name = _query_windows_volume_details(r)
            is_removable = (dtype == DRIVE_REMOVABLE) or (
                dtype == DRIVE_FIXED and norm_r not in self._baseline_fixed_roots
            )
            if is_removable:
                try:
                    if os.path.exists(r):
                        discovered.append(RemovableVolumeInfo(
                            mount_point=r,
                            label=label,
                            filesystem=fs_name,
                        ))
                except OSError:
                    continue
        return discovered


def is_ignored_path(path_obj: Path) -> bool:
    """Filter out obvious OS/editor/browser temporary noise while keeping all user documents."""
    name = path_obj.name
    if not name:
        return True
    lower_name = name.lower()
    if lower_name in IGNORED_FILENAMES:
        return True
    if lower_name.startswith("~$") or lower_name.startswith(".~"):
        return True
    ext = path_obj.suffix.lower()
    if ext in IGNORED_EXTENSIONS:
        return True
    return False


def compute_file_sha256(
    file_path: str,
    chunk_size: int = 1024 * 1024,
    max_bytes: int = MAX_HASH_SIZE_BYTES,
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """
    Stream-compute SHA-256 for a file without loading it wholly into memory.
    Returns (sha256_hex, total_bytes_read, error_reason).
    Never raises on locked, deleted, disconnected, or inaccessible files.
    """
    hasher = hashlib.sha256()
    total_bytes = 0
    try:
        st = os.stat(file_path)
        if st.st_size > max_bytes:
            return None, st.st_size, "exceeds_hash_ceiling"
        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    return None, total_bytes, "exceeds_hash_ceiling"
                hasher.update(chunk)
        return hasher.hexdigest(), total_bytes, None
    except (PermissionError, FileNotFoundError, OSError) as exc:
        return None, None, str(exc)
    except Exception as exc:
        return None, None, str(exc)


def generate_idempotency_key(
    endpoint_id: str,
    event_type: str,
    source_path: Optional[str],
    destination_path: Optional[str],
    file_size: Optional[int],
    mtime_ns: int,
    sha256: Optional[str] = None,
) -> str:
    """
    Generate a deterministic idempotency key from stable event attributes
    so retries do not create duplicate server-side DLP events.
    """
    canonical = "|".join([
        str(endpoint_id or "pending"),
        str(event_type or "").upper(),
        os.path.normcase(source_path) if source_path else "",
        os.path.normcase(destination_path) if destination_path else "",
        str(file_size if file_size is not None else -1),
        str(mtime_ns),
        str(sha256 or ""),
    ])
    digest = hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()
    return f"dlp-{digest[:40]}"


def _path_is_within_prefix(file_path: str, normalized_root_prefix: str) -> bool:
    norm_p = os.path.normcase(os.path.abspath(file_path))
    prefix_no_sep = normalized_root_prefix.rstrip(os.sep)
    return norm_p == prefix_no_sep or norm_p.startswith(normalized_root_prefix)


class FilesystemEventCollector:
    """
    Authorized, transparent filesystem and removable-storage DLP event collector
    for managed Windows endpoints. Monitors configured local directories plus
    dynamically connected USB/removable volumes via a single unified pipeline.
    """

    def __init__(
        self,
        client,
        agent_config=None,
        max_depth: int = 16,
        volume_detector: Optional[Callable[[], List[RemovableVolumeInfo]]] = None,
    ):
        self.client = client
        self.config = agent_config or default_config
        self.max_depth = max_depth
        self.running = False

        self._default_detector = WindowsRemovableVolumeDetector()
        self._volume_detector: Callable[[], List[RemovableVolumeInfo]] = (
            volume_detector if volume_detector is not None else self._default_detector.discover
        )

        self._initialized = False
        self._snapshot: Dict[str, FileSnapshot] = {}
        self._uploaded_blobs: Dict[str, str] = {}  # sha256 -> file_blob_id
        self._active_removable_volumes: Dict[str, RemovableVolumeInfo] = {}  # normalized_key -> info

        max_q = int(self.config.dlp.get("max_queue_size", 500) or 500)
        self._retry_queue: deque = deque(maxlen=max(1, max_q))

        meta = get_system_metadata()
        self._employee_name = meta.get("employee_name", "Employee")

    def stop(self):
        self.running = False

    @property
    def pending_queue_size(self) -> int:
        return len(self._retry_queue)

    @property
    def active_removable_volumes(self) -> List[RemovableVolumeInfo]:
        return list(self._active_removable_volumes.values())

    def get_monitored_roots(self) -> List[Path]:
        """Resolve configured local directories using AgentConfig.resolve_monitored_directories()."""
        try:
            roots = self.config.resolve_monitored_directories()
        except Exception as exc:
            print(f"[DLP] Failed to resolve monitored directories: {exc}")
            return []

        valid_roots: List[Path] = []
        for root in roots:
            try:
                if root.exists() and root.is_dir():
                    valid_roots.append(root)
            except OSError:
                continue
        return valid_roots

    def _match_removable_volume(self, file_path: Optional[str]) -> Optional[RemovableVolumeInfo]:
        if not file_path:
            return None
        for root_key, vol_info in self._active_removable_volumes.items():
            if _path_is_within_prefix(file_path, root_key):
                return vol_info
        return None

    def _classify_event_context(
        self,
        event_type: str,
        source_path: Optional[str],
        destination_path: Optional[str],
    ) -> Tuple[str, str, Optional[RemovableVolumeInfo], Optional[str]]:
        """
        Determine (channel, risk_level, removable_vol_info, transfer_direction)
        without inventing relationships.
        """
        src_vol = self._match_removable_volume(source_path)
        dst_vol = self._match_removable_volume(destination_path)
        vol = dst_vol or src_vol

        if vol is not None:
            direction: Optional[str] = None
            if source_path and destination_path:
                if src_vol is None and dst_vol is not None:
                    direction = "local_to_removable"
                elif src_vol is not None and dst_vol is None:
                    direction = "removable_to_local"
                else:
                    direction = "removable_internal"
            return USB_CHANNEL_NAME, "high", vol, direction

        # Fallback drive check if an explicit non-system drive path was configured
        sys_drive = os.environ.get("SystemDrive", "C:").rstrip("\\/").lower()
        for p in (source_path, destination_path):
            if p:
                drive, _ = os.path.splitdrive(p)
                if drive and sys_drive and drive.lower() != sys_drive:
                    return USB_CHANNEL_NAME, "high", RemovableVolumeInfo(mount_point=drive + "\\"), None

        if event_type in ("DELETED", "MOVED", "COPIED"):
            return LOCAL_CHANNEL_NAME, "medium", None, None
        return LOCAL_CHANNEL_NAME, "low", None, None

    def _scan_root_into(
        self,
        root: Path,
        visited_dirs: Set[Tuple[int, int]],
        out: Dict[str, FileSnapshot],
        removable_root_key: Optional[str] = None,
    ):
        self._walk_dir(root, depth=0, visited_dirs=visited_dirs, out=out, removable_root_key=removable_root_key)

    def _scan_directory_tree(self) -> Tuple[Dict[str, FileSnapshot], Set[str]]:
        """
        Scan all configured local directories and currently active removable volumes.
        Returns (path_snapshot_map, unreachable_removable_keys).
        """
        results: Dict[str, FileSnapshot] = {}
        visited_dirs: Set[Tuple[int, int]] = set()
        unreachable_removable: Set[str] = set()

        for root in self.get_monitored_roots():
            self._scan_root_into(root, visited_dirs, results, removable_root_key=None)

        for rem_key, vol_info in list(self._active_removable_volumes.items()):
            rem_path = Path(vol_info.mount_point)
            try:
                if not rem_path.exists() or not rem_path.is_dir():
                    unreachable_removable.add(rem_key)
                    continue
            except OSError:
                unreachable_removable.add(rem_key)
                continue
            self._scan_root_into(rem_path, visited_dirs, results, removable_root_key=rem_key)

        return results, unreachable_removable

    def _walk_dir(
        self,
        directory: Path,
        depth: int,
        visited_dirs: Set[Tuple[int, int]],
        out: Dict[str, FileSnapshot],
        removable_root_key: Optional[str] = None,
    ):
        if depth > self.max_depth:
            return
        try:
            dir_stat = os.stat(directory)
            if dir_stat.st_ino:
                dir_id = (dir_stat.st_dev, dir_stat.st_ino)
                if dir_id in visited_dirs:
                    return
                visited_dirs.add(dir_id)
        except OSError:
            return

        try:
            with os.scandir(directory) as it:
                for entry in it:
                    try:
                        entry_name_lower = entry.name.lower()
                        if entry.is_dir(follow_symlinks=False):
                            if entry_name_lower in IGNORED_DIR_NAMES or entry_name_lower.startswith("."):
                                continue
                            self._walk_dir(
                                Path(entry.path),
                                depth + 1,
                                visited_dirs,
                                out,
                                removable_root_key=removable_root_key,
                            )
                        elif entry.is_file(follow_symlinks=False):
                            p_obj = Path(entry.path)
                            if is_ignored_path(p_obj):
                                continue
                            try:
                                st = os.stat(entry.path, follow_symlinks=False)
                            except OSError:
                                st = entry.stat(follow_symlinks=False)
                            full_path = os.path.abspath(entry.path)
                            norm_key = os.path.normcase(full_path)
                            out[norm_key] = FileSnapshot(
                                path=full_path,
                                key=norm_key,
                                name=p_obj.name,
                                extension=p_obj.suffix.lower(),
                                size=st.st_size,
                                mtime_ns=st.st_mtime_ns,
                                ino=st.st_ino,
                                dev=st.st_dev,
                                removable_root_key=removable_root_key,
                            )
                    except (PermissionError, FileNotFoundError, OSError):
                        continue
        except (PermissionError, FileNotFoundError, OSError):
            return

    def _discover_volumes_safe(self) -> List[RemovableVolumeInfo]:
        if not self.config.dlp.get("monitor_removable_storage", True):
            return []
        try:
            return list(self._volume_detector() or [])
        except Exception as exc:
            print(f"[DLP] Removable volume discovery warning: {exc}")
            return []

    def initialize_baseline(self) -> int:
        """Capture initial local and connected removable volume state without emitting file flood events."""
        self._active_removable_volumes.clear()
        for vol in self._discover_volumes_safe():
            self._active_removable_volumes[vol.normalized_key] = vol

        snap, _ = self._scan_directory_tree()
        self._snapshot = snap
        self._initialized = True
        return len(self._snapshot)

    def sync_removable_volumes(self) -> Tuple[List[RemovableVolumeInfo], List[RemovableVolumeInfo]]:
        """
        Compare currently mounted removable volumes with registered state.
        Newly connected volumes have their pre-existing files baseline-indexed and start active monitoring.
        Disconnected volumes stop monitoring immediately and purge their cached entries cleanly.
        Returns (newly_connected, newly_disconnected).
        """
        current_vols = self._discover_volumes_safe()
        current_by_key: Dict[str, RemovableVolumeInfo] = {v.normalized_key: v for v in current_vols}

        connected: List[RemovableVolumeInfo] = []
        disconnected: List[RemovableVolumeInfo] = []

        # 1. Handle disconnected volumes first
        for old_key in list(self._active_removable_volumes.keys()):
            if old_key not in current_by_key:
                vol_info = self._active_removable_volumes.pop(old_key)
                disconnected.append(vol_info)
                # Purge snapshot entries belonging to the unplugged volume without false DELETED events
                for snap_k in [
                    k for k, s in self._snapshot.items()
                    if s.removable_root_key == old_key or _path_is_within_prefix(s.path, old_key)
                ]:
                    self._snapshot.pop(snap_k, None)

        # 2. Handle newly connected volumes
        visited_dirs: Set[Tuple[int, int]] = set()
        for new_key, vol_info in current_by_key.items():
            if new_key not in self._active_removable_volumes:
                self._active_removable_volumes[new_key] = vol_info
                connected.append(vol_info)
                # Baseline-index pre-existing files on the newly mounted removable volume
                vol_entries: Dict[str, FileSnapshot] = {}
                self._scan_root_into(
                    Path(vol_info.mount_point),
                    visited_dirs=visited_dirs,
                    out=vol_entries,
                    removable_root_key=new_key,
                )
                self._snapshot.update(vol_entries)

        return connected, disconnected

    async def _emit_volume_lifecycle_event(self, vol: RemovableVolumeInfo, event_type: str):
        """Emit DRIVE_CONNECTED or DRIVE_DISCONNECTED audit event using the unified DLP event pipeline."""
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
        # Round connect/disconnect idempotency bucket to 1 second per mount cycle
        idem_key = generate_idempotency_key(
            endpoint_id=self.config.device_id or "local-endpoint",
            event_type=event_type,
            source_path=vol.mount_point,
            destination_path=None,
            file_size=0,
            mtime_ns=now_ns // 1_000_000_000,
            sha256=None,
        )
        metadata = {
            "removable_volume": vol.mount_point,
            "evidence_status": "not_applicable",
            "file_size": 0,
        }
        if vol.label:
            metadata["volume_label"] = vol.label
            metadata["drive_label"] = vol.label
        if vol.filesystem:
            metadata["filesystem"] = vol.filesystem

        payload = {
            "idempotency_key": idem_key,
            "endpoint_id": self.config.device_id,
            "employee_name": self._employee_name,
            "event_type": event_type,
            "file_name": vol.display_name,
            "file_extension": None,
            "source_path": vol.mount_point,
            "destination_path": None,
            "channel": USB_CHANNEL_NAME,
            "application_name": None,
            "risk_level": "medium" if event_type == "DRIVE_CONNECTED" else "low",
            "file_blob_id": None,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata,
        }
        await self._deliver_or_enqueue({
            "payload": payload,
            "target_path": None,
            "expected_sha256": None,
            "attempts": 0,
        })

    def collect_changes(self) -> List[DetectedFileChange]:
        """
        Compare current filesystem + removable volume state against previous snapshot and return
        detected CREATED, MODIFIED, RENAMED, MOVED, COPIED, and DELETED changes.
        """
        if not self._initialized:
            self.initialize_baseline()
            return []

        current_map, unreachable_rem_keys = self._scan_directory_tree()
        prev_map = dict(self._snapshot)

        # If a removable drive vanished mid-scan before sync_removable_volumes ran, purge cleanly
        if unreachable_rem_keys:
            for rem_key in unreachable_rem_keys:
                self._active_removable_volumes.pop(rem_key, None)
                for k in [
                    pk for pk, ps in prev_map.items()
                    if ps.removable_root_key == rem_key or _path_is_within_prefix(ps.path, rem_key)
                ]:
                    prev_map.pop(k, None)

        prev_keys = set(prev_map.keys())
        curr_keys = set(current_map.keys())

        disappeared_keys = prev_keys - curr_keys
        appeared_keys = curr_keys - prev_keys
        common_keys = prev_keys & curr_keys

        changes: List[DetectedFileChange] = []

        # 1. Match same-volume RENAMED and MOVED via unambiguous 1-to-1 (dev, ino)
        disappeared_by_ino: Dict[Tuple[int, int], List[str]] = {}
        for k in disappeared_keys:
            snap = prev_map[k]
            if snap.ino > 0:
                disappeared_by_ino.setdefault((snap.dev, snap.ino), []).append(k)

        appeared_by_ino: Dict[Tuple[int, int], List[str]] = {}
        for k in appeared_keys:
            snap = current_map[k]
            if snap.ino > 0:
                appeared_by_ino.setdefault((snap.dev, snap.ino), []).append(k)

        matched_old: Set[str] = set()
        matched_new: Set[str] = set()

        for file_id, old_k_list in disappeared_by_ino.items():
            new_k_list = appeared_by_ino.get(file_id, [])
            if len(old_k_list) == 1 and len(new_k_list) == 1:
                old_k = old_k_list[0]
                new_k = new_k_list[0]
                old_snap = prev_map[old_k]
                new_snap = current_map[new_k]

                matched_old.add(old_k)
                matched_new.add(new_k)

                if old_snap.size == new_snap.size and old_snap.mtime_ns == new_snap.mtime_ns:
                    new_snap.sha256 = old_snap.sha256
                    new_snap.blob_id = old_snap.blob_id

                old_parent = os.path.normcase(os.path.dirname(old_snap.path))
                new_parent = os.path.normcase(os.path.dirname(new_snap.path))
                ev_type = "RENAMED" if old_parent == new_parent else "MOVED"

                changes.append(DetectedFileChange(
                    event_type=ev_type,
                    file_name=new_snap.name,
                    file_extension=new_snap.extension,
                    source_path=old_snap.path,
                    destination_path=new_snap.path,
                    file_size=new_snap.size,
                    mtime_ns=new_snap.mtime_ns,
                    target_path=new_snap.path,
                    known_sha256=new_snap.sha256,
                    known_blob_id=new_snap.blob_id,
                ))

        remaining_disappeared = disappeared_keys - matched_old
        remaining_appeared = appeared_keys - matched_new

        # 2. Cross-volume / non-inode MOVED detection (e.g. Local <-> USB removable volume)
        # Across distinct volumes (C:\ NTFS <-> USB FAT32/exFAT), st_dev/st_ino differ.
        # Pair ONLY when unambiguous 1-to-1 match by (size + known SHA-256) OR (same filename + size + preserved mtime).
        if remaining_disappeared and remaining_appeared:
            # First try exact known SHA-256 + size match
            old_with_sha = [
                k for k in remaining_disappeared
                if prev_map[k].sha256 and prev_map[k].size > 0
            ]
            if old_with_sha:
                old_by_sha: Dict[Tuple[int, str], List[str]] = {}
                for k in old_with_sha:
                    s = prev_map[k]
                    old_by_sha.setdefault((s.size, s.sha256), []).append(k)

                candidate_sizes = {prev_map[k].size for k in old_with_sha}
                new_by_sha: Dict[Tuple[int, str], List[str]] = {}
                for k in list(remaining_appeared):
                    s = current_map[k]
                    if s.size in candidate_sizes and s.size > 0:
                        sha, _, err = compute_file_sha256(s.path)
                        if sha and not err:
                            s.sha256 = sha
                            new_by_sha.setdefault((s.size, sha), []).append(k)

                for sig, ok_list in old_by_sha.items():
                    nk_list = new_by_sha.get(sig, [])
                    if len(ok_list) == 1 and len(nk_list) == 1:
                        old_k = ok_list[0]
                        new_k = nk_list[0]
                        old_snap = prev_map[old_k]
                        new_snap = current_map[new_k]
                        remaining_disappeared.discard(old_k)
                        remaining_appeared.discard(new_k)
                        new_snap.blob_id = old_snap.blob_id

                        old_parent = os.path.normcase(os.path.dirname(old_snap.path))
                        new_parent = os.path.normcase(os.path.dirname(new_snap.path))
                        ev_type = "RENAMED" if old_parent == new_parent else "MOVED"

                        changes.append(DetectedFileChange(
                            event_type=ev_type,
                            file_name=new_snap.name,
                            file_extension=new_snap.extension,
                            source_path=old_snap.path,
                            destination_path=new_snap.path,
                            file_size=new_snap.size,
                            mtime_ns=new_snap.mtime_ns,
                            target_path=new_snap.path,
                            known_sha256=new_snap.sha256,
                            known_blob_id=new_snap.blob_id,
                        ))

            # Second, for baseline files (sha256 not yet computed) moved across volumes:
            # match unambiguously by identical normalized filename + non-zero size + preserved mtime (<= 2.1s FAT32 tolerance)
            if remaining_disappeared and remaining_appeared:
                old_by_namesize: Dict[Tuple[str, int], List[str]] = {}
                for k in remaining_disappeared:
                    s = prev_map[k]
                    if s.size > 0:
                        old_by_namesize.setdefault((os.path.normcase(s.name), s.size), []).append(k)

                new_by_namesize: Dict[Tuple[str, int], List[str]] = {}
                for k in remaining_appeared:
                    s = current_map[k]
                    if s.size > 0:
                        new_by_namesize.setdefault((os.path.normcase(s.name), s.size), []).append(k)

                for ns_key, ok_list in old_by_namesize.items():
                    nk_list = new_by_namesize.get(ns_key, [])
                    if len(ok_list) == 1 and len(nk_list) == 1:
                        old_snap = prev_map[ok_list[0]]
                        new_snap = current_map[nk_list[0]]
                        # Require different volumes/roots and preserved modification timestamp within FAT32 2s granularity
                        if (
                            old_snap.removable_root_key != new_snap.removable_root_key
                            and abs(old_snap.mtime_ns - new_snap.mtime_ns) <= 2_100_000_000
                        ):
                            remaining_disappeared.discard(ok_list[0])
                            remaining_appeared.discard(nk_list[0])
                            changes.append(DetectedFileChange(
                                event_type="MOVED",
                                file_name=new_snap.name,
                                file_extension=new_snap.extension,
                                source_path=old_snap.path,
                                destination_path=new_snap.path,
                                file_size=new_snap.size,
                                mtime_ns=new_snap.mtime_ns,
                                target_path=new_snap.path,
                            ))

        # 3. Existing paths: case-only RENAMED and content MODIFIED
        for k in sorted(common_keys):
            old_snap = prev_map[k]
            curr_snap = current_map[k]

            if old_snap.name != curr_snap.name:
                changes.append(DetectedFileChange(
                    event_type="RENAMED",
                    file_name=curr_snap.name,
                    file_extension=curr_snap.extension,
                    source_path=old_snap.path,
                    destination_path=curr_snap.path,
                    file_size=curr_snap.size,
                    mtime_ns=curr_snap.mtime_ns,
                    target_path=curr_snap.path,
                    known_sha256=old_snap.sha256 if (old_snap.size == curr_snap.size and old_snap.mtime_ns == curr_snap.mtime_ns) else None,
                    known_blob_id=old_snap.blob_id if (old_snap.size == curr_snap.size and old_snap.mtime_ns == curr_snap.mtime_ns) else None,
                ))

            if old_snap.size != curr_snap.size or old_snap.mtime_ns != curr_snap.mtime_ns:
                changes.append(DetectedFileChange(
                    event_type="MODIFIED",
                    file_name=curr_snap.name,
                    file_extension=curr_snap.extension,
                    source_path=curr_snap.path,
                    destination_path=None,
                    file_size=curr_snap.size,
                    mtime_ns=curr_snap.mtime_ns,
                    target_path=curr_snap.path,
                ))
            else:
                curr_snap.sha256 = old_snap.sha256
                curr_snap.blob_id = old_snap.blob_id

        # 4. Newly appeared files: check unambiguous cross-tier COPIED (Local <-> Removable) via SHA-256, else CREATED
        for new_k in sorted(remaining_appeared):
            new_snap = current_map[new_k]
            matched_copy_source: Optional[str] = None

            if self._active_removable_volumes and new_snap.size > 0:
                new_is_removable = new_snap.removable_root_key is not None
                opposite_candidates = [
                    s for k_cand, s in current_map.items()
                    if k_cand != new_k
                    and (s.removable_root_key is not None) != new_is_removable
                    and s.size == new_snap.size
                ]
                if 1 <= len(opposite_candidates) <= 5:
                    new_sha = new_snap.sha256
                    if not new_sha:
                        new_sha, _, err = compute_file_sha256(new_snap.path)
                        if new_sha and not err:
                            new_snap.sha256 = new_sha
                    if new_sha:
                        exact_matches: List[FileSnapshot] = []
                        for cand in opposite_candidates:
                            c_sha = cand.sha256
                            if not c_sha:
                                c_sha, _, c_err = compute_file_sha256(cand.path)
                                if c_sha and not c_err:
                                    cand.sha256 = c_sha
                            if c_sha == new_sha:
                                exact_matches.append(cand)
                        if len(exact_matches) == 1:
                            matched_copy_source = exact_matches[0].path
                            if exact_matches[0].blob_id and not new_snap.blob_id:
                                new_snap.blob_id = exact_matches[0].blob_id

            if matched_copy_source:
                changes.append(DetectedFileChange(
                    event_type="COPIED",
                    file_name=new_snap.name,
                    file_extension=new_snap.extension,
                    source_path=matched_copy_source,
                    destination_path=new_snap.path,
                    file_size=new_snap.size,
                    mtime_ns=new_snap.mtime_ns,
                    target_path=new_snap.path,
                    known_sha256=new_snap.sha256,
                    known_blob_id=new_snap.blob_id,
                ))
            else:
                changes.append(DetectedFileChange(
                    event_type="CREATED",
                    file_name=new_snap.name,
                    file_extension=new_snap.extension,
                    source_path=None,
                    destination_path=new_snap.path,
                    file_size=new_snap.size,
                    mtime_ns=new_snap.mtime_ns,
                    target_path=new_snap.path,
                    known_sha256=new_snap.sha256,
                    known_blob_id=new_snap.blob_id,
                ))

        # 5. DELETED files
        for old_k in sorted(remaining_disappeared):
            old_snap = prev_map[old_k]
            changes.append(DetectedFileChange(
                event_type="DELETED",
                file_name=old_snap.name,
                file_extension=old_snap.extension,
                source_path=old_snap.path,
                destination_path=None,
                file_size=old_snap.size,
                mtime_ns=old_snap.mtime_ns,
                target_path=None,
                known_sha256=old_snap.sha256,
                known_blob_id=old_snap.blob_id,
            ))

        self._snapshot = current_map
        return changes

    async def _process_change(self, change: DetectedFileChange):
        """Compute SHA-256 when accessible, upload eligible evidence, and submit or queue the DLP event."""
        dlp_cfg = self.config.dlp or {}
        evidence_enabled = bool(dlp_cfg.get("evidence_enabled", True))
        max_mb = float(dlp_cfg.get("maximum_evidence_size_mb", 25))
        max_evidence_bytes = int(max_mb * 1024 * 1024)
        evidence_events = {str(e).upper() for e in dlp_cfg.get("evidence_events", [])}

        sha256_hex: Optional[str] = change.known_sha256
        actual_size: Optional[int] = change.file_size
        hash_error: Optional[str] = None

        if change.target_path and not sha256_hex:
            sha256_hex, read_bytes, hash_error = await asyncio.to_thread(
                compute_file_sha256,
                change.target_path,
            )
            if read_bytes is not None:
                actual_size = read_bytes

        if change.target_path and sha256_hex:
            norm_k = os.path.normcase(os.path.abspath(change.target_path))
            snap = self._snapshot.get(norm_k)
            if snap:
                snap.sha256 = sha256_hex

        file_blob_id: Optional[str] = None
        evidence_status = "disabled_by_policy"
        needs_deferred_evidence = False

        if evidence_enabled and change.event_type in evidence_events:
            if sha256_hex and sha256_hex in self._uploaded_blobs:
                file_blob_id = self._uploaded_blobs[sha256_hex]
                evidence_status = "archived"
            elif change.known_blob_id:
                file_blob_id = change.known_blob_id
                evidence_status = "archived"
            elif not change.target_path:
                evidence_status = "locked_or_unavailable"
            elif hash_error or sha256_hex is None:
                evidence_status = "locked_or_unavailable"
            elif actual_size is not None and actual_size == 0:
                evidence_status = "empty_file"
            elif actual_size is not None and actual_size > max_evidence_bytes:
                evidence_status = "exceeds_size_limit"
            else:
                up_res = await self.client.upload_dlp_evidence(
                    file_path=change.target_path,
                    expected_sha256=sha256_hex,
                    original_filename=change.file_name,
                )
                status_str = up_res.get("status")
                if status_str == "ok" and up_res.get("file_id"):
                    file_blob_id = up_res["file_id"]
                    self._uploaded_blobs[sha256_hex] = file_blob_id
                    evidence_status = "archived"
                    norm_k = os.path.normcase(os.path.abspath(change.target_path))
                    if norm_k in self._snapshot:
                        self._snapshot[norm_k].blob_id = file_blob_id
                elif status_str in ("retry", "auth_error"):
                    needs_deferred_evidence = True
                    evidence_status = "pending_retry"
                else:
                    evidence_status = "upload_failed"

        endpoint_id = self.config.device_id or ""
        idem_key = generate_idempotency_key(
            endpoint_id=endpoint_id or "local-endpoint",
            event_type=change.event_type,
            source_path=change.source_path,
            destination_path=change.destination_path,
            file_size=actual_size,
            mtime_ns=change.mtime_ns,
            sha256=sha256_hex,
        )

        channel, risk_level, rem_vol, transfer_dir = self._classify_event_context(
            change.event_type,
            change.source_path,
            change.destination_path,
        )

        metadata = {
            "file_size": actual_size if actual_size is not None else 0,
            "evidence_status": evidence_status,
        }
        if sha256_hex:
            metadata["sha256"] = sha256_hex
        if rem_vol is not None:
            metadata["removable_volume"] = rem_vol.mount_point
            if rem_vol.label:
                metadata["volume_label"] = rem_vol.label
                metadata["drive_label"] = rem_vol.label
            if rem_vol.filesystem:
                metadata["filesystem"] = rem_vol.filesystem
        if transfer_dir:
            metadata["transfer_direction"] = transfer_dir
        if hash_error:
            metadata["access_note"] = hash_error[:200]

        payload = {
            "idempotency_key": idem_key,
            "endpoint_id": self.config.device_id,
            "employee_name": self._employee_name,
            "event_type": change.event_type,
            "file_name": change.file_name,
            "file_extension": change.file_extension or None,
            "source_path": change.source_path,
            "destination_path": change.destination_path,
            "channel": channel,
            "application_name": None,
            "risk_level": risk_level,
            "file_blob_id": file_blob_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata,
        }

        queue_item = {
            "payload": payload,
            "target_path": change.target_path if needs_deferred_evidence else None,
            "expected_sha256": sha256_hex if needs_deferred_evidence else None,
            "attempts": 0,
        }

        if needs_deferred_evidence:
            self._retry_queue.append(queue_item)
            return

        await self._deliver_or_enqueue(queue_item)

    async def _deliver_or_enqueue(self, item: dict) -> bool:
        """Attempt delivery of one event item; enqueue on transient failure."""
        payload = item["payload"]
        if self.config.device_id:
            payload["endpoint_id"] = self.config.device_id

        if item.get("target_path") and item.get("expected_sha256") and not payload.get("file_blob_id"):
            up_res = await self.client.upload_dlp_evidence(
                file_path=item["target_path"],
                expected_sha256=item["expected_sha256"],
                original_filename=payload["file_name"],
            )
            if up_res.get("status") == "ok" and up_res.get("file_id"):
                blob_id = up_res["file_id"]
                payload["file_blob_id"] = blob_id
                payload["metadata"]["evidence_status"] = "archived"
                self._uploaded_blobs[item["expected_sha256"]] = blob_id
                item["target_path"] = None
            elif up_res.get("status") in ("retry", "auth_error") and item["attempts"] < 3:
                item["attempts"] += 1
                self._retry_queue.append(item)
                return False
            else:
                payload["metadata"]["evidence_status"] = "upload_failed"
                item["target_path"] = None

        res = await self.client.post_dlp_event(payload)
        status_str = res.get("status")
        if status_str == "ok":
            return True
        if status_str in ("retry", "auth_error"):
            item["attempts"] = item.get("attempts", 0) + 1
            self._retry_queue.append(item)
            return False

        print(f"[DLP] Dropping rejected event ({res.get('code')}): {res.get('detail')}")
        return False

    async def flush_retry_queue(self, max_items: int = 25):
        """Attempt to drain bounded retry queue without blocking indefinitely."""
        count = min(len(self._retry_queue), max_items)
        for _ in range(count):
            if not self._retry_queue:
                break
            item = self._retry_queue.popleft()
            delivered = await self._deliver_or_enqueue(item)
            if not delivered:
                break

    async def run_once(self) -> int:
        """Execute a single volume-sync + filesystem-scan + delivery pass."""
        if not self.config.dlp.get("enabled", True):
            return 0

        try:
            await self.flush_retry_queue()
            if not self._initialized:
                await asyncio.to_thread(self.initialize_baseline)
                return 0

            connected, disconnected = await asyncio.to_thread(self.sync_removable_volumes)
            for vol in connected:
                print(f"[DLP] Removable storage connected: {vol.display_name} -> monitoring started.")
                await self._emit_volume_lifecycle_event(vol, "DRIVE_CONNECTED")
            for vol in disconnected:
                print(f"[DLP] Removable storage disconnected: {vol.display_name} -> monitoring stopped.")
                await self._emit_volume_lifecycle_event(vol, "DRIVE_DISCONNECTED")

            changes = await asyncio.to_thread(self.collect_changes)
            for change in changes:
                try:
                    await self._process_change(change)
                except Exception as item_err:
                    print(f"[DLP] Error processing event for {change.file_name}: {item_err}")
            return len(changes) + len(connected) + len(disconnected)
        except Exception as scan_err:
            print(f"[DLP] Scan cycle error: {scan_err}")
            return 0

    async def run_loop(self):
        """Background async task managed by AgentDaemon."""
        if not self.config.dlp.get("enabled", True):
            print("[DLP] Filesystem DLP collector is disabled by configuration.")
            return

        self.running = True
        try:
            roots = self.get_monitored_roots()
            baseline_count = await asyncio.to_thread(self.initialize_baseline)
            rem_count = len(self._active_removable_volumes)
            print(
                f"[DLP] Filesystem & USB DLP Collector active ({len(roots)} local dirs, "
                f"{rem_count} removable volumes, {baseline_count} baseline files indexed)."
            )
        except Exception as init_err:
            print(f"[DLP] Baseline initialization warning: {init_err}")

        while self.running:
            try:
                poll_sec = float(self.config.dlp.get("poll_interval_sec", 2.0) or 2.0)
                await asyncio.sleep(max(0.5, poll_sec))
                if not self.running:
                    break
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as loop_err:
                print(f"[DLP] Unexpected collector loop error: {loop_err}")
                await asyncio.sleep(2.0)
