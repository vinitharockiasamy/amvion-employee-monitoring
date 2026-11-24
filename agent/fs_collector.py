import os
import stat
import time
import ctypes
import struct
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
    "wpsettings.dat",
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
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
REPARSE_TAGS_TO_SKIP: Set[int] = {
    getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003),
    getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C),
}
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
    ctime_ns: int = 0
    seen_seq: int = 0
    sha256: Optional[str] = None
    blob_id: Optional[str] = None
    removable_root_key: Optional[str] = None
    is_baseline: bool = False
    baseline_timestamp: Optional[str] = None


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
    was_baseline: bool = False


@dataclass
class RecycleBinMatch:
    payload_path: str
    original_path: str
    file_size: int
    sha256: Optional[str] = None
    size_exceeded_ceiling: bool = False


def parse_recycle_bin_metadata_file(i_file_path: str) -> Optional[Tuple[int, int, str]]:
    """
    Parse a Windows $Ixxxxxx metadata header (Version 1 Vista/Win7 or Version 2 Win8/10/11).
    Returns (original_size_bytes, deletion_filetime_100ns, original_full_path) or None.
    """
    try:
        raw = Path(i_file_path).read_bytes()
        if len(raw) < 28:
            return None
        version, orig_size, del_filetime = struct.unpack_from("<QQQ", raw, 0)
        if version == 2:
            char_count = struct.unpack_from("<I", raw, 24)[0]
            byte_len = min(len(raw) - 28, char_count * 2)
            if byte_len <= 0:
                return None
            orig_path = raw[28:28 + byte_len].decode("utf-16-le", errors="replace").rstrip("\x00")
        elif version == 1:
            orig_path = raw[24:544].decode("utf-16-le", errors="replace").split("\x00", 1)[0]
        else:
            return None

        if not orig_path:
            return None
        return int(orig_size), int(del_filetime), orig_path
    except Exception:
        return None


def _iter_recycle_bin_i_files(
    deleted_source_path: str,
    custom_recycle_roots: Optional[List[str]] = None,
    max_candidates: int = 300,
) -> List[Tuple[int, str, str]]:
    """
    Collect (mtime_ns, i_file_path, r_companion_path) from custom test roots or
    the current user's accessible <Drive>:\\$Recycle.Bin\\<SID> directories.
    """
    search_dirs: List[str] = []
    if custom_recycle_roots:
        for r in custom_recycle_roots:
            if not r:
                continue
            rp = Path(r)
            if rp.is_dir():
                search_dirs.append(str(rp))
                try:
                    for sub in os.scandir(rp):
                        if sub.is_dir(follow_symlinks=False):
                            search_dirs.append(sub.path)
                except OSError:
                    pass
    elif os.name == "nt" and deleted_source_path:
        drive, _ = os.path.splitdrive(os.path.abspath(deleted_source_path))
        if drive:
            rb_root = Path(f"{drive}\\$Recycle.Bin")
            try:
                if rb_root.exists():
                    for sid_dir in os.scandir(rb_root):
                        try:
                            if sid_dir.is_dir(follow_symlinks=False):
                                search_dirs.append(sid_dir.path)
                        except OSError:
                            continue
            except OSError:
                pass

    candidates: List[Tuple[int, str, str]] = []
    seen_i: Set[str] = set()
    for sdir in search_dirs:
        try:
            with os.scandir(sdir) as it:
                for entry in it:
                    try:
                        if not entry.name.startswith("$I") or len(entry.name) < 3:
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        norm_i = os.path.normcase(entry.path)
                        if norm_i in seen_i:
                            continue
                        seen_i.add(norm_i)
                        r_companion = os.path.join(sdir, "$R" + entry.name[2:])
                        st = entry.stat(follow_symlinks=False)
                        candidates.append((st.st_mtime_ns, entry.path, r_companion))
                    except OSError:
                        continue
        except (PermissionError, FileNotFoundError, OSError):
            continue

    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[:max_candidates]


