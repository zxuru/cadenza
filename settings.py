"""Persisted user settings (download folder, preferred format)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_NAME = "Cadenza"
# Window/app id used by the Linux shell to match the desktop entry.
APP_ID = "cadenza"
APP_DIR_NAME = APP_NAME
CONFIG_FILE_NAME = "config.json"


def config_dir() -> Path:
    """Per-user config directory for the current platform."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / APP_DIR_NAME


def suggested_music_dir() -> Path:
    """Starting point for the folder picker: the user's music folder if there is one."""
    for candidate in (_xdg_music_dir(), Path.home() / "Music"):
        if candidate is not None and candidate.is_dir():
            return candidate
    return Path.home()


def _xdg_music_dir() -> Path | None:
    """Music folder from `user-dirs.dirs`, which is localized (e.g. ~/Música)."""
    if sys.platform != "linux":
        return None
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    try:
        text = (Path(config_home) / "user-dirs.dirs").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    home = str(Path.home())
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() != "XDG_MUSIC_DIR":
            continue
        raw = value.strip().strip('"').replace("$HOME", home).replace("${HOME}", home)
        path = Path(raw)
        return path if path.is_dir() else None
    return None


class Settings:
    """Tiny JSON-backed settings store; missing or broken files fall back to defaults."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or config_dir() / CONFIG_FILE_NAME
        self.download_root: Path | None = None
        self.format: str | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> Settings:
        settings = cls(path)
        try:
            raw = json.loads(settings.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return settings
        root = raw.get("download_root")
        if isinstance(root, str) and root.strip():
            settings.download_root = Path(root).expanduser()
        fmt = raw.get("format")
        if isinstance(fmt, str) and fmt:
            settings.format = fmt
        return settings

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "download_root": str(self.download_root) if self.download_root else None,
            "format": self.format,
        }
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
