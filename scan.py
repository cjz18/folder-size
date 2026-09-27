"""File and folder size scanning. Does not follow junctions or directory symlinks."""

from __future__ import annotations

import ctypes
import heapq
import os
import stat as stat_module
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable

FILE_ATTRIBUTE_SPARSE_FILE = 0x200
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_COMPRESSED = 0x800
INVALID_FILE_SIZE = 0xFFFFFFFF
DENIED_LIMIT = 50

ProgressFn = Callable[[str, int, int], None]
CancelFn = Callable[[], bool]


@dataclass
class ChildStat:
    name: str
    path: str
    is_dir: bool
    size: int
    allocated: int
    files: int = 0
    folders: int = 0
    skipped_link: bool = False
    unscanned: bool = False
    excluded: bool = False
    error: str | None = None


@dataclass
class ScanResult:
    path: str
    is_dir: bool
    size: int
    allocated: int
    files: int
    folders: int
    mtime: float | None
    children: list[ChildStat] = field(default_factory=list)
    errors: int = 0
    cancelled: bool = False
    recursive: bool = False
    top_files: list[FileHit] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)


TOP_FILE_LIMIT = 15
DEFAULT_EXCLUDES = (".git", "node_modules", "__pycache__")


@dataclass(order=True)
class _HeapItem:
    size: int
    seq: int
    path: str = field(compare=False)


class _TopFiles:
    def __init__(self, limit: int = TOP_FILE_LIMIT) -> None:
        self.limit = limit
        self._heap: list[_HeapItem] = []
        self._seq = 0

    def add(self, path: str, size: int) -> None:
        self._seq += 1
        item = _HeapItem(size=int(size), seq=self._seq, path=path)
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, item)
            return
        if item.size > self._heap[0].size:
            heapq.heapreplace(self._heap, item)

    def result(self) -> list[FileHit]:
        ranked = sorted(self._heap, key=lambda item: (-item.size, item.path.lower()))
        return [FileHit(path=item.path, size=item.size) for item in ranked]


@dataclass
class FileHit:
    path: str
    size: int


def normalize_excludes(names: set[str] | None) -> set[str]:
    source = DEFAULT_EXCLUDES if names is None else names
    return {os.path.normcase(name.strip()) for name in source if name and name.strip()}


def name_excluded(name: str, excludes: set[str]) -> bool:
    return os.path.normcase(name) in excludes


def is_drive_root(path: str) -> bool:
    try:
        full = os.path.abspath(path)
    except (OSError, ValueError):
        return False
    _drive, rest = os.path.splitdrive(full)
    return rest in {"\\", "/"}


def share_bar(size: int, total: int, width: int = 10) -> str:
    """10-cell bar plus percent. Empty when there is nothing to compare."""
    if total <= 0 or size <= 0:
        return ""
    ratio = min(1.0, size / total)
    filled = int(round(ratio * width))
    filled = min(width, max(0, filled))
    if filled == 0:
        filled = 1
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {round(ratio * 100)}%"


def format_bytes(n: int) -> str:
    n = max(0, int(n))
    value = float(n)
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(n)} B"
            text = f"{value:.2f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        value /= 1024
    return f"{n} B"


def _is_reparse(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)


def _mark_seen(seen: set[tuple], path: str, st: os.stat_result | None = None) -> bool:
    """Return True if this directory was already visited (cycle)."""
    path_key = ("path", os.path.normcase(os.path.abspath(path)))
    if path_key in seen:
        return True
    id_key = None
    if st is not None:
        ino = getattr(st, "st_ino", 0) or 0
        dev = getattr(st, "st_dev", 0) or 0
        if ino:
            id_key = ("id", int(dev), int(ino))
            if id_key in seen:
                return True
    seen.add(path_key)
    if id_key is not None:
        seen.add(id_key)
    return False


_get_compressed = None


def normalize_target(path: str) -> str:
    path = path.strip().strip('"').strip()
    if path.endswith('\\"'):
        path = path[:-2] + "\\"
    path = path.strip('"')
    path = os.path.expandvars(path)
    try:
        path = os.path.expanduser(path)
    except Exception:
        pass
    return os.path.normpath(path)


def allocation_for(path: str, logical: int, attrs: int) -> int:
    """Logical size, unless the file is compressed or sparse."""
    logical = max(0, int(logical))
    if attrs & (FILE_ATTRIBUTE_COMPRESSED | FILE_ATTRIBUTE_SPARSE_FILE):
        return _on_disk_size(path, logical)
    return logical


def _file_attrs(st: os.stat_result) -> int:
    return int(getattr(st, "st_file_attributes", 0) or 0)


def _identity_stat(path: str, st: os.stat_result) -> os.stat_result:
    """DirEntry.stat on Windows leaves st_ino and st_nlink at 0."""
    if int(getattr(st, "st_ino", 0) or 0):
        return st
    try:
        return os.lstat(path)
    except OSError:
        return st


