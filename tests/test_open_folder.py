"""Opening the download folder, and the chip that asks for it.

The click cannot be made by hand in a test, so what is checked here is the part
that decides what a click does: which launcher a platform gets, that the chip
is inert when there is no file manager to hand the path to, and that the
settings bar keeps its two-column shape.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import flet as ft
import pytest

import components
import main


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", ["open", "/music"]),
        ("linux", ["xdg-open", "/music"]),
    ],
)
def test_opening_a_folder_uses_the_platform_launcher(monkeypatch, platform, expected):
    commands: list[list[str]] = []

    def record(command, **kwargs):
        commands.append(command)

    monkeypatch.setattr(subprocess, "Popen", record)
    monkeypatch.setattr(sys, "platform", platform)

    main.reveal_folder(Path("/music"))

    assert commands == [expected]


def test_windows_gets_its_own_spelling(monkeypatch, tmp_path):
    """`os.startfile` is Windows-only, so the branch is reached by name."""
    opened: list[Path] = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(main.os, "startfile", opened.append, raising=False)

    main.reveal_folder(tmp_path)

    assert opened == [tmp_path]


# ------------------------------------------------------------------- the chip


def test_the_folder_chip_opens_the_folder_it_shows():
    opened: list[bool] = []
    path_text = ft.Text("/music")

    chip = components.folder_chip(
        components.folder_icon_badge(), path_text, lambda: opened.append(True), "Open folder"
    )
    assert chip.on_click is not None
    chip.on_click(None)  # the click, as the client delivers it

    assert opened == [True]
    assert chip.tooltip == "Open folder"
    assert path_text.expand is True
    assert path_text.overflow == ft.TextOverflow.ELLIPSIS


def test_a_chip_with_nowhere_to_go_does_nothing():
    """A phone has no file manager: the chip shows the path and stays inert."""
    chip = components.folder_chip(components.folder_icon_badge(), ft.Text("/music"))

    assert chip.on_click is None


def test_the_settings_bar_folds_the_format_below_the_path():
    bar = components.settings_bar(ft.Container(), ft.Container(), ft.Dropdown())

    assert isinstance(bar, ft.ResponsiveRow)
    assert [panel.col for panel in bar.controls] == [
        {"xs": 12, "md": 8},
        {"xs": 12, "md": 4},
    ]
