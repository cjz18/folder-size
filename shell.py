"""Install or remove Explorer context-menu entries under HKCU (no admin)."""

from __future__ import annotations

import os
import sys
import winreg

MENU_TEXT = "查看占用空间"
KEY_NAME = "FolderSize"


def _pythonw() -> str:
    exe = os.path.abspath(sys.executable)
    base, name = os.path.split(exe)
    if name.lower() == "python.exe":
        candidate = os.path.join(base, "pythonw.exe")
        if os.path.isfile(candidate):
            return candidate
    return exe


def _script_path() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "app.py"))


def _command(placeholder: str) -> str:
    return f'"{_pythonw()}" "{_script_path()}" {placeholder}'


def _write_verb(root_key: str, placeholder: str) -> None:
    shell_path = rf"{root_key}\shell\{KEY_NAME}"
    cmd_path = rf"{shell_path}\command"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, shell_path) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, MENU_TEXT)
        winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, _pythonw())
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, cmd_path) as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, _command(placeholder))


def _delete_tree(root: str) -> None:
    path = rf"{root}\shell\{KEY_NAME}"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_ALL_ACCESS) as key:
            while True:
                try:
                    sub = winreg.EnumKey(key, 0)
                except OSError:
                    break
                winreg.DeleteKey(key, sub)
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
    except FileNotFoundError:
        return


def is_installed() -> bool:
    try:
        winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            rf"Software\Classes\Directory\shell\{KEY_NAME}",
        )
        return True
    except FileNotFoundError:
        return False


def install() -> None:
    # %L is the long path; extra trailing space avoids the \" quoting bug on folders.
    _write_verb(r"Software\Classes\*", '"%L"')
    _write_verb(r"Software\Classes\Directory", '"%L"')
    _write_verb(r"Software\Classes\Directory\Background", '"%V"')
    _write_verb(r"Software\Classes\Drive", '"%L"')


def uninstall() -> None:
    _delete_tree(r"Software\Classes\*")
    _delete_tree(r"Software\Classes\Directory\Background")
    _delete_tree(r"Software\Classes\Directory")
    _delete_tree(r"Software\Classes\Drive")
