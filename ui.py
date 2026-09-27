"""Small always-on-top window for file and folder occupancy."""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import traceback
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from dropfiles import enable_file_drop
from scan import ScanResult, format_bytes, is_drive_root, scan_path, share_bar
from settings import DEFAULT_EXCLUDES_TEXT, load_state, save_state
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
        self._item_paths: dict[str, tuple[str, bool]] = {}
        self._top_paths: dict[str, tuple[str, bool]] = {}
        self._scanning = False
        self._ready = False
        self._ignore_recursive = False
        self._toggle_job: str | None = None
        self._drain_after: str | None = None
        self._saved = load_state()
        self._rows: list[ListRow] = []
        self._top_rows: list[ListRow] = []
        self._sort_column: str | None = None
        self._sort_desc = True
        self._top_sort_column: str | None = None
        self._top_sort_desc = True
        self._filter_job: str | None = None
        self._scan_started = 0.0
        self._denied_paths: dict[str, tuple[str, bool]] = {}
        self._drop_ready = False
        self._drop_after: str | None = None

        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._drain_after = self.root.after(80, self._drain_queue)

        launch = initial_path
        if not launch:
            remembered = str(self._saved.get("last_path") or "")
            if remembered and os.path.lexists(remembered):
                launch = remembered
        if launch:
            self._show_work()
            self._start_scan(launch, recursive=False)
        else:
            self._show_home()

        self._ready = True
        self.recursive_var.trace_add("write", self._on_recursive_write)
        self._drop_after = self.root.after(200, self._enable_drop)
        saved_geometry = self._saved.get("geometry")
        if isinstance(saved_geometry, str) and saved_geometry:
            self.root.geometry(saved_geometry)

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
        ttk.Button(top, text="刷新", command=self._refresh).pack(side=tk.LEFT, padx=(8, 0))
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
        ttk.Label(opts, text="排除").pack(side=tk.LEFT, padx=(12, 4))
        self.exclude_var = tk.StringVar(value=str(self._saved.get("excludes") or DEFAULT_EXCLUDES_TEXT))
        ttk.Entry(opts, textvariable=self.exclude_var, width=28).pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.path_var = tk.StringVar(value="")
        ttk.Label(self.work, textvariable=self.path_var, wraplength=520).pack(fill=tk.X, **pad)

        self.summary_var = tk.StringVar(value="")
        ttk.Label(self.work, textvariable=self.summary_var, font=("Segoe UI", 12, "bold")).pack(
            fill=tk.X, **pad
        )

        self.detail_var = tk.StringVar(value="")
        ttk.Label(self.work, textvariable=self.detail_var, wraplength=520).pack(fill=tk.X, **pad)

        self.denied_frame = ttk.Frame(self.work)
        ttk.Label(self.denied_frame, text="无法访问").pack(anchor=tk.W)
        self.denied_tree = self._scrolled_tree(self.denied_frame, ("path",), height=3)
        self.denied_tree.heading("path", text="路径")
        self.denied_tree.column("path", width=480, anchor=tk.W)
        self.denied_tree.bind("<Button-3>", self._on_right_click)

        self.progress = ttk.Progressbar(self.work, mode="indeterminate")
        self.progress.pack(fill=tk.X, padx=12, pady=(0, 4))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self.work, textvariable=self.status_var, wraplength=520).pack(fill=tk.X, padx=12)

        filt = ttk.Frame(self.work)
        filt.pack(fill=tk.X, padx=12, pady=(4, 0))
        ttk.Label(filt, text="筛选").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar()
        ttk.Entry(filt, textvariable=self.filter_var).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        self.filter_var.trace_add("write", self._on_filter_write)

        style = ttk.Style(self.root)
        style.configure("TPanedwindow", sashthickness=8)
        self.pane = ttk.Panedwindow(self.work, orient=tk.VERTICAL)
        self.pane.pack(fill=tk.BOTH, expand=True, padx=12, pady=(4, 12))
        self.main_pane = ttk.Frame(self.pane)
        self.pane.add(self.main_pane, weight=3)

        columns = ("name", "kind", "size", "share", "files")
        self.tree = self._scrolled_tree(self.main_pane, columns, height=8)
        self.tree.heading("name", text="名称")
        self.tree.heading("kind", text="类型")
        self.tree.heading("size", text="大小")
        self.tree.heading("share", text="占比")
        self.tree.heading("files", text="文件数")
        self.tree.column("name", width=200)
        self.tree.column("kind", width=70, anchor=tk.CENTER)
        self.tree.column("size", width=90, anchor=tk.E)
        self.tree.column("share", width=150, anchor=tk.W)
        self.tree.column("files", width=70, anchor=tk.E)
        for column in ("name", "size", "share"):
            self.tree.heading(column, command=lambda col=column: self._toggle_sort(col))
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._on_right_click)

        self.top_frame = ttk.Frame(self.pane)
        self.sash_grip = tk.Label(
            self.top_frame,
            text="⇅  拖动调整高度",
            cursor="sb_v_double_arrow",
            relief=tk.RAISED,
            bd=1,
            pady=1,
        )
        self.sash_grip.pack(fill=tk.X, pady=(2, 0))
        self.sash_grip.bind("<ButtonPress-1>", self._sash_press)
        self.sash_grip.bind("<B1-Motion>", self._sash_drag)
        self.sash_grip.bind("<ButtonRelease-1>", self._sash_release)
        self._sash_line: tk.Frame | None = None
        ttk.Label(self.top_frame, text="最大的文件").pack(anchor=tk.W, pady=(4, 0))
        self.top_tree = self._scrolled_tree(self.top_frame, ("path", "size", "share"), height=6)
        self.top_tree.heading("path", text="路径")
        self.top_tree.heading("size", text="大小")
        self.top_tree.heading("share", text="占比")
        self.top_tree.column("path", width=280)
        self.top_tree.column("size", width=90, anchor=tk.E)
        self.top_tree.column("share", width=150, anchor=tk.W)
        for column in ("path", "size", "share"):
            self.top_tree.heading(column, command=lambda col=column: self._toggle_top_sort(col))
        self.top_tree.bind("<Button-3>", self._on_right_click)

        self._menu = tk.Menu(self.root, tearoff=0)
        self._menu.add_command(label="在资源管理器中打开", command=self._open_selected)
        self._menu.add_command(label="复制路径", command=self._copy_selected_path)
        self._menu_target: tuple[str, bool] | None = None

    def _scrolled_tree(self, parent: ttk.Frame, columns: tuple[str, ...], height: int) -> ttk.Treeview:
        holder = ttk.Frame(parent)
        tree = ttk.Treeview(holder, columns=columns, show="headings", height=height)
        yscroll = tk.Scrollbar(holder, orient=tk.VERTICAL, command=tree.yview, width=16, relief=tk.GROOVE)
        xscroll = tk.Scrollbar(holder, orient=tk.HORIZONTAL, command=tree.xview, width=16, relief=tk.GROOVE)
        tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        holder.pack(fill=tk.BOTH, expand=True)
        tree.bind("<MouseWheel>", self._on_mousewheel, add="+")
        return tree

    def _sash_press(self, event: tk.Event) -> None:
        if len(self.pane.panes()) < 2:
            return
        self._sash_origin_y = event.y_root
        try:
            self._sash_origin_pos = int(self.pane.sashpos(0))
        except tk.TclError:
            return
        self._move_sash_line(event.y_root)
        self.sash_grip.grab_set()

    def _sash_drag(self, event: tk.Event) -> None:
        if self._sash_line is None:
            return
        self._move_sash_line(event.y_root)

    def _move_sash_line(self, y_root: int) -> None:
        height = max(self.pane.winfo_height(), 1)
        y = y_root - self.pane.winfo_rooty()
        y = max(48, min(y, height - 48))
        if self._sash_line is None:
            self._sash_line = tk.Frame(self.pane, height=4, bg="#1f6feb", cursor="sb_v_double_arrow")
        self._sash_line.place(x=0, y=y, relwidth=1, height=4)
        self._sash_line.lift()

    def _sash_release(self, event: tk.Event) -> None:
        try:
            self.sash_grip.grab_release()
        except tk.TclError:
            pass
        if self._sash_line is not None:
            self._sash_line.destroy()
            self._sash_line = None
        if len(self.pane.panes()) < 2:
            return
        height = max(self.pane.winfo_height(), 1)
        delta = event.y_root - getattr(self, "_sash_origin_y", event.y_root)
        target = int(getattr(self, "_sash_origin_pos", 0)) + delta
        target = max(48, min(target, height - 48))
        try:
            self.pane.sashpos(0, target)
            self.pane.update_idletasks()
        except tk.TclError:
            return

    def _on_mousewheel(self, event: tk.Event) -> str:
        widget = event.widget
        if isinstance(widget, ttk.Treeview) and event.delta:
            widget.yview_scroll(int(-event.delta / 120), "units")
        return "break"

    def _show_home(self) -> None:
        self.work.pack_forget()
        self.home.pack(fill=tk.BOTH, expand=True)
        self.root.minsize(360, 280)
        if not self._saved.get("geometry"):
            self.root.geometry("420x320")
        self._refresh_install_btn()

    def _show_work(self) -> None:
        self.home.pack_forget()
        self.work.pack(fill=tk.BOTH, expand=True)
        self.root.minsize(480, 380)
        if not self._saved.get("geometry"):
            self.root.geometry("560x640")

    def _refresh_install_btn(self) -> None:
        if is_installed():
            self.install_btn.config(text="移除右键菜单")
            self.home_hint.set("已可在资源管理器里右键使用。若一级菜单没有，请按住 Shift 再右键。")
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

    def _refresh(self) -> None:
        if self._current_path:
            self._start_scan(self._current_path)

    def _on_recursive_write(self, *_args) -> None:
        if self._ignore_recursive or not self._ready or not self._current_path:
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
        if recursive and is_drive_root(path):
            ok = messagebox.askyesno(
                "查看占用空间",
                f"将统计整个磁盘 {path}，可能需要很久。继续？",
                parent=self.root,
            )
            if not ok:
                recursive = False
        if recursive != bool(self.recursive_var.get()):
            self._ignore_recursive = True
            self.recursive_var.set(recursive)
            self._ignore_recursive = False

        self._cancel.set()
        self._scan_id += 1
        scan_id = self._scan_id
        self._scan_started = time.monotonic()
        self._cancel = threading.Event()
        self._current_path = path
        self._scanning = True
        self._item_paths.clear()
        self._top_paths.clear()
        self._hide_top_files()
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
        excludes = parse_excludes(self.exclude_var.get())

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
                    excludes=excludes,
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
            elapsed = max(0.001, time.monotonic() - self._scan_started)
            rate = files / elapsed
            self.status_var.set(
                f"已扫 {files} 个文件 · {format_bytes(size)} · {elapsed:.0f} 秒 · 约 {rate:.0f} 个/秒 · {current}"
            )
            self.summary_var.set(f"统计中… {format_bytes(size)}")

    def _drain_queue(self) -> None:
        self._process_queue()
        try:
            if self.root.winfo_exists():
                self._drain_after = self.root.after(80, self._drain_queue)
        except tk.TclError:
            self._drain_after = None

    def shutdown(self) -> None:
        self._remember()
        self._cancel.set()
        self._scan_id += 1
        if self._filter_job is not None:
            try:
                self.root.after_cancel(self._filter_job)
            except tk.TclError:
                pass
            self._filter_job = None
        if self._toggle_job is not None:
            try:
                self.root.after_cancel(self._toggle_job)
            except tk.TclError:
                pass
            self._toggle_job = None
        if self._drop_after is not None:
            try:
                self.root.after_cancel(self._drop_after)
            except tk.TclError:
                pass
            self._drop_after = None
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
        self._top_paths.clear()
        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.top_tree.get_children():
            self.top_tree.delete(item)

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

        self._rows = [_row_from_child(child, result.size) for child in result.children]
        self._top_rows = []
        if result.recursive:
            root = result.path
            for hit in result.top_files:
                label = _relative_path(root, hit.path)
                self._top_rows.append(
                    ListRow(
                        label=label,
                        kind="文件",
                        size=hit.size,
                        size_txt=format_bytes(hit.size),
                        share_txt=share_bar(hit.size, result.size),
                        files_txt="",
                        path=hit.path,
                        open_dir=False,
                    )
                )
        self._apply_view()
        self._show_denied(result.denied)
        saved_sash = self._saved.get("sash")
        if self._top_rows and isinstance(saved_sash, int):
            self.root.after(30, lambda: self._restore_sash(saved_sash))

    def _restore_sash(self, pos: int) -> None:
        try:
            if len(self.pane.panes()) > 1:
                self.pane.sashpos(0, pos)
        except tk.TclError:
            pass

    def _apply_view(self) -> None:
        query = self.filter_var.get()
        rows = [row for row in self._rows if name_matches(row.label, query)]
        top_rows = [row for row in self._top_rows if name_matches(row.label, query)]
        if self._sort_column:
            rows = sort_rows(rows, self._sort_column, self._sort_desc)
        if self._top_sort_column:
            top_rows = sort_rows(top_rows, self._top_sort_column, self._top_sort_desc)
        self._fill_tree(self.tree, rows, self._item_paths, include_kind=True)
        if top_rows:
            self._show_top_pane()
            self._fill_tree(self.top_tree, top_rows, self._top_paths, include_kind=False)
        else:
            self._hide_top_files()
        self._mark_sort_headings()

    def _fill_tree(
        self,
        tree: ttk.Treeview,
        rows: list[ListRow],
        paths: dict[str, tuple[str, bool]],
        include_kind: bool,
    ) -> None:
        paths.clear()
        for item in tree.get_children():
            tree.delete(item)
        for row in rows:
            if include_kind:
                values = (row.label, row.kind, row.size_txt, row.share_txt, row.files_txt)
            else:
                values = (row.label, row.size_txt, row.share_txt)
            try:
                item_id = tree.insert("", tk.END, values=values)
            except tk.TclError:
                safe = tuple(repr(value) if index == 0 else value for index, value in enumerate(values))
                item_id = tree.insert("", tk.END, values=safe)
            if row.path:
                paths[item_id] = (row.path, row.open_dir)

    def _toggle_sort(self, column: str) -> None:
        self._sort_column, self._sort_desc = _next_sort(self._sort_column, self._sort_desc, column)
        self._apply_view()

    def _toggle_top_sort(self, column: str) -> None:
        self._top_sort_column, self._top_sort_desc = _next_sort(
            self._top_sort_column, self._top_sort_desc, column
        )
        self._apply_view()

    def _mark_sort_headings(self) -> None:
        labels = {"name": "名称", "size": "大小", "share": "占比"}
        for column, text in labels.items():
            mark = _sort_mark(self._sort_column, self._sort_desc, column)
            self.tree.heading(column, text=text + mark, command=lambda col=column: self._toggle_sort(col))
        top_labels = {"path": "路径", "size": "大小", "share": "占比"}
        for column, text in top_labels.items():
            mark = _sort_mark(self._top_sort_column, self._top_sort_desc, column)
            self.top_tree.heading(
                column, text=text + mark, command=lambda col=column: self._toggle_top_sort(col)
            )

    def _on_filter_write(self, *_args) -> None:
        if not self._ready:
            return
        if self._filter_job is not None:
            try:
                self.root.after_cancel(self._filter_job)
            except tk.TclError:
                pass
        self._filter_job = self.root.after(200, self._apply_filter)

    def _apply_filter(self) -> None:
        self._filter_job = None
        self._apply_view()

    def _show_denied(self, paths: list[str]) -> None:
        self._denied_paths.clear()
        for item in self.denied_tree.get_children():
            self.denied_tree.delete(item)
        if not paths:
            self.denied_frame.pack_forget()
            return
        for path in paths:
            try:
                item_id = self.denied_tree.insert("", tk.END, values=(path,))
            except tk.TclError:
                item_id = self.denied_tree.insert("", tk.END, values=(repr(path),))
            self._denied_paths[item_id] = (path, False)
        self.denied_frame.pack(fill=tk.X, padx=12, pady=(0, 4), before=self.progress)

    def _show_top_pane(self) -> None:
        if str(self.top_frame) not in self.pane.panes():
            self.pane.add(self.top_frame, weight=1)

    def _enable_drop(self) -> None:
        if self._drop_ready:
            return
        self._drop_ready = True
        try:
            enable_file_drop(self.root, self._on_drop)
        except (OSError, AttributeError, ValueError):
            return

    def _on_drop(self, paths: list[str]) -> None:
        for path in paths:
            if path and os.path.lexists(path):
                self._start_scan(path)
                return

    def _remember(self) -> None:
        data = {
            "geometry": self.root.winfo_geometry(),
            "last_path": self._current_path or "",
            "excludes": self.exclude_var.get(),
        }
        try:
            if len(self.pane.panes()) > 1:
                data["sash"] = int(self.pane.sashpos(0))
        except (tk.TclError, ValueError):
            pass
        try:
            save_state(data)
        except OSError:
            pass

    def _hide_top_files(self) -> None:
        for item in self.top_tree.get_children():
            self.top_tree.delete(item)
        if str(self.top_frame) in self.pane.panes():
            self.pane.forget(self.top_frame)

    def _row_target(self, tree: ttk.Treeview, item: str) -> tuple[str, bool] | None:
        if tree is self.top_tree:
            return self._top_paths.get(item)
        if tree is self.denied_tree:
            return self._denied_paths.get(item)
        return self._item_paths.get(item)

    def _on_right_click(self, event: tk.Event) -> None:
        tree = event.widget
        if not isinstance(tree, ttk.Treeview):
            return
        item = tree.identify_row(event.y)
        if not item:
            return
        target = self._row_target(tree, item)
        if target is None:
            return
        tree.selection_set(item)
        self._menu_target = target
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _open_selected(self) -> None:
        target = self._menu_target
        if target is None:
            return
        path, is_dir = target
        if is_dir:
            os.startfile(path)  # noqa: S606 — open the folder the user picked
            return
        subprocess.Popen(["explorer", f'/select,"{path}"'])

    def _copy_selected_path(self) -> None:
        target = self._menu_target
        if target is None:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(target[0])

    def _on_double_click(self, _event: tk.Event) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        target = self._item_paths.get(selection[0])
        if not target:
            return
        path, open_dir = target
        if open_dir:
            self._start_scan(path)
            return
        os.startfile(path)  # noqa: S606 — open the file the user double-clicked

    def _on_close(self) -> None:
        self.shutdown()