def find_in_windows_recycle_bin(
    deleted_source_path: str,
    expected_size: Optional[int] = None,
    expected_sha256: Optional[str] = None,
    custom_recycle_roots: Optional[List[str]] = None,
) -> Optional[RecycleBinMatch]:
    """
    Locate a deleted file inside the Windows Recycle Bin without restoring or moving it.
    Supports both direct file deletion ($Rxxxxxx.ext) and parent-folder deletion ($Rxxxxxx\\subpath).
    Verifies SHA-256 against expected_sha256 when known.
    """
    if not deleted_source_path:
        return None

    target_abs = os.path.abspath(deleted_source_path)
    target_norm = os.path.normcase(target_abs)

    i_candidates = _iter_recycle_bin_i_files(
        deleted_source_path=target_abs,
        custom_recycle_roots=custom_recycle_roots,
    )

    for _mtime_ns, i_path, r_companion in i_candidates:
        parsed = parse_recycle_bin_metadata_file(i_path)
        if not parsed:
            continue
        _meta_size, _del_ft, orig_path = parsed
        orig_abs = os.path.abspath(orig_path)
        orig_norm = os.path.normcase(orig_abs)

        payload_candidate: Optional[Path] = None
        r_obj = Path(r_companion)

        if orig_norm == target_norm and r_obj.is_file():
            payload_candidate = r_obj
        elif target_norm.startswith(orig_norm.rstrip(os.sep) + os.sep) and r_obj.is_dir():
            rel_tail = target_abs[len(orig_abs.rstrip("\\/")):].lstrip("\\/")
            nested = r_obj / rel_tail
            if nested.is_file():
                payload_candidate = nested

        if payload_candidate is None:
            continue

        try:
            st = os.stat(payload_candidate)
        except OSError:
            continue

        if expected_size is not None and expected_size >= 0 and st.st_size != expected_size and expected_sha256:
            # If we know exact baseline hash & size and size differs, keep looking for newer/exact item
            pass

        sha, read_sz, err = compute_file_sha256(str(payload_candidate))
        if err == "exceeds_hash_ceiling":
            return RecycleBinMatch(
                payload_path=str(payload_candidate),
                original_path=orig_abs,
                file_size=st.st_size,
                sha256=expected_sha256,
                size_exceeded_ceiling=True,
            )
        if err or sha is None:
            continue

        if expected_sha256 and sha != expected_sha256:
            # Content differs from baseline SHA-256 (e.g. older deleted item at same path); keep checking
            continue

        return RecycleBinMatch(
            payload_path=str(payload_candidate),
            original_path=orig_abs,
            file_size=read_sz if read_sz is not None else st.st_size,
            sha256=sha,
        )

    return None


