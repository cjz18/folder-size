"""Extreme cases: empty, deep, huge lists, odd names, deny, cancel."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ["FOLDER_SIZE_STATE"] = "-"

import tkinter as tk

from scan import format_bytes, scan_path, share_bar
from ui import SizeWindow


def wait_idle(win: SizeWindow, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        win.root.update()
        if not win._scanning and win._last_result is not None:
            return win._last_result
        time.sleep(0.02)
    raise TimeoutError(win.summary_var.get() + " " + win.status_var.get()[:300])


class ExtremeScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_folder(self) -> None:
        empty = self.base / "empty"
        empty.mkdir()
        result = scan_path(str(empty), recursive=True)
        self.assertEqual(result.size, 0)
        self.assertEqual(result.files, 0)
        self.assertEqual(result.children, [])
        self.assertEqual(result.top_files, [])
        self.assertFalse(result.cancelled)

    def test_only_empty_dirs_and_zero_files(self) -> None:
        (self.base / "a" / "b").mkdir(parents=True)
        (self.base / "zero.txt").write_bytes(b"")
        result = scan_path(str(self.base), recursive=True)
        self.assertEqual(result.size, 0)
        self.assertGreaterEqual(result.files, 1)
        self.assertEqual(share_bar(0, 0), "")

    def test_deep_nesting(self) -> None:
        current = self.base / "deep"
        current.mkdir()
        depth = 0
        for _ in range(40):
            nxt = current / "d"
            try:
                nxt.mkdir()
            except OSError:
                break
            current = nxt
            depth += 1
        (current / "leaf.txt").write_bytes(b"xyz")
        self.assertGreaterEqual(depth, 8)
        result = scan_path(str(self.base / "deep"), recursive=True)
        self.assertEqual(result.size, 3)
        self.assertEqual(result.files, 1)
        self.assertGreaterEqual(result.folders, depth)

    def test_many_files_and_top_limit(self) -> None:
        folder = self.base / "many"
        folder.mkdir()
        for i in range(40):
            (folder / f"f{i:03d}.bin").write_bytes(b"x" * (i + 1))
        result = scan_path(str(folder), recursive=True)
        self.assertEqual(result.files, 40)
        self.assertEqual(result.size, sum(range(1, 41)))
        self.assertEqual(len(result.top_files), 15)
        self.assertEqual(result.top_files[0].size, 40)
        sizes = [hit.size for hit in result.top_files]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_odd_names(self) -> None:
        names = ["空 格.txt", "引号\".txt", "中文.txt", "emoji-文件.txt"]
        for name in names:
            try:
                (self.base / name).write_bytes(b"ab")
            except OSError:
                continue
        result = scan_path(str(self.base), recursive=False)
        self.assertGreaterEqual(result.files, 1)
        self.assertGreaterEqual(result.size, 2)

    def test_missing_and_file(self) -> None:
        with self.assertRaises(FileNotFoundError):
            scan_path(str(self.base / "no-such"))
        target = self.base / "one.txt"
        target.write_bytes(b"hello")
        result = scan_path(f'"{target}\\"', recursive=False)
        self.assertFalse(result.is_dir)
        self.assertEqual(result.size, 5)

    def test_cancel_large_tree(self) -> None:
        folder = self.base / "wide"
        folder.mkdir()
        for i in range(30):
            sub = folder / f"s{i}"
            sub.mkdir()
            for j in range(30):
                (sub / f"{j}.bin").write_bytes(b"y" * 8)
        result = scan_path(str(folder), recursive=True, cancel=lambda: True)
        self.assertTrue(result.cancelled)
        self.assertLess(result.files, 30 * 30)

    def test_denied_directory_does_not_crash(self) -> None:
        blocked = Path(r"C:\System Volume Information")
        if not blocked.exists():
            self.skipTest("no protected system folder")
        result = scan_path(str(blocked), recursive=True)
        self.assertGreaterEqual(result.errors, 1)
        self.assertEqual(result.size, 0)

    def test_junction_not_followed(self) -> None:
        real = self.base / "real"
        real.mkdir()
        (real / "big.txt").write_bytes(b"x" * 1000)
        link = self.base / "link"
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(real)],
            capture_output=True,
            text=True,
        )
        if created.returncode != 0 or not link.exists():
            self.skipTest("could not create junction")
        result = scan_path(str(self.base), recursive=True)
        linked = next(child for child in result.children if child.name == "link")
        self.assertTrue(linked.skipped_link)
        self.assertEqual(result.size, 1000)

    def test_format_huge_and_negative(self) -> None:
        self.assertIn("TB", format_bytes(5 * 1024**4))
        self.assertEqual(format_bytes(-5), "0 B")


class ExtremeUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self) -> None:
        try:
            self.win.shutdown()
        except Exception:
            pass
        self._tmp.cleanup()

    def test_empty_and_many_rows(self) -> None:
        empty = self.base / "empty"
        empty.mkdir()
        self.win = SizeWindow(self.root, str(empty))
        result = wait_idle(self.win)
        self.assertEqual(result.size, 0)
        self.assertEqual(self.win.tree.get_children(), ())

        many = self.base / "many"
        many.mkdir()
        for i in range(800):
            (many / f"n{i:04d}.txt").write_bytes(b"z")
        long_name = "很长" * 40 + ".txt"
        (many / long_name).write_bytes(b"zz")
        self.win._start_scan(str(many), recursive=False)
        result = wait_idle(self.win)
        self.assertEqual(result.files, 801)
        self.assertEqual(len(self.win.tree.get_children()), 801)
        self.win.filter_var.set("不存在的名字xyz")
        deadline = time.monotonic() + 1
        while self.win._filter_job is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.root.update()
        self.assertEqual(self.win.tree.get_children(), ())
        self.win.filter_var.set("")
        while self.win._filter_job is not None and time.monotonic() < deadline + 1:
            self.root.update()
            time.sleep(0.02)
        self.root.update()
        self.win._toggle_sort("name")
        names = [self.win.tree.item(i, "values")[0] for i in self.win.tree.get_children()]
        self.assertEqual(names, sorted(names, key=str.casefold))


if __name__ == "__main__":
    unittest.main(verbosity=2)
