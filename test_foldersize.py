"""Automated tests for folder occupancy: scan, paths, cancel, and UI toggle."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import path_after_script
from scan import format_bytes, scan_path, stat_file
from ui import SizeWindow


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def make_tree(base: Path) -> dict[str, int]:
    """Nested fixture: root file + sub/deep files. Returns expected sizes."""
    sizes = {
        "a.txt": 100,
        "sub/b.txt": 200,
        "sub/deep/c.txt": 50,
        "sub/deep/d.txt": 25,
    }
    for rel, n in sizes.items():
        _write(base / rel, b"x" * n)
    return sizes


def wait_idle(win: SizeWindow, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        win.root.update()
        if not win._scanning and win._last_result is not None:
            return win._last_result
        time.sleep(0.02)
    raise TimeoutError(
        f"scan stuck: scanning={win._scanning} summary={win.summary_var.get()!r} "
        f"status={win.status_var.get()!r} detail={win.detail_var.get()!r}"
    )


class FormatBytesTests(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(1024), "1 KB")
        self.assertEqual(format_bytes(1536), "1.5 KB")
        self.assertEqual(format_bytes(1048576), "1 MB")


class PathParseTests(unittest.TestCase):
    def test_quoted_file(self) -> None:
        script = r"C:\mb\app.py"
        got = path_after_script(
            r'"C:\py\pythonw.exe" "C:\mb\app.py" "C:\mb\README.md"',
            script,
        )
        self.assertEqual(got, r"C:\mb\README.md")

    def test_trailing_backslash_folder(self) -> None:
        got = path_after_script(
            r'"C:\py\pythonw.exe" "C:\mb\app.py" "C:\mb\"',
            r"C:\mb\app.py",
        )
        self.assertEqual(got, r"C:\mb")

    def test_drive_root(self) -> None:
        got = path_after_script(
            r'"C:\py\pythonw.exe" "C:\mb\app.py" "C:\"',
            r"C:\mb\app.py",
        )
        self.assertEqual(got, "C:\\")


class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.sizes = make_tree(self.base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_file_matches_stat(self) -> None:
        path = self.base / "a.txt"
        result = scan_path(str(path))
        self.assertFalse(result.is_dir)
        self.assertEqual(result.size, 100)
        self.assertEqual(stat_file(str(path)).size, path.stat().st_size)

    def test_missing_path(self) -> None:
        with self.assertRaises(FileNotFoundError):
            scan_path(str(self.base / "nope"))

    def test_current_folder_only(self) -> None:
        result = scan_path(str(self.base), recursive=False)
        self.assertFalse(result.recursive)
        self.assertEqual(result.size, 100)
        self.assertEqual(result.files, 1)
        self.assertEqual(result.folders, 1)
        sub = next(c for c in result.children if c.name == "sub")
        self.assertTrue(sub.is_dir)
        self.assertTrue(sub.unscanned)
        self.assertEqual(sub.size, 0)

    def test_include_subfolders(self) -> None:
        result = scan_path(str(self.base), recursive=True)
        self.assertTrue(result.recursive)
        self.assertEqual(result.size, sum(self.sizes.values()))
        self.assertEqual(result.files, 4)
        self.assertGreaterEqual(result.folders, 1)
        sub = next(c for c in result.children if c.name == "sub")
        self.assertFalse(sub.unscanned)
        self.assertEqual(sub.size, 200 + 50 + 25)
        self.assertGreater(sub.size, 0)

    def test_recursive_larger_than_flat(self) -> None:
        flat = scan_path(str(self.base), recursive=False)
        deep = scan_path(str(self.base), recursive=True)
        self.assertGreater(deep.size, flat.size)
        self.assertGreater(deep.files, flat.files)

    def test_cancel_stops(self) -> None:
        nested = self.base / "many"
        for i in range(80):
            _write(nested / f"f{i}.bin", b"y" * 10)
            _write(nested / f"d{i}" / "z.bin", b"z" * 10)
        result = scan_path(str(self.base), recursive=True, cancel=lambda: True)
        self.assertTrue(result.cancelled)

    def test_quoted_normalized_path(self) -> None:
        quoted = f'"{self.base}"'
        result = scan_path(quoted, recursive=False)
        self.assertEqual(result.size, 100)


class UiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        make_tree(self.base)
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = SizeWindow(self.root, str(self.base))
        self.root.attributes("-topmost", False)

    def tearDown(self) -> None:
        try:
            self.win.shutdown()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_opens_current_folder_only(self) -> None:
        result = wait_idle(self.win)
        self.assertFalse(result.recursive)
        self.assertEqual(result.size, 100)
        self.assertIn("仅当前文件夹", self.win.detail_var.get())
        self.assertNotIn("统计中", self.win.summary_var.get())
        names = [self.win.tree.item(i, "values")[0] for i in self.win.tree.get_children()]
        self.assertIn("a.txt", names)
        self.assertIn("sub", names)
        sub_row = next(
            self.win.tree.item(i, "values")
            for i in self.win.tree.get_children()
            if self.win.tree.item(i, "values")[0] == "sub"
        )
        self.assertEqual(sub_row[2], "—")

    def test_toggle_include_subfolders_rescans(self) -> None:
        wait_idle(self.win)
        self.win.recursive_var.set(True)
        self.root.update()
        deadline = time.monotonic() + 2
        while self.win._toggle_job is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        # Allow the scheduled restart to start a new scan.
        started = time.monotonic()
        while time.monotonic() < started + 1:
            self.root.update()
            if self.win._scanning or (
                self.win._last_result and self.win._last_result.recursive
            ):
                break
            time.sleep(0.02)
        result = wait_idle(self.win)
        self.assertTrue(result.recursive, msg=self.win.detail_var.get())
        self.assertEqual(result.size, 375)
        self.assertIn("含所有子文件夹", self.win.detail_var.get())
        self.assertNotIn("统计中", self.win.summary_var.get())
        self.assertTrue(self.win.summary_var.get())
        sub_row = next(
            self.win.tree.item(i, "values")
            for i in self.win.tree.get_children()
            if self.win.tree.item(i, "values")[0] == "sub"
        )
        self.assertNotEqual(sub_row[2], "—")

    def test_uncheck_returns_to_current_folder(self) -> None:
        wait_idle(self.win)
        self.win.recursive_var.set(True)
        self.root.update()
        wait_idle(self.win)
        self.win.recursive_var.set(False)
        self.root.update()
        deadline = time.monotonic() + 2
        while self.win._toggle_job is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        result = wait_idle(self.win)
        self.assertFalse(result.recursive)
        self.assertEqual(result.size, 100)
        self.assertIn("仅当前文件夹", self.win.detail_var.get())

    def test_double_click_subfolder(self) -> None:
        wait_idle(self.win)
        item = next(
            i
            for i in self.win.tree.get_children()
            if self.win.tree.item(i, "values")[0] == "sub"
        )
        self.win.tree.selection_set(item)
        self.win._on_double_click(None)  # type: ignore[arg-type]
        result = wait_idle(self.win)
        self.assertEqual(Path(result.path).name, "sub")
        self.assertFalse(result.recursive)
        self.assertEqual(result.size, 200)

    def test_progress_does_not_override_done(self) -> None:
        wait_idle(self.win)
        from scan import ScanResult

        fake = ScanResult(
            path=str(self.base),
            is_dir=True,
            size=123,
            allocated=123,
            files=1,
            folders=0,
            mtime=None,
            children=[],
            recursive=True,
        )
        self.win._scan_id = 99
        self.win._scanning = True
        self.win._queue.put(("progress", 99, str(self.base), 3, 50))
        self.win._queue.put(("done", 99, fake))
        self.win._process_queue()
        self.assertFalse(self.win._scanning)
        self.assertEqual(self.win._last_result, fake)
        self.assertNotIn("统计中", self.win.summary_var.get())
        self.assertEqual(self.win.status_var.get(), "完成")


class HomeUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        make_tree(self.base)
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = SizeWindow(self.root, None)
        self.root.attributes("-topmost", False)

    def tearDown(self) -> None:
        try:
            self.win.shutdown()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_starts_on_home(self) -> None:
        self.root.update()
        self.assertEqual(self.win.home.winfo_manager(), "pack")
        self.assertEqual(self.win.work.winfo_manager(), "")
        self.assertFalse(self.win._scanning)
        self.assertIn(
            self.win.install_btn.cget("text"),
            ("安装到右键菜单", "移除右键菜单"),
        )

    def test_choosing_folder_leaves_home(self) -> None:
        self.win._start_scan(str(self.base))
        result = wait_idle(self.win)
        self.assertEqual(self.win.work.winfo_manager(), "pack")
        self.assertEqual(self.win.home.winfo_manager(), "")
        self.assertEqual(result.size, 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
