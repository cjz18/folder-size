"""Remember window size, split position, and the last opened folder."""

from __future__ import annotations

import json
import os

DEFAULT_EXCLUDES_TEXT = ".git, node_modules, __pycache__"


def state_path() -> str | None:
    override = os.environ.get("FOLDER_SIZE_STATE")
    if override == "-":
        return None
    if override:
        return override
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "folder-size", "state.json")


def load_state() -> dict:
    path = state_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(data: dict) -> None:
    path = state_path()
    if not path:
        return
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
