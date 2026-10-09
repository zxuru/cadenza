"""The app must ask for the desktop client it ships, whatever the cwd.

`flet_desktop` picks the client flavor at startup from an environment variable,
then from the `pyproject.toml` of the current directory, then `light` on Linux.
The light client ships without `libaudioplayers_linux_plugin.so`, and without
that plugin the audio service is never registered in the client: every call on
it - the 30s preview - waits for an answer that never arrives and dies on a
ten-second timeout. That is what happened: the same executable, run from the
project directory, played fine, and run from anywhere else did not.
"""

from __future__ import annotations

import os

import main


def test_the_app_asks_for_the_full_client(monkeypatch):
    monkeypatch.delenv("FLET_DESKTOP_FLAVOR", raising=False)
    main.pin_client_flavor()
    assert os.environ["FLET_DESKTOP_FLAVOR"] == "full"


def test_it_overrides_whatever_the_environment_said(monkeypatch):
    """Not `setdefault`: a leftover `light` would take the preview away."""
    monkeypatch.setenv("FLET_DESKTOP_FLAVOR", "light")
    main.pin_client_flavor()
    assert os.environ["FLET_DESKTOP_FLAVOR"] == "full"