@dataclass
class ListRow:
    label: str
    kind: str
    size: int | None
    size_txt: str
    share_txt: str
    files_txt: str
    path: str
    open_dir: bool


def parse_excludes(text: str) -> set[str]:
    names: set[str] = set()
    for part in text.replace("，", ",").split(","):
        name = part.strip()
        if name:
            names.add(name)
    return names


def name_matches(label: str, query: str) -> bool:
    needle = query.strip().casefold()
    if not needle:
        return True
    return needle in label.casefold()


def _next_sort(current: str | None, descending: bool, column: str) -> tuple[str, bool]:
    if current == column:
        return column, not descending
    return column, column not in {"name", "path"}


def _sort_mark(current: str | None, descending: bool, column: str) -> str:
    if current != column:
        return ""
    return " ↓" if descending else " ↑"


def sort_rows(rows: list[ListRow], column: str, descending: bool) -> list[ListRow]:
    def key(row: ListRow):
        if column in {"name", "path"}:
            return row.label.casefold()
        return row.size if row.size is not None else -1

    return sorted(rows, key=key, reverse=descending)


def _row_from_child(child, total: int) -> ListRow:
    if child.skipped_link:
        kind, size_txt, size = "链接", "—", None
    elif child.excluded:
        kind, size_txt, size = "排除", "—", None
    elif child.unscanned:
        kind, size_txt, size = "文件夹", "—", None
    elif child.error:
        kind, size_txt, size = "错误", "—", None
    elif child.is_dir:
        kind, size_txt, size = "文件夹", format_bytes(child.size), child.size
    else:
        kind, size_txt, size = "文件", format_bytes(child.size), child.size
    files_txt = str(child.files) if child.is_dir and not child.unscanned and not child.excluded and child.files else ""
    share_txt = ""
    if size is not None and not child.error:
        share_txt = share_bar(size, total)
    return ListRow(
        label=child.name,
        kind=kind,
        size=size,
        size_txt=size_txt,
        share_txt=share_txt,
        files_txt=files_txt,
        path="" if child.error else child.path,
        open_dir=bool(child.is_dir and not child.skipped_link and not child.error),
    )


def _relative_path(root: str, path: str) -> str:
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return path
    if rel in {".", ""}:
        return os.path.basename(path)
    return rel


def run_ui(path: str | None = None) -> None:
    if path:
        try:
            path = os.path.abspath(path.strip().strip('"'))
        except (OSError, ValueError):
            pass
    root = tk.Tk()
    SizeWindow(root, path)
    root.mainloop()