def find_in_secondary_backups(
    file_name: str,
    expected_size: Optional[int],
    expected_sha256: Optional[str],
    backup_directories: List[str],
) -> Optional[Tuple[str, int, str]]:
    """
    Optional secondary fallback: check administrator-configured backup/shadow directories
    for a verified SHA-256 copy when primary & Recycle Bin sources are unavailable.
    Never matches by filename alone; requires cryptographic SHA-256 match.
    """
    if not expected_sha256 or not backup_directories:
        return None

    for bdir in backup_directories:
        if not bdir:
            continue
        bpath = Path(bdir)
        if not bpath.is_dir():
            continue
        try:
            for cand in bpath.rglob(file_name):
                try:
                    if not cand.is_file():
                        continue
                    st = cand.stat()
                    if expected_size is not None and st.st_size != expected_size:
                        continue
                    sha, sz, err = compute_file_sha256(str(cand))
                    if not err and sha == expected_sha256:
                        return str(cand), (sz if sz is not None else st.st_size), sha
                except OSError:
                    continue
        except OSError:
            continue
    return None


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
        recycle_bin_roots: Optional[List[str]] = None,
    ):
        self.client = client
        self.config = agent_config or default_config
        self.max_depth = max_depth
        self.running = False
        self._custom_recycle_roots = recycle_bin_roots

        self._default_detector = WindowsRemovableVolumeDetector()
        self._volume_detector: Callable[[], List[RemovableVolumeInfo]] = (
            volume_detector if volume_detector is not None else self._default_detector.discover
        )

        self._initialized = False
        self._snapshot: Dict[str, FileSnapshot] = {}
        self._uploaded_blobs: Dict[str, str] = {}  # sha256 -> file_blob_id
        self._active_removable_volumes: Dict[str, RemovableVolumeInfo] = {}  # normalized_key -> info
        self._deferred_new_files: Dict[str, int] = {}  # norm_key -> deferral count for locked mid-copy files
        self._seen_counter: int = 0

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

    def get_baseline_snapshot(self) -> Dict[str, FileSnapshot]:
        """Expose current indexed snapshot metadata for diagnostics and automated tests."""
        return dict(self._snapshot)

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
        if not file_path or file_path == "Recycle Bin":
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
            if source_path and destination_path and destination_path != "Recycle Bin":
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
            if p and p != "Recycle Bin":
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
        dir_id: Optional[Tuple[int, int]] = None
        try:
            dir_stat = os.stat(directory)
            if dir_stat.st_ino:
                dir_id = (dir_stat.st_dev, dir_stat.st_ino)
                if dir_id in visited_dirs:
                    return
        except OSError:
            return

        try:
            with os.scandir(directory) as it:
                # Only mark directory visited once scandir succeeds so unreadable NTFS compatibility
                # junctions (e.g. Documents\My Pictures in Python 3.11) never poison visited_dirs!
                if dir_id is not None:
                    visited_dirs.add(dir_id)
                for entry in it:
                    try:
                        entry_name_lower = entry.name.lower()
                        if entry.is_symlink():
                            continue
                        try:
                            entry_lstat = entry.stat(follow_symlinks=False)
                        except OSError:
                            entry_lstat = None

                        if entry_lstat is not None:
                            reparse_tag = getattr(entry_lstat, "st_reparse_tag", 0)
                            if reparse_tag in REPARSE_TAGS_TO_SKIP:
                                continue

                        if entry.is_dir(follow_symlinks=False):
                            if entry_name_lower in IGNORED_DIR_NAMES or entry_name_lower.startswith("."):
                                continue
                            # Guard Python 3.11 Windows junction edge case where st_reparse_tag may be 0
                            # on DirEntry.stat() unless os.lstat is checked when FILE_ATTRIBUTE_REPARSE_POINT is set
                            if entry_lstat is not None and (
                                getattr(entry_lstat, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
                            ):
                                try:
                                    l_st = os.lstat(entry.path)
                                    if getattr(l_st, "st_reparse_tag", 0) in REPARSE_TAGS_TO_SKIP:
                                        continue
                                except OSError:
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
                                st = entry_lstat or entry.stat(follow_symlinks=False)
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
                                ctime_ns=getattr(st, "st_ctime_ns", 0),
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

    def _populate_baseline_metadata(self, entries: Dict[str, FileSnapshot]):
        """
        Compute SHA-256 content identity on readable baseline files (up to baseline_hash_max_mb)
        and stamp baseline=True + baseline_timestamp WITHOUT uploading any files to the vault.
        """
        dlp_cfg = self.config.dlp or {}
        hash_max_mb = float(dlp_cfg.get("baseline_hash_max_mb", 100))
        hash_max_bytes = int(hash_max_mb * 1024 * 1024)
        baseline_ts = datetime.now(timezone.utc).isoformat()

        for snap in entries.values():
            snap.is_baseline = True
            snap.baseline_timestamp = baseline_ts
            snap.seen_seq = 0
            if 0 < snap.size <= hash_max_bytes and snap.sha256 is None:
                sha, _, err = compute_file_sha256(snap.path, max_bytes=hash_max_bytes)
                if sha and not err:
                    snap.sha256 = sha
            elif snap.size == 0 and snap.sha256 is None:
                snap.sha256 = hashlib.sha256(b"").hexdigest()

    def initialize_baseline(self) -> int:
        """
        Capture initial local and connected removable volume metadata (including SHA-256 when readable)
        without uploading pre-existing files or emitting flood events.
        """
        self._active_removable_volumes.clear()
        for vol in self._discover_volumes_safe():
            self._active_removable_volumes[vol.normalized_key] = vol

        snap, _ = self._scan_directory_tree()
        self._populate_baseline_metadata(snap)
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

        for old_key in list(self._active_removable_volumes.keys()):
            if old_key not in current_by_key:
                vol_info = self._active_removable_volumes.pop(old_key)
                disconnected.append(vol_info)
                for snap_k in [
                    k for k, s in self._snapshot.items()
                    if s.removable_root_key == old_key or _path_is_within_prefix(s.path, old_key)
                ]:
                    self._snapshot.pop(snap_k, None)

        visited_dirs: Set[Tuple[int, int]] = set()
        for new_key, vol_info in current_by_key.items():
            if new_key not in self._active_removable_volumes:
                self._active_removable_volumes[new_key] = vol_info
                connected.append(vol_info)
                vol_entries: Dict[str, FileSnapshot] = {}
                self._scan_root_into(
                    Path(vol_info.mount_point),
                    visited_dirs=visited_dirs,
                    out=vol_entries,
                    removable_root_key=new_key,
                )
                self._populate_baseline_metadata(vol_entries)
                self._snapshot.update(vol_entries)

        return connected, disconnected

    async def _emit_volume_lifecycle_event(self, vol: RemovableVolumeInfo, event_type: str):
        """Emit DRIVE_CONNECTED or DRIVE_DISCONNECTED audit event using the unified DLP event pipeline."""
        now_ns = int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)
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
            "file_path": vol.mount_point,
            "evidence_status": "NO_EVIDENCE",
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

    def _reconcile_split_move_candidates(
        self,
        current_map: Dict[str, FileSnapshot],
        common_keys: Set[str],
        appeared_keys: Set[str],
        disappeared_keys: Set[str],
    ):
        """
        Guard B: If a source folder was scanned moments BEFORE an Explorer Cut/Move and the destination
        folder was scanned moments AFTER the Paste within the same scan pass, both paths momentarily
        appear in current_map with the same (dev, ino). Re-check disk existence on the old path so
        same-volume MOVED is paired atomically instead of falsely emitting COPIED + DELETED.
        """
        if not appeared_keys or not common_keys:
            return
        common_by_ino: Dict[Tuple[int, int], List[str]] = {}
        for ck in common_keys:
            cs = current_map[ck]
            if cs.ino > 0:
                common_by_ino.setdefault((cs.dev, cs.ino), []).append(ck)

        for nk in list(appeared_keys):
            ns = current_map.get(nk)
            if not ns or ns.ino <= 0:
                continue
            for cand_k in common_by_ino.get((ns.dev, ns.ino), []):
                cand_snap = current_map.get(cand_k)
                if cand_snap and not os.path.exists(cand_snap.path):
                    current_map.pop(cand_k, None)
                    common_keys.discard(cand_k)
                    disappeared_keys.add(cand_k)

    def collect_changes(self) -> List[DetectedFileChange]:
        """
        Compare current filesystem + removable volume state against previous snapshot and return
        detected CREATED, MODIFIED, RENAMED, MOVED, COPIED, and DELETED changes.
        """
        if not self._initialized:
            self.initialize_baseline()
            return []

        dlp_cfg = self.config.dlp or {}
        debug_diag = bool(dlp_cfg.get("debug_diagnostics", False))

        current_map, unreachable_rem_keys = self._scan_directory_tree()
        prev_map = dict(self._snapshot)

        def _purge_unreachable(rem_keys: Set[str]):
            for rem_key in rem_keys:
                self._active_removable_volumes.pop(rem_key, None)
                for k in [
                    pk for pk, ps in prev_map.items()
                    if ps.removable_root_key == rem_key or _path_is_within_prefix(ps.path, rem_key)
                ]:
                    prev_map.pop(k, None)

        if unreachable_rem_keys:
            _purge_unreachable(unreachable_rem_keys)

        prev_keys = set(prev_map.keys())
        curr_keys = set(current_map.keys())

        disappeared_keys = prev_keys - curr_keys
        appeared_keys = curr_keys - prev_keys
        common_keys = prev_keys & curr_keys

        self._reconcile_split_move_candidates(current_map, common_keys, appeared_keys, disappeared_keys)

        # Guard A: If a file disappeared during this scan pass with no newly-appeared counterpart yet
        # (e.g. destination root was scanned just before Explorer finished Cut -> Paste), wait briefly
        # and re-scan once so mid-flight Explorer moves coalesce cleanly into MOVED instead of DELETED + CREATED.
        if disappeared_keys and not appeared_keys:
            time.sleep(0.12)
            current_map, unreachable_rem_keys2 = self._scan_directory_tree()
            if unreachable_rem_keys2:
                _purge_unreachable(unreachable_rem_keys2)
                prev_keys = set(prev_map.keys())
            curr_keys = set(current_map.keys())
            disappeared_keys = prev_keys - curr_keys
            appeared_keys = curr_keys - prev_keys
            common_keys = prev_keys & curr_keys
            self._reconcile_split_move_candidates(current_map, common_keys, appeared_keys, disappeared_keys)

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
                self._deferred_new_files.pop(new_k, None)

                if old_snap.size == new_snap.size and old_snap.mtime_ns == new_snap.mtime_ns:
                    new_snap.sha256 = old_snap.sha256
                    new_snap.blob_id = old_snap.blob_id
                new_snap.is_baseline = old_snap.is_baseline
                new_snap.baseline_timestamp = old_snap.baseline_timestamp
                self._seen_counter += 1
                new_snap.seen_seq = self._seen_counter

                old_parent = os.path.normcase(os.path.dirname(old_snap.path))
                new_parent = os.path.normcase(os.path.dirname(new_snap.path))
                ev_type = "RENAMED" if old_parent == new_parent else "MOVED"

                if debug_diag:
                    print(
                        f"[DLP-DIAG] INODE_MATCH -> {ev_type} | src={old_snap.path} | dst={new_snap.path} "
                        f"| dev_ino={file_id} | size={new_snap.size} | sha256={new_snap.sha256}"
                    )

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
                    was_baseline=old_snap.is_baseline,
                ))

        remaining_disappeared = disappeared_keys - matched_old
        remaining_appeared = appeared_keys - matched_new

        # In-flight Windows Explorer copy/write lock deferral:
        # When Explorer copies a file (or performs a cross-volume Cut+Paste), it creates the destination
        # file and holds a write handle while streaming bytes. If a newly appeared file is currently locked
        # or still growing, defer it for up to 2 cycles so SHA-256 correlation sees the settled file.
        for new_k in list(remaining_appeared):
            ns = current_map.get(new_k)
            if ns is None:
                continue
            defer_reason: Optional[str] = None
            try:
                st_now = os.stat(ns.path, follow_symlinks=False)
                if st_now.st_size != ns.size or st_now.st_mtime_ns != ns.mtime_ns:
                    defer_reason = f"size/mtime still changing ({ns.size} -> {st_now.st_size})"
                elif ns.size == 0:
                    # Verify 0-byte file isn't held under an exclusive write share-lock
                    try:
                        with open(ns.path, "rb"):
                            pass
                        ns.sha256 = hashlib.sha256(b"").hexdigest()
                    except PermissionError as pe:
                        defer_reason = f"0-byte file locked ({pe})"
                elif 0 < ns.size <= MAX_HASH_SIZE_BYTES and not ns.sha256:
                    sha_val, read_sz, h_err = compute_file_sha256(ns.path)
                    if h_err and ("PermissionError" in h_err or "WinError 32" in h_err):
                        defer_reason = h_err
                    elif sha_val and not h_err:
                        if read_sz != ns.size:
                            defer_reason = f"partial read ({read_sz} != {ns.size})"
                        else:
                            ns.sha256 = sha_val
            except FileNotFoundError:
                # Transient Explorer temp/staging file vanished before classification
                remaining_appeared.discard(new_k)
                current_map.pop(new_k, None)
                self._deferred_new_files.pop(new_k, None)
                continue
            except PermissionError as pe:
                defer_reason = str(pe)
            except OSError:
                pass

            if defer_reason:
                attempts = self._deferred_new_files.get(new_k, 0)
                if attempts < 2:
                    self._deferred_new_files[new_k] = attempts + 1
                    remaining_appeared.discard(new_k)
                    current_map.pop(new_k, None)
                    if debug_diag:
                        print(
                            f"[DLP-DIAG] DEFERRED_IN_FLIGHT (attempt {attempts + 1}/2) | path={ns.path} "
                            f"| reason={defer_reason}"
                        )
                    continue
            self._deferred_new_files.pop(new_k, None)

        # Clean up stale deferral counters for keys no longer pending
        for dk in list(self._deferred_new_files.keys()):
            if dk in current_map or not os.path.exists(dk):
                if dk in current_map:
                    self._deferred_new_files.pop(dk, None)

        # 2. Cross-volume / copy+unlink MOVED detection (Local <-> Local across partitions OR Local <-> USB)
        # Pair ONLY when unambiguous 1-to-1 match by exact (size + SHA-256) OR cross-volume (filename + size + preserved mtime).
        if remaining_disappeared and remaining_appeared:
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
                        sha = s.sha256
                        if not sha:
                            sha, _, err = compute_file_sha256(s.path)
                            if sha and not err:
                                s.sha256 = sha
                        if sha:
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
                        new_snap.is_baseline = old_snap.is_baseline
                        new_snap.baseline_timestamp = old_snap.baseline_timestamp
                        self._seen_counter += 1
                        new_snap.seen_seq = self._seen_counter

                        old_parent = os.path.normcase(os.path.dirname(old_snap.path))
                        new_parent = os.path.normcase(os.path.dirname(new_snap.path))
                        ev_type = "RENAMED" if old_parent == new_parent else "MOVED"

                        if debug_diag:
                            print(
                                f"[DLP-DIAG] SHA_MOVE_MATCH -> {ev_type} | src={old_snap.path} | dst={new_snap.path} "
                                f"| size={new_snap.size} | sha256={new_snap.sha256}"
                            )

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
                            was_baseline=old_snap.is_baseline,
                        ))

            # Fallback for giant files (> baseline_hash_max_mb) moved across volumes where sha256 wasn't pre-computed
            if remaining_disappeared and remaining_appeared:
                old_by_namesize: Dict[Tuple[str, int], List[str]] = {}
                for k in remaining_disappeared:
                    s = prev_map[k]
                    if s.size > 0 and s.sha256 is None:
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
                        different_volume = (
                            old_snap.removable_root_key != new_snap.removable_root_key
                            or old_snap.dev != new_snap.dev
                            or old_snap.ino == 0
                        )
                        if different_volume and abs(old_snap.mtime_ns - new_snap.mtime_ns) <= 2_100_000_000:
                            remaining_disappeared.discard(ok_list[0])
                            remaining_appeared.discard(nk_list[0])
                            self._seen_counter += 1
                            new_snap.seen_seq = self._seen_counter
                            changes.append(DetectedFileChange(
                                event_type="MOVED",
                                file_name=new_snap.name,
                                file_extension=new_snap.extension,
                                source_path=old_snap.path,
                                destination_path=new_snap.path,
                                file_size=new_snap.size,
                                mtime_ns=new_snap.mtime_ns,
                                target_path=new_snap.path,
                                was_baseline=old_snap.is_baseline,
                            ))

        # 3. Existing paths: case-only RENAMED and content MODIFIED
        for k in sorted(common_keys):
            old_snap = prev_map[k]
            curr_snap = current_map[k]
            curr_snap.is_baseline = old_snap.is_baseline
            curr_snap.baseline_timestamp = old_snap.baseline_timestamp
            curr_snap.seen_seq = old_snap.seen_seq

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
                    was_baseline=old_snap.is_baseline,
                ))

            if old_snap.size != curr_snap.size or old_snap.mtime_ns != curr_snap.mtime_ns:
                self._seen_counter += 1
                curr_snap.seen_seq = self._seen_counter
                changes.append(DetectedFileChange(
                    event_type="MODIFIED",
                    file_name=curr_snap.name,
                    file_extension=curr_snap.extension,
                    source_path=curr_snap.path,
                    destination_path=None,
                    file_size=curr_snap.size,
                    mtime_ns=curr_snap.mtime_ns,
                    target_path=curr_snap.path,
                    was_baseline=old_snap.is_baseline,
                ))
            else:
                curr_snap.sha256 = old_snap.sha256
                curr_snap.blob_id = old_snap.blob_id

        # 4. Newly appeared files: check reliable SHA-256 COPIED correlation across ALL monitored directories
        # (Local -> Local, Local -> USB, USB -> Local) without ever matching by filename alone!
        pre_existing_pool_keys = common_keys | matched_new
        for new_k in sorted(remaining_appeared):
            new_snap = current_map[new_k]
            self._seen_counter += 1
            new_snap.seen_seq = self._seen_counter
            matched_copy_source: Optional[FileSnapshot] = None

            if new_snap.size > 0:
                size_candidates = [
                    current_map[k_cand]
                    for k_cand in pre_existing_pool_keys
                    if k_cand in current_map
                    and k_cand != new_k
                    and current_map[k_cand].size == new_snap.size
                ]
                if debug_diag:
                    print(
                        f"[DLP-DIAG] RAW_APPEARED | path={new_snap.path} | size={new_snap.size} "
                        f"| dev_ino=({new_snap.dev},{new_snap.ino}) | sha256={new_snap.sha256} "
                        f"| size_candidates={[c.path for c in size_candidates]}"
                    )
                if 1 <= len(size_candidates) <= 25:
                    new_sha = new_snap.sha256
                    if not new_sha:
                        new_sha, _, err = compute_file_sha256(new_snap.path)
                        if new_sha and not err:
                            new_snap.sha256 = new_sha
                    if new_sha:
                        sha_matches: List[FileSnapshot] = []
                        for cand in size_candidates:
                            c_sha = cand.sha256
                            if not c_sha:
                                c_sha, _, c_err = compute_file_sha256(cand.path)
                                if c_sha and not c_err:
                                    cand.sha256 = c_sha
                            if c_sha == new_sha:
                                sha_matches.append(cand)

                        if len(sha_matches) == 1:
                            matched_copy_source = sha_matches[0]
                        elif len(sha_matches) > 1:
                            # Disambiguate multi-hop Explorer chains (e.g. Docs -> Desktop -> Downloads -> USB):
                            # Windows Explorer Copy/Paste preserves LastWriteTime (mtime_ns) on every hop,
                            # so we rank same-filename copies by (seen_seq, ctime_ns, mtime_ns) to pick the
                            # most recently materialized hop while refusing to guess on unrelated filenames!
                            same_name = [
                                c for c in sha_matches
                                if os.path.normcase(c.name) == os.path.normcase(new_snap.name)
                            ]
                            if len(same_name) == 1:
                                matched_copy_source = same_name[0]
                            elif len(same_name) > 1:
                                def _hop_rank(c: FileSnapshot) -> Tuple[int, int, int]:
                                    return (c.seen_seq, c.ctime_ns, c.mtime_ns)

                                ranked_same = sorted(same_name, key=_hop_rank, reverse=True)
                                if _hop_rank(ranked_same[0]) > _hop_rank(ranked_same[1]):
                                    matched_copy_source = ranked_same[0]
                            else:
                                # Check Windows Explorer same-folder duplicate pattern ("<stem> - Copy<ext>")
                                new_parent = os.path.normcase(os.path.dirname(new_snap.path))
                                new_stem = os.path.normcase(Path(new_snap.name).stem)
                                explorer_dup_cands = [
                                    c for c in sha_matches
                                    if os.path.normcase(os.path.dirname(c.path)) == new_parent
                                    and new_stem.startswith(os.path.normcase(Path(c.name).stem) + " - copy")
                                ]
                                if len(explorer_dup_cands) == 1:
                                    matched_copy_source = explorer_dup_cands[0]
                                else:
                                    # Different filename across folders with multiple identical SHA-256 copies:
                                    # only correlate if a single candidate has a strictly newer mtime_ns.
                                    sorted_by_mtime = sorted(sha_matches, key=lambda c: c.mtime_ns, reverse=True)
                                    if sorted_by_mtime[0].mtime_ns > sorted_by_mtime[1].mtime_ns:
                                        matched_copy_source = sorted_by_mtime[0]

            if matched_copy_source is not None:
                if matched_copy_source.blob_id and not new_snap.blob_id:
                    new_snap.blob_id = matched_copy_source.blob_id
                if debug_diag:
                    print(
                        f"[DLP-DIAG] CLASSIFIED -> COPIED | src={matched_copy_source.path} "
                        f"| dst={new_snap.path} | sha256={new_snap.sha256}"
                    )
                changes.append(DetectedFileChange(
                    event_type="COPIED",
                    file_name=new_snap.name,
                    file_extension=new_snap.extension,
                    source_path=matched_copy_source.path,
                    destination_path=new_snap.path,
                    file_size=new_snap.size,
                    mtime_ns=new_snap.mtime_ns,
                    target_path=new_snap.path,
                    known_sha256=new_snap.sha256,
                    known_blob_id=new_snap.blob_id,
                ))
                # Add newly confirmed file to pool so sequential copies in one cycle can chain cleanly
                pre_existing_pool_keys.add(new_k)
            else:
                if debug_diag:
                    print(
                        f"[DLP-DIAG] CLASSIFIED -> CREATED | dst={new_snap.path} "
                        f"| size={new_snap.size} | sha256={new_snap.sha256}"
                    )
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
            # Level 1 opportunistic check: if still accessible right now (e.g. pending handle), allow reading
            still_accessible_path = old_snap.path if os.path.isfile(old_snap.path) else None
            if debug_diag:
                print(
                    f"[DLP-DIAG] CLASSIFIED -> DELETED | src={old_snap.path} "
                    f"| size={old_snap.size} | sha256={old_snap.sha256}"
                )
            changes.append(DetectedFileChange(
                event_type="DELETED",
                file_name=old_snap.name,
                file_extension=old_snap.extension,
                source_path=old_snap.path,
                destination_path=None,
                file_size=old_snap.size,
                mtime_ns=old_snap.mtime_ns,
                target_path=still_accessible_path,
                known_sha256=old_snap.sha256,
                known_blob_id=old_snap.blob_id,
                was_baseline=old_snap.is_baseline,
            ))

        self._snapshot = current_map
        return changes

    async def _process_change(self, change: DetectedFileChange):
        """
        Multi-level evidence resolution & metadata preservation:
          - Level 1: Accessible file or previously archived SHA-256 blob -> CAPTURED
          - Level 2: Windows Recycle Bin ($Recycle.Bin $I/$R) fallback -> CAPTURED_FROM_RECYCLE_BIN
          - Level 3: Permanent deletion (Shift+Delete) + optional secondary backup dirs -> CAPTURED or NOT_CAPTURED_UNAVAILABLE
          - Level 4: Configured maximum_evidence_size_mb enforced across all levels -> NOT_CAPTURED_SIZE_LIMIT
        """
        dlp_cfg = self.config.dlp or {}
        evidence_enabled = bool(dlp_cfg.get("evidence_enabled", True))
        max_mb = float(dlp_cfg.get("maximum_evidence_size_mb", 25))
        max_evidence_bytes = int(max_mb * 1024 * 1024)
        evidence_events = {str(e).upper() for e in dlp_cfg.get("evidence_events", [])}
        rb_enabled = bool(dlp_cfg.get("recycle_bin_recovery_enabled", True))
        custom_rb_roots = self._custom_recycle_roots or dlp_cfg.get("recycle_bin_roots")
        backup_dirs = list(dlp_cfg.get("secondary_backup_directories") or [])

        sha256_hex: Optional[str] = change.known_sha256
        actual_size: Optional[int] = change.file_size
        hash_error: Optional[str] = None
        effective_destination = change.destination_path
        upload_source_path: Optional[str] = change.target_path
        recovered_from_recycle_bin = False
        recovered_from_backup = False

        # Hash accessible target_path when not already known
        if upload_source_path and not sha256_hex:
            sha256_hex, read_bytes, hash_error = await asyncio.to_thread(
                compute_file_sha256,
                upload_source_path,
            )
            if read_bytes is not None:
                actual_size = read_bytes

        # Multi-level DELETE inspection (Recycle Bin & optional secondary backup directories)
        if change.event_type == "DELETED" and not upload_source_path:
            if rb_enabled and change.source_path:
                rb_match = await asyncio.to_thread(
                    find_in_windows_recycle_bin,
                    change.source_path,
                    actual_size,
                    sha256_hex,
                    custom_rb_roots,
                )
                if rb_match is not None:
                    effective_destination = "Recycle Bin"
                    actual_size = rb_match.file_size
                    if rb_match.sha256:
                        sha256_hex = rb_match.sha256
                    if not rb_match.size_exceeded_ceiling:
                        upload_source_path = rb_match.payload_path
                        recovered_from_recycle_bin = True

            # Optional secondary backup/shadow-copy directory fallback when permanently deleted
            if not upload_source_path and backup_dirs and sha256_hex:
                b_match = await asyncio.to_thread(
                    find_in_secondary_backups,
                    change.file_name,
                    actual_size,
                    sha256_hex,
                    backup_dirs,
                )
                if b_match is not None:
                    b_path, b_size, b_sha = b_match
                    upload_source_path = b_path
                    actual_size = b_size
                    sha256_hex = b_sha
                    recovered_from_backup = True

        # Persist computed hash into active snapshot for live files
        if change.target_path and sha256_hex:
            norm_k = os.path.normcase(os.path.abspath(change.target_path))
            snap = self._snapshot.get(norm_k)
            if snap:
                snap.sha256 = sha256_hex

        file_blob_id: Optional[str] = None
        evidence_status = "NOT_CAPTURED_DISABLED"
        needs_deferred_evidence = False
        deferred_status_on_success = "CAPTURED_FROM_RECYCLE_BIN" if recovered_from_recycle_bin else "CAPTURED"

        if evidence_enabled and change.event_type in evidence_events:
            # Level 4: Always check configured size ceiling first so oversized files (even on DELETE)
            # accurately report NOT_CAPTURED_SIZE_LIMIT rather than generic unavailability.
            if actual_size is not None and actual_size > max_evidence_bytes:
                evidence_status = "NOT_CAPTURED_SIZE_LIMIT"
            elif actual_size is not None and actual_size == 0:
                evidence_status = "NO_EVIDENCE"
            elif sha256_hex and sha256_hex in self._uploaded_blobs:
                file_blob_id = self._uploaded_blobs[sha256_hex]
                evidence_status = "CAPTURED_FROM_RECYCLE_BIN" if recovered_from_recycle_bin else "CAPTURED"
            elif change.known_blob_id:
                file_blob_id = change.known_blob_id
                evidence_status = "CAPTURED"
            elif not upload_source_path or hash_error or sha256_hex is None:
                evidence_status = "NOT_CAPTURED_UNAVAILABLE"
            else:
                up_res = await self.client.upload_dlp_evidence(
                    file_path=upload_source_path,
                    expected_sha256=sha256_hex,
                    original_filename=change.file_name,
                )
                status_str = up_res.get("status")
                if status_str == "ok" and up_res.get("file_id"):
                    file_blob_id = up_res["file_id"]
                    self._uploaded_blobs[sha256_hex] = file_blob_id
                    evidence_status = "CAPTURED_FROM_RECYCLE_BIN" if recovered_from_recycle_bin else "CAPTURED"
                    if change.target_path:
                        norm_k = os.path.normcase(os.path.abspath(change.target_path))
                        if norm_k in self._snapshot:
                            self._snapshot[norm_k].blob_id = file_blob_id
                elif status_str in ("retry", "auth_error"):
                    needs_deferred_evidence = True
                    evidence_status = "NOT_CAPTURED_UNAVAILABLE"
                else:
                    evidence_status = "NOT_CAPTURED_UNAVAILABLE"

        endpoint_id = self.config.device_id or ""
        idem_key = generate_idempotency_key(
            endpoint_id=endpoint_id or "local-endpoint",
            event_type=change.event_type,
            source_path=change.source_path,
            destination_path=effective_destination,
            file_size=actual_size,
            mtime_ns=change.mtime_ns,
            sha256=sha256_hex,
        )

        channel, risk_level, rem_vol, transfer_dir = self._classify_event_context(
            change.event_type,
            change.source_path,
            effective_destination,
        )

        configured_limit_val = int(max_mb) if max_mb.is_integer() else max_mb
        metadata = {
            "file_path": (
                effective_destination
                if (effective_destination and effective_destination != "Recycle Bin")
                else change.source_path
            ),
            "file_size": actual_size if actual_size is not None else 0,
            "file_extension": change.file_extension or "",
            "evidence_status": evidence_status,
            "maximum_evidence_size_mb": configured_limit_val,
            "baseline": bool(change.was_baseline),
        }
        if sha256_hex:
            metadata["sha256"] = sha256_hex
        if recovered_from_recycle_bin:
            metadata["recovery_source"] = "windows_recycle_bin"
        elif recovered_from_backup:
            metadata["recovery_source"] = "secondary_backup"
        elif change.event_type == "DELETED" and effective_destination != "Recycle Bin":
            metadata["deletion_mode"] = "permanent_or_bypassed_recycle_bin"

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
            "destination_path": effective_destination,
            "channel": channel,
            "application_name": None,
            "risk_level": risk_level,
            "file_blob_id": file_blob_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata,
        }

        queue_item = {
            "payload": payload,
            "target_path": upload_source_path if needs_deferred_evidence else None,
            "expected_sha256": sha256_hex if needs_deferred_evidence else None,
            "success_evidence_status": deferred_status_on_success,
            "attempts": 0,
        }

        print(
            f"[DLP] {change.event_type}: file={change.file_name} | src={change.source_path} "
            f"| dst={effective_destination} | size={actual_size} | channel={channel} | status={evidence_status}"
        )

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
                payload["metadata"]["evidence_status"] = item.get("success_evidence_status", "CAPTURED")
                self._uploaded_blobs[item["expected_sha256"]] = blob_id
                item["target_path"] = None
            elif up_res.get("status") in ("retry", "auth_error") and item["attempts"] < 3:
                item["attempts"] += 1
                self._retry_queue.append(item)
                return False
            else:
                payload["metadata"]["evidence_status"] = "NOT_CAPTURED_UNAVAILABLE"
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
