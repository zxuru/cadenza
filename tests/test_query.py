"""The search box: the X, and when Buscar can run.

Typing shows the X; clearing (backspacing or clicking it) hides it again,
and Buscar enables only with non-blank text, never while busy. The X stays
laid out and only fades: removing the suffix resizes the field mid-typing.
"""

from __future__ import annotations

import flet as ft
import pytest

import main


def make_row() -> tuple[ft.TextField, ft.IconButton, ft.FilledButton]:
    field = ft.TextField()
    clear = ft.IconButton(icon=ft.Icons.CLEAR, opacity=0.0, disabled=True)
    search = ft.FilledButton(content="search", disabled=True)
    return field, clear, search


@pytest.mark.parametrize("value", [None, "", "   "])
def test_nothing_typed_keeps_the_x_hidden(value):
    field, clear, search = make_row()
    field.value = value
    assert main.refresh_query_widgets(field, clear, search, working=False) is False
    assert clear.opacity == 0.0
    assert clear.disabled is True
    assert search.disabled is True


@pytest.mark.parametrize("value", ["x", "  x  ", "una canción"])
def test_text_shows_the_x_and_enables_search(value):
    field, clear, search = make_row()
    field.value = value
    assert main.refresh_query_widgets(field, clear, search, working=False) is True
    assert clear.opacity == 1.0
    assert clear.disabled is False
    assert search.disabled is False


def test_busy_never_enables_search_but_keeps_the_x():
    field, clear, search = make_row()
    field.value = "x"
    assert main.refresh_query_widgets(field, clear, search, working=True) is True
    assert clear.disabled is False
    assert search.disabled is True


def test_a_saved_format_is_offered_back(tmp_path):
    import settings

    saved = settings.Settings(path=tmp_path / "config.json")
    saved.format = "mp3"
    saved.save()
    reloaded = settings.Settings.load(path=tmp_path / "config.json")
    assert reloaded.format == "mp3"


def test_a_format_nobody_saved_leaves_no_choice(tmp_path):
    import settings

    reloaded = settings.Settings.load(path=tmp_path / "config.json")
    assert reloaded.format is None


def test_clearing_the_text_hides_the_x_again():
    field, clear, search = make_row()
    field.value = "shake it"
    main.refresh_query_widgets(field, clear, search, working=False)
    assert clear.disabled is False
    field.value = ""
    assert main.refresh_query_widgets(field, clear, search, working=False) is False
    assert clear.opacity == 0.0
    assert clear.disabled is True
    assert search.disabled is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, False), ("", False), ("   ", False), ("x", True), ("  x  ", True)],
)
def test_the_predicate_ignores_blank_text(value, expected):
    assert main.query_has_text(value) is expected
