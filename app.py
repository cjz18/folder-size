"""Entry point: context-menu target, or a picker window with no arguments."""

from __future__ import annotations

import ctypes
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from scan import normalize_target
from shell import install, uninstall
from ui import run_ui


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def path_after_script(command_line: str, script_path: str) -> str | None:
    """Take the target path from Explorer's raw command line, not parsed argv.

    Quoted folder paths often end with a backslash, which breaks Windows
    argument parsing (the trailing \\ swallows the closing quote).
    """
    if not command_line:
        return None
    lower = command_line.lower()
    script = os.path.abspath(script_path)
    idx = lower.find(script.lower())
    if idx == -1:
        base = os.path.basename(script).lower()
        idx = lower.find(base)
        if idx == -1:
            return None
        end = idx + len(base)
    else:
        end = idx + len(script)
    rest = command_line[end:]
    rest = rest.lstrip(" \t")
    if rest.startswith('"'):
        rest = rest[1:]
        if rest.endswith('"'):
            rest = rest[:-1]
    rest = rest.strip().strip('"').strip()
    if not rest or rest.startswith("--"):
        return None
    return normalize_target(rest)


def _raw_command_line() -> str:
    if sys.platform != "win32":
        return " ".join(sys.argv)
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCommandLineW.restype = ctypes.c_wchar_p
    return kernel32.GetCommandLineW() or " ".join(sys.argv)


def resolve_launch_path(args: list[str]) -> str | None:
    extracted = path_after_script(_raw_command_line(), os.path.join(_ROOT, "app.py"))
    if extracted and os.path.lexists(extracted):
        return extracted
    if not args:
        return extracted
    joined = normalize_target(" ".join(args))
    if os.path.lexists(joined):
        return joined
    first = normalize_target(args[0])
    if os.path.lexists(first):
        return first
    return extracted or joined or first


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--install"]:
        install()
        print("已安装资源管理器右键菜单「查看占用空间」（当前用户）。")
        print("Windows 11：右键后选「显示更多选项」，或按住 Shift 再右键。")
        return 0
    if args == ["--uninstall"]:
        uninstall()
        print("已移除右键菜单。")
        return 0
    if args and args[0] in {"-h", "--help"}:
        print("双击 打开.bat 即可。")
        print("  python app.py")
        print("  python app.py --install")
        print("  python app.py --uninstall")
        print("  python app.py [路径]")
        return 0

    path = resolve_launch_path(args)
    if not path:
        path = None
    run_ui(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
