"""File and folder size scanning. Does not follow junctions or directory symlinks."""

from __future__ import annotations

import ctypes
import os
import stat as stat_module
import sys
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
INVALID_FILE_SIZE = 0xFFFFFFFF

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


def format_bytes(n: int) -> str:
    value = float(max(0, n))
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
        allocated=_on_disk_size(path, logical),
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
    seen: set[tuple],
) -> tuple[int, int, int, int, int, bool]:
    """Return size, allocated, files, folders, errors, cancelled."""
    size = 0
    allocated = 0
    files = 0
    folders = 0
    errors = 0
    cancelled = False
    stack = [path]

    while stack:
        if cancel and cancel():
            cancelled = True
            stack.clear()
            break
        current = stack.pop()
        try:
            it = os.scandir(current)
        except OSError:
            errors += 1
            continue
        with it:
            for entry in it:
                if cancel and cancel():
                    cancelled = True
                    stack.clear()
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                    is_directory = entry.is_dir(follow_symlinks=False)
                    is_link = entry.is_symlink()
                except OSError:
                    errors += 1
                    continue

                if is_directory:
                    folders += 1
                    if is_link or _is_reparse(st):
                        continue
                    if _mark_seen(seen, entry.path, st):
                        continue
                    stack.append(entry.path)
                    continue

                files += 1
                logical = int(st.st_size)
                size += logical
                allocated += logical
                if progress and files % 80 == 0:
                    progress(entry.path, files, size)

    return size, allocated, files, folders, errors, cancelled


def scan_path(
    path: str,
    progress: ProgressFn | None = None,
    cancel: CancelFn | None = None,
    recursive: bool = False,
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
    seen: set[tuple] = set()
    _mark_seen(seen, path, st)

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
            if not recursive:
                children.append(
                    ChildStat(
                        name=entry.name,
                        path=entry.path,
                        is_dir=True,
                        size=0,
                        allocated=0,
                        unscanned=True,
                    )
                )
                continue
            if progress:
                progress(entry.path, total_files, total_size)
            if _mark_seen(seen, entry.path, est):
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
            size, allocated, files, folders, err, walk_cancelled = _walk_dir(
                entry.path, progress, cancel, seen
            )
            if walk_cancelled or (cancel and cancel()):
                cancelled = True
            total_size += size
            total_alloc += allocated
            total_files += files
            total_folders += folders
            errors += err
            children.append(
                ChildStat(
                    name=entry.name,
                    path=entry.path,
                    is_dir=True,
                    size=size,
                    allocated=allocated,
                    files=files,
                    folders=folders,
                )
            )
            if cancelled:
                break
            continue

        logical = int(est.st_size)
        on_disk = _on_disk_size(entry.path, logical)
        total_files += 1
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
    )
