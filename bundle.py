"""Where the app finds the files it ships with.

Three layouts have to work, and they differ only in the directory that holds
the app's own tree (`main.py`, `assets/`, `locales/`, and the JavaScript
runtime a frozen build carries):

* running from a checkout, where that directory is the one holding this file;
* a frozen desktop build, which unpacks itself into `sys._MEIPASS` (see
  `build.py`);
* a `flet build` bundle, where the runtime unpacks the app next to its
  interpreter and `__file__` points at that copy.

Since every payload is looked up by the same relative path, a single ordered
list of candidate directories serves all three: the one that exists wins.
"""

from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path


def app_dirs() -> tuple[Path, ...]:
    """Directories that may hold the app's own files, most likely first."""
    dirs: list[Path] = []
    if (frozen := getattr(sys, "_MEIPASS", None)) is not None:
        dirs.append(Path(frozen))
    dirs.append(Path(__file__).resolve().parent)
    return tuple(dirs)


@lru_cache(maxsize=None)
def find(relative: str) -> Path | None:
    """The first existing candidate for `relative`, or None when there is none."""
    for directory in app_dirs():
        candidate = directory / relative
        if candidate.exists():
            return candidate
    return None


def assets_dir() -> str | None:
    """Path of `assets/` for `ft.run(assets_dir=...)`; None when there is none."""
    found = find("assets")
    return str(found) if found is not None else None


def locales_dir() -> Path:
    """Directory the locale files are read from (which may not exist)."""
    found = find("locales")
    return found if found is not None else Path(__file__).resolve().parent / "locales"
