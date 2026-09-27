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
os.environ["FOLDER_SIZE_STATE"] = "-"

from app import path_after_script
from scan import allocation_for, format_bytes, is_drive_root, scan_path, share_bar, stat_file
from settings import load_state, save_state
from ui import SizeWindow, name_matches, parse_excludes, sort_rows, ListRow


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

    def test_share_bar(self) -> None:
        self.assertEqual(share_bar(0, 100), "")
        self.assertEqual(share_bar(42, 0), "")
        self.assertEqual(share_bar(42, 100), "████░░░░░░ 42%")
        self.assertEqual(share_bar(100, 100), "██████████ 100%")
        self.assertEqual(share_bar(275, 375), "███████░░░ 73%")


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

    def test_top_files_sorted_and_include_nested(self) -> None:
        flat = scan_path(str(self.base), recursive=False)
        self.assertEqual(flat.top_files, [])
        result = scan_path(str(self.base), recursive=True)
        names = [os.path.basename(hit.path) for hit in result.top_files]
        self.assertEqual(names, ["b.txt", "a.txt", "c.txt", "d.txt"])
        sizes = [hit.size for hit in result.top_files]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(result.top_files[0].size, 200)
        self.assertTrue(any("deep" in hit.path for hit in result.top_files))

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
        self.assertTrue(self.win.tree.cget("yscrollcommand"))
        self.assertTrue(self.win.top_tree.cget("yscrollcommand"))
        self.assertEqual(sub_row[3], "")
        file_item = next(
            i
            for i in self.win.tree.get_children()
            if self.win.tree.item(i, "values")[0] == "a.txt"
        )
        file_row = self.win.tree.item(file_item, "values")
        self.assertEqual(file_row[3], "██████████ 100%")
        stored = self.win._item_paths[file_item]
        self.assertEqual(stored[0], str(self.base / "a.txt"))
        self.assertFalse(stored[1])

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
        self.assertTrue(sub_row[3].endswith("%"))
        self.assertIn(str(self.win.top_frame), self.win.pane.panes())
        self.assertIn("拖动", self.win.sash_grip.cget("text"))
        self.assertEqual(self.win.sash_grip.cget("cursor"), "sb_v_double_arrow")
        top_names = [self.win.top_tree.item(i, "values")[0] for i in self.win.top_tree.get_children()]
        self.assertTrue(any(name.endswith("b.txt") for name in top_names))
        top_item = self.win.top_tree.get_children()[0]
        top_path, is_dir = self.win._top_paths[top_item]
        self.assertFalse(is_dir)
        self.assertTrue(top_path.endswith("b.txt"))

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


class ExtraTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.sizes = make_tree(self.base)

    def tearDown(self) -> None:
        os.environ["FOLDER_SIZE_STATE"] = "-"
        self._tmp.cleanup()

    def test_drive_root_and_excludes(self) -> None:
        self.assertTrue(is_drive_root("C:\\"))
        self.assertFalse(is_drive_root(str(self.base)))
        self.assertEqual(parse_excludes(" .git，node_modules, "), {".git", "node_modules"})
        self.assertTrue(name_matches("Hello.TXT", "txt"))
        self.assertFalse(name_matches("Hello.TXT", "pdf"))
        _write(self.base / "node_modules" / "pkg.js", b"z" * 500)
        result = scan_path(str(self.base), recursive=True)
        self.assertEqual(result.size, sum(self.sizes.values()))
        skipped = next(child for child in result.children if child.name == "node_modules")
        self.assertTrue(skipped.excluded)
        self.assertEqual(skipped.size, 0)

    def test_sort_rows(self) -> None:
        rows = [
            ListRow("b", "文件", 2, "2 B", "", "", "b", False),
            ListRow("a", "文件", 10, "10 B", "", "", "a", False),
        ]
        self.assertEqual(sort_rows(rows, "size", True)[0].label, "a")
        self.assertEqual(sort_rows(rows, "name", False)[0].label, "a")

    def test_state_roundtrip(self) -> None:
        path = self.base / "state.json"
        os.environ["FOLDER_SIZE_STATE"] = str(path)
        save_state({"geometry": "800x600+10+10", "last_path": str(self.base), "sash": 180})
        data = load_state()
        self.assertEqual(data["geometry"], "800x600+10+10")
        self.assertEqual(data["sash"], 180)
        self.assertEqual(data["last_path"], str(self.base))

    def test_hard_link_counted_once(self) -> None:
        src = self.base / "a.txt"
        linked = self.base / "sub" / "same.txt"
        os.link(src, linked)
        result = scan_path(str(self.base), recursive=True, excludes=set())
        self.assertEqual(result.size, sum(self.sizes.values()))

    def test_plain_file_skips_allocation_query(self) -> None:
        target = self.base / "a.txt"
        self.assertEqual(allocation_for(str(target), 10, 0), 10)
        self.assertEqual(allocation_for(str(target), target.stat().st_size, 0x800), target.stat().st_size)

    def test_denied_paths_are_listed(self) -> None:
        blocked = r"C:\System Volume Information"
        if not os.path.isdir(blocked):
            self.skipTest("no protected system folder")
        result = scan_path(blocked, recursive=False)
        self.assertGreaterEqual(result.errors, 1)
        self.assertTrue(result.denied)
        self.assertTrue(any("System Volume Information" in item for item in result.denied))


class FilterUiTests(unittest.TestCase):
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

    def test_filter_and_open_file(self) -> None:
        wait_idle(self.win)
        self.win.filter_var.set("a.txt")
        deadline = time.monotonic() + 1
        while self.win._filter_job is not None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(0.02)
        self.root.update()
        names = [self.win.tree.item(i, "values")[0] for i in self.win.tree.get_children()]
        self.assertEqual(names, ["a.txt"])
        opened: list[str] = []
        original = os.startfile
        os.startfile = lambda path: opened.append(path)  # type: ignore[assignment]
        try:
            item = self.win.tree.get_children()[0]
            self.win.tree.selection_set(item)
            self.win._on_double_click(None)
        finally:
            os.startfile = original  # type: ignore[assignment]
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].endswith("a.txt"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
