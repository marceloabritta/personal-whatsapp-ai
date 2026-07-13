"""Small, idempotent primitives for migrations. A migration mutates the working FOLDER.

Everything here is safe to run twice — that is the point.
"""
from __future__ import annotations

import json
import os


def read_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def write_json(path: str, payload: dict) -> None:
    """Atomic: a migration interrupted halfway must never leave a truncated board.json."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def set_board_key(ws, key: str, value) -> bool:
    """Add/overwrite a top-level key in board.json. False if there is no board yet."""
    data = read_json(ws.board_path)
    if data is None:
        return False
    if data.get(key) == value:
        return False
    data[key] = value
    write_json(ws.board_path, data)
    return True


def add_env_key(ws, key: str, default: str = "", comment: str = "") -> bool:
    """Append a config key to the working folder's .env if it isn't already there.
    Never changes a value you have set — a new capability arrives OFF, and you turn it on."""
    try:
        with open(ws.env_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        text = ""
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped.startswith(f"{key}="):
            return False
    block = ("\n" if text and not text.endswith("\n") else "") + (
        f"\n# {comment}\n" if comment else "\n"
    ) + f"{key}={default}\n"
    with open(ws.env_path, "a", encoding="utf-8") as fh:
        fh.write(block)
    return True


def ensure_dirs(*paths: str) -> list[str]:
    made = []
    for p in paths:
        if not os.path.isdir(p):
            os.makedirs(p, exist_ok=True)
            made.append(p)
    return made
