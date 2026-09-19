"""Small always-on-top window for file and folder occupancy."""

from __future__ import annotations

import os
import queue
import threading
import time
import traceback
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk

from scan import ScanResult, format_bytes, scan_path
from shell import install, is_installed, uninstall


class SizeWindow:
    def __init__(self, root: tk.Tk, initial_path: str | None = None) -> None:
        self.root = root
        self.root.title("查看占用空间")
        self.root.geometry("420x320")
        self.root.minsize(360, 280)
        self.root.attributes("-topmost", True)

        self._cancel = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._current_path = initial_path
        self._scan_id = 0
        self._item_paths: dict[str, str] = {}
        self._scanning = False
        self._ready = False
        self._toggle_job: str | None = None
        self._drain_after: str | None = None

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_after = self.root.after(80, self._drain_queue)

        if initial_path:
            self._show_work()
            self._start_scan(initial_path, recursive=False)
        else:
            self._show_home()

        self._ready = True
        self.recursive_var.trace_add("write", self._on_recursive_write)

    def _build(self) -> None:
        self._build_home()
        self._build_work()
        self._last_result: ScanResult | None = None

    def _build_home(self) -> None:
        self.home = ttk.Frame(self.root, padding=32)
        ttk.Label(self.home, text="查看占用空间", font=("Segoe UI", 18)).pack(pady=(12, 6))
        ttk.Label(self.home, text="看一个文件夹占了多少磁盘").pack(pady=(0, 20))
        ttk.Button(self.home, text="选择文件夹", command=self._pick_dir).pack(ipadx=16, ipady=6)
        self.install_btn = ttk.Button(self.home, text="安装到右键菜单", command=self._toggle_shell)
        self.install_btn.pack(pady=(16, 8))
        self.home_hint = tk.StringVar()
        ttk.Label(self.home, textvariable=self.home_hint, wraplength=340, justify=tk.CENTER).pack()
        self._refresh_install_btn()

    def _build_work(self) -> None:
        self.work = ttk.Frame(self.root)
        pad = {"padx": 12, "pady": 6}

        top = ttk.Frame(self.work)
        top.pack(fill=tk.X, **pad)
        ttk.Button(top, text="打开", command=self._pick_dir).pack(side=tk.LEFT)
        ttk.Button(top, text="上级", command=self._go_parent).pack(side=tk.LEFT, padx=(8, 0))
        self.stop_btn = ttk.Button(top, text="停止", command=self._stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(top, text="复制", command=self._copy).pack(side=tk.RIGHT)

        opts = ttk.Frame(self.work)
        opts.pack(fill=tk.X, padx=12)
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts,
            text="包含子文件夹",
            variable=self.recursive_var,
        ).pack(side=tk.LEFT)

        self.path_var = tk.StringVar(value="")
        ttk.Label(self.work, textvariable=self.path_var, wraplength=520).pack(fill=tk.X, **pad)

        self.summary_var = tk.StringVar(value="")
        ttk.Label(self.work, textvariable=self.summary_var, font=("Segoe UI", 12, "bold")).pack(
            fill=tk.X, **pad
        )

        self.detail_var = tk.StringVar(value="")
        ttk.Label(self.work, textvariable=self.detail_var, wraplength=520).pack(fill=tk.X, **pad)

        self.progress = ttk.Progressbar(self.work, mode="indeterminate")
        self.progress.pack(fill=tk.X, padx=12, pady=(0, 4))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.work, textvariable=self.status_var, wraplength=520).pack(fill=tk.X, padx=12)

        columns = ("name", "kind", "size", "files")
        self.tree = ttk.Treeview(self.work, columns=columns, show="headings", height=12)
        self.tree.heading("name", text="名称")
        self.tree.heading("kind", text="类型")
        self.tree.heading("size", text="大小")
        self.tree.heading("files", text="文件数")
        self.tree.column("name", width=260)
        self.tree.column("kind", width=70, anchor=tk.CENTER)
        self.tree.column("size", width=100, anchor=tk.E)
        self.tree.column("files", width=70, anchor=tk.E)
        self.tree.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 12))
        self.tree.bind("<Double-1>", self._on_double_click)

    def _show_home(self) -> None:
        self.work.pack_forget()
        self.home.pack(fill=tk.BOTH, expand=True)
        self.root.minsize(360, 280)
        self.root.geometry("420x320")
        self._refresh_install_btn()

    def _show_work(self) -> None:
        self.home.pack_forget()
        self.work.pack(fill=tk.BOTH, expand=True)
        self.root.minsize(480, 380)
        self.root.geometry("560x520")

    def _refresh_install_btn(self) -> None:
        if is_installed():
            self.install_btn.config(text="移除右键菜单")
            self.home_hint.set("已可在资源管理器里右键使用。Windows 11 请按住 Shift 再右键。")
        else:
            self.install_btn.config(text="安装到右键菜单")
            self.home_hint.set("装好后，右键文件或文件夹即可查看占用。不需要管理员。")

    def _toggle_shell(self) -> None:
        if is_installed():
            uninstall()
        else:
            install()
        self._refresh_install_btn()

    def _pick_dir(self) -> None:
        path = filedialog.askdirectory(parent=self.root)
        if path:
            self._start_scan(path)

    def _go_parent(self) -> None:
        path = self._current_path
        if not path:
            return
        parent = os.path.dirname(os.path.abspath(path))
        if parent and parent != os.path.abspath(path):
            self._start_scan(parent)

    def _stop(self) -> None:
        self._cancel.set()
        self.status_var.set("正在停止…")

    def _on_recursive_write(self, *_args) -> None:
        if not self._ready or not self._current_path:
            return
        if self._toggle_job is not None:
            try:
                self.root.after_cancel(self._toggle_job)
            except tk.TclError:
                pass
        # Wait until BooleanVar has the new value, and coalesce double-toggles.
        self._toggle_job = self.root.after(50, self._restart_after_toggle)

    def _restart_after_toggle(self) -> None:
        self._toggle_job = None
        path = self._current_path
        if not path:
            return
        self._start_scan(path, recursive=bool(self.recursive_var.get()))

    def _copy(self) -> None:
        result = self._last_result
        if result is None:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.path_var.get())
            return
        lines = [
            result.path,
            f"逻辑大小: {format_bytes(result.size)} ({result.size} 字节)",
            f"占用: {format_bytes(result.allocated)} ({result.allocated} 字节)",
        ]
        if result.is_dir:
            scope = "含所有子文件夹" if result.recursive else "仅当前文件夹内的文件"
            lines.append(scope)
            lines.append(f"文件: {result.files}  文件夹: {result.folders}")
            for child in result.children[:20]:
                kind = "文件夹" if child.is_dir else "文件"
                extra = "（链接，未展开）" if child.skipped_link else ""
                extra += "（未计入子文件夹）" if child.unscanned else ""
                err = f" 错误:{child.error}" if child.error else ""
                lines.append(f"  {child.name}\t{kind}\t{format_bytes(child.size)}{extra}{err}")
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(lines))

    def _start_scan(self, path: str, recursive: bool | None = None) -> None:
        self._show_work()
        if recursive is None:
            recursive = bool(self.recursive_var.get())
        else:
            recursive = bool(recursive)

        self._cancel.set()
        self._scan_id += 1
        scan_id = self._scan_id
        self._cancel = threading.Event()
        self._current_path = path
        self._scanning = True
        self._item_paths.clear()
        self.path_var.set(path)
        self.summary_var.set("正在统计…")
        self.detail_var.set("正在统计所有子文件夹…" if recursive else "仅当前文件夹内的文件")
        self.status_var.set("")
        self._last_result = None
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.progress.start(12)
        self.stop_btn.config(state=tk.NORMAL)

        cancel_event = self._cancel

        def work() -> None:
            last_emit = 0.0

            def progress(current: str, files: int, size: int) -> None:
                nonlocal last_emit
                now = time.monotonic()
                if now - last_emit < 0.2:
                    return
                last_emit = now
                self._queue.put(("progress", scan_id, current, files, size))

            try:
                result = scan_path(
                    path,
                    progress=progress,
                    cancel=cancel_event.is_set,
                    recursive=recursive,
                )
                self._queue.put(("done", scan_id, result))
            except Exception as exc:  # noqa: BLE001 — show any scan failure in the UI
                self._queue.put(("error", scan_id, f"{exc}\n\n{traceback.format_exc()}"))

        self._worker = threading.Thread(target=work, daemon=True, name=f"scan-{scan_id}")
        self._worker.start()

    def _process_queue(self) -> None:
        latest_progress = None
        completed = False
        try:
            while True:
                item = self._queue.get_nowait()
                kind = item[0]
                scan_id = item[1]
                if scan_id != self._scan_id:
                    continue
                if kind == "progress":
                    latest_progress = item
                elif kind == "done":
                    completed = True
                    try:
                        self._show_result(item[2])
                    except Exception as exc:  # noqa: BLE001
                        self._idle()
                        self.summary_var.set("无法显示结果")
                        self.status_var.set(f"{exc}\n\n{traceback.format_exc()}")
                elif kind == "error":
                    completed = True
                    self._idle()
                    self.summary_var.set("无法统计")
                    self.detail_var.set("请检查路径是否存在，或看下方详细信息。")
                    self.status_var.set(item[2])
        except queue.Empty:
            pass
        if latest_progress is not None and not completed and self._scanning:
            _, _, current, files, size = latest_progress
            self.status_var.set(f"已扫 {files} 个文件 · {format_bytes(size)} · {current}")
            self.summary_var.set(f"统计中… {format_bytes(size)}")

    def _drain_queue(self) -> None:
        self._process_queue()
        try:
            if self.root.winfo_exists():
                self._drain_after = self.root.after(80, self._drain_queue)
        except tk.TclError:
            self._drain_after = None

    def shutdown(self) -> None:
        self._cancel.set()
        self._scan_id += 1
        if self._toggle_job is not None:
            try:
                self.root.after_cancel(self._toggle_job)
            except tk.TclError:
                pass
            self._toggle_job = None
        if self._drain_after is not None:
            try:
                self.root.after_cancel(self._drain_after)
            except tk.TclError:
                pass
            self._drain_after = None
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _idle(self) -> None:
        self._scanning = False
        self.progress.stop()
        self.stop_btn.config(state=tk.DISABLED)

    def _show_result(self, result: ScanResult) -> None:
        self._last_result = result
        self._idle()
        self._item_paths.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)

        suffix = "（已停止，结果不完整）" if result.cancelled else ""
        self.summary_var.set(f"{format_bytes(result.size)}{suffix}")

        details = [f"占用约 {format_bytes(result.allocated)}"]
        if result.is_dir:
            if result.recursive:
                details.append("含所有子文件夹")
            else:
                details.append("仅当前文件夹内的文件，不含子文件夹")
        if result.mtime is not None:
            try:
                details.append(
                    "修改时间 " + datetime.fromtimestamp(result.mtime).strftime("%Y-%m-%d %H:%M:%S")
                )
            except (OSError, OverflowError, ValueError):
                pass
        if result.is_dir:
            details.append(f"{result.files} 个文件，{result.folders} 个子文件夹")
        if result.errors:
            details.append(f"{result.errors} 项无法访问")
        self.detail_var.set(" · ".join(details))
        if result.cancelled:
            self.status_var.set("已停止")
        elif result.is_dir and not result.recursive:
            self.status_var.set("双击子文件夹可统计那一层")
        else:
            self.status_var.set("完成")

        for child in result.children:
            if child.skipped_link:
                kind = "链接"
                size_txt = "—"
            elif child.unscanned:
                kind = "文件夹"
                size_txt = "—"
            elif child.error:
                kind = "错误"
                size_txt = "—"
            elif child.is_dir:
                kind = "文件夹"
                size_txt = format_bytes(child.size)
            else:
                kind = "文件"
                size_txt = format_bytes(child.size)
            files_txt = str(child.files) if child.is_dir and not child.unscanned and child.files else ""
            try:
                item_id = self.tree.insert(
                    "",
                    tk.END,
                    values=(child.name, kind, size_txt, files_txt),
                )
            except tk.TclError:
                item_id = self.tree.insert(
                    "",
                    tk.END,
                    values=(repr(child.name), kind, size_txt, files_txt),
                )
            if child.is_dir and not child.skipped_link:
                self._item_paths[item_id] = child.path

    def _on_double_click(self, _event: tk.Event) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        path = self._item_paths.get(selection[0])
        if path:
            self._start_scan(path)

    def _on_close(self) -> None:
        self.shutdown()


def run_ui(path: str | None = None) -> None:
    if path:
        try:
            path = os.path.abspath(path.strip().strip('"'))
        except (OSError, ValueError):
            pass
    root = tk.Tk()
    SizeWindow(root, path)
    root.mainloop()