def _file_id(st: os.stat_result) -> tuple | None:
    nlink = int(getattr(st, "st_nlink", 1) or 1)
    ino = int(getattr(st, "st_ino", 0) or 0)
    if nlink <= 1 or not ino:
        return None
    return ("file", int(getattr(st, "st_dev", 0) or 0), ino)


def _remember_denied(denied: list[str], path: str) -> None:
    if len(denied) < DENIED_LIMIT:
        denied.append(path)


class _Gate:
    """Shared directory/file identity and progress across parallel walks."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen_dirs: set[tuple] = set()
        self.seen_files: set[tuple] = set()
        self.progress_files = 0
        self.progress_size = 0

    def mark_dir(self, path: str, st: os.stat_result | None = None) -> bool:
        with self.lock:
            return _mark_seen(self.seen_dirs, path, st)

    def take_file(self, st: os.stat_result, path: str, logical: int, tops: _TopFiles | None) -> bool:
        with self.lock:
            ident = _file_id(st)
            if ident is not None:
                if ident in self.seen_files:
                    return False
                self.seen_files.add(ident)
            if tops is not None:
                tops.add(path, logical)
            return True

    def report(self, progress: ProgressFn | None, path: str, files: int, size: int) -> None:
        if files <= 0:
            return
        with self.lock:
            self.progress_files += files
            self.progress_size += size
            seen_files = self.progress_files
            seen_size = self.progress_size
        if progress and seen_files // 80 != (seen_files - files) // 80:
            progress(path, seen_files, seen_size)


@dataclass
class _WalkTotals:
    size: int = 0
    allocated: int = 0
    files: int = 0
    folders: int = 0
    errors: int = 0
    cancelled: bool = False
    denied: list[str] = field(default_factory=list)


def _on_disk_size(path: str, logical: int) -> int:
    logical = max(0, int(logical))
    if sys.platform != "win32":
        return logical
    global _get_compressed
    try:
        if _get_compressed is None:
            func = ctypes.WinDLL("kernel32", use_last_error=True).GetCompressedFileSizeW
            func.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(wintypes.DWORD)]
            func.restype = wintypes.DWORD
            _get_compressed = func
        high = wintypes.DWORD(0)
        low = _get_compressed(path, ctypes.byref(high))
        if low == INVALID_FILE_SIZE:
            err = ctypes.get_last_error()
            if err:
                return logical
        return (int(high.value) << 32) | int(low)
    except Exception:
        return logical


def stat_file(path: str) -> ScanResult:
    st = os.lstat(path)
    logical = int(st.st_size)
    return ScanResult(
        path=path,
        is_dir=False,
        size=logical,
        allocated=allocation_for(path, logical, _file_attrs(st)),
        files=1,
        folders=0,
        mtime=st.st_mtime,
        children=[],
        errors=0,
        recursive=False,
    )


def _walk_dir(
    path: str,
    progress: ProgressFn | None,
    cancel: CancelFn | None,
    gate: _Gate,
    tops: _TopFiles | None = None,
    excludes: set[str] | None = None,
) -> _WalkTotals:
    out = _WalkTotals()
    stack = [path]
    batch_files = 0
    batch_size = 0

    def flush(current: str) -> None:
        nonlocal batch_files, batch_size
        if batch_files:
            gate.report(progress, current, batch_files, batch_size)
            batch_files = 0
            batch_size = 0

    while stack:
        if cancel and cancel():
            out.cancelled = True
            stack.clear()
            break
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            out.errors += 1
            _remember_denied(out.denied, current)
            continue
        with it:
            for entry in it:
                if cancel and cancel():
                    out.cancelled = True
                    stack.clear()
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                    is_directory = entry.is_dir(follow_symlinks=False)
                    is_link = entry.is_symlink()
                except OSError:
                    out.errors += 1
                    _remember_denied(out.denied, entry.path)
                    continue

                if is_directory:
                    out.folders += 1
                    if is_link or _is_reparse(st) or name_excluded(entry.name, excludes or set()):
                        continue
                    if gate.mark_dir(entry.path, st):
                        continue
                    stack.append(entry.path)
                    continue

                out.files += 1
                logical = int(st.st_size)
                batch_files += 1
                if gate.take_file(_identity_stat(entry.path, st), entry.path, logical, tops):
                    out.size += logical
                    out.allocated += allocation_for(entry.path, logical, _file_attrs(st))
                    batch_size += logical
                if batch_files >= 80:
                    flush(entry.path)
            flush(current)
        if out.cancelled:
            break

    return out


def scan_path(
    path: str,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
    recursive: bool = False,
    excludes: set[str] | None = None,
) -> ScanResult:
    path = normalize_target(path)
    try:
        path = os.path.abspath(path)
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(f"路径无效: {path}") from exc
    if not os.path.lexists(path):
        raise FileNotFoundError(f"找不到路径: {path}")

    try:
        st = os.lstat(path)
    except OSError as exc:
        raise OSError(f"无法访问: {path}\n{exc}") from exc
    is_dir = stat_module.S_ISDIR(st.st_mode)
    if not is_dir:
        return stat_file(path)
    if _is_reparse(st) or os.path.islink(path):
        return ScanResult(
            path=path,
            is_dir=True,
            size=0,
            allocated=0,
            files=0,
            folders=0,
            mtime=st.st_mtime,
            children=[],
            errors=0,
            recursive=recursive,
        )

    children: list[ChildStat] = []
    total_size = 0
    total_alloc = 0
    total_files = 0
    total_folders = 0
    errors = 0
    cancelled = False
    denied: list[str] = []
    gate = _Gate()
    tops = _TopFiles() if recursive else None
    skipped_names = normalize_excludes(excludes if recursive else set())
    gate.mark_dir(path, st)
    pending: list[tuple[str, str]] = []

    def absorb(walked: _WalkTotals) -> None:
        nonlocal total_size, total_alloc, total_files, total_folders, errors, cancelled
        total_size += walked.size
        total_alloc += walked.allocated
        total_files += walked.files
        total_folders += walked.folders
        errors += walked.errors
        if walked.cancelled:
            cancelled = True
        for item in walked.denied:
            _remember_denied(denied, item)

    try:
        entries = list(os.scandir(path))
    except OSError:
        return ScanResult(
            path=path,
            is_dir=True,
            size=0,
            allocated=0,
            files=0,
            folders=0,
            mtime=st.st_mtime,
            children=[],
            errors=1,
            recursive=recursive,
            denied=[path],
        )

    for entry in entries:
        if cancel and cancel():
            cancelled = True
            break
        try:
            est = entry.stat(follow_symlinks=False)
            is_directory = entry.is_dir(follow_symlinks=False)
            is_link = entry.is_symlink()
        except OSError as exc:
            errors += 1
            _remember_denied(denied, entry.path)
            children.append(
                ChildStat(
                    name=entry.name,
                    path=entry.path,
                    is_dir=False,
                    size=0,
                    allocated=0,
                    error=str(exc),
                )
            )
            continue

        if is_directory:
            total_folders += 1
            if is_link or _is_reparse(est):
                children.append(
                    ChildStat(
                        name=entry.name,
                        path=entry.path,
                        is_dir=True,
                        size=0,
                        allocated=0,
                        skipped_link=True,
                    )
                )
                continue
            if not recursive or name_excluded(entry.name, skipped_names):
                children.append(
                    ChildStat(
                        name=entry.name,
                        path=entry.path,
                        is_dir=True,
                        size=0,
                        allocated=0,
                        unscanned=not recursive or not name_excluded(entry.name, skipped_names),
                        excluded=name_excluded(entry.name, skipped_names),
                    )
                )
                continue
            if gate.mark_dir(entry.path, est):
                children.append(
                    ChildStat(
                        name=entry.name,
                        path=entry.path,
                        is_dir=True,
                        size=0,
                        allocated=0,
                        skipped_link=True,
                    )
                )
                continue
            pending.append((entry.name, entry.path))
            continue

        logical = int(est.st_size)
        on_disk = allocation_for(entry.path, logical, _file_attrs(est))
        total_files += 1
        if gate.take_file(_identity_stat(entry.path, est), entry.path, logical, tops):
            total_size += logical
            total_alloc += on_disk
        children.append(
            ChildStat(
                name=entry.name,
                path=entry.path,
                is_dir=False,
                size=logical,
                allocated=on_disk,
                files=1,
            )
        )

    if pending and not cancelled:
        def walk_one(folder: str) -> _WalkTotals:
            return _walk_dir(folder, progress, cancel, gate, tops, skipped_names)

        if len(pending) == 1:
            walks = [walk_one(pending[0][1])]
        else:
            workers = min(4, len(pending))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                walks = list(pool.map(walk_one, [folder for _name, folder in pending]))
        for (name, folder), walked in zip(pending, walks):
            absorb(walked)
            children.append(
                ChildStat(
                    name=name,
                    path=folder,
                    is_dir=True,
                    size=walked.size,
                    allocated=walked.allocated,
                    files=walked.files,
                    folders=walked.folders,
                )
            )

    children.sort(key=lambda c: (c.is_dir, -c.size, c.name.lower()))
    return ScanResult(
        path=path,
        is_dir=True,
        size=total_size,
        allocated=total_alloc,
        files=total_files,
        folders=total_folders,
        mtime=st.st_mtime,
        children=children,
        errors=errors,
        cancelled=cancelled,
        recursive=recursive,
        top_files=tops.result() if tops is not None else [],
        denied=denied,
    )
