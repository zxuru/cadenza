"""A result row's play cell, and the trap that once killed the whole list.

A row is routinely off the page when it is told to go back to rest: a new
search clears the list first, and the download button resets every row before
it starts. Anything that pushes to the client from there raises on a detached
control, and the exception escapes through the caller - which is how a stale
`update()` in this helper stopped both the results list and the download
button from doing anything at all.
"""

from __future__ import annotations

import flet as ft
import pytest

import main


def detached_row() -> tuple[ft.IconButton, ft.ProgressRing]:
    """A row exactly as a cleared results list leaves it: off the page."""
    return (
        ft.IconButton(icon=ft.Icons.PLAY_ARROW),
        ft.ProgressRing(width=20, height=20, visible=False),
    )


@pytest.mark.parametrize(
    "state", [main.PREVIEW_IDLE, main.PREVIEW_LOADING, main.PREVIEW_PLAYING]
)
def test_a_detached_row_never_raises(state):
    button, spinner = detached_row()
    main.apply_preview_state(button, spinner, state)  # must not raise


def test_idle_shows_the_arrow_and_hides_the_spinner():
    button, spinner = detached_row()
    main.apply_preview_state(button, spinner, main.PREVIEW_LOADING)
    main.apply_preview_state(button, spinner, main.PREVIEW_IDLE)
    assert button.icon == ft.Icons.PLAY_ARROW
    assert button.visible is True
    assert spinner.visible is False


def test_loading_shows_the_spinner_in_place_of_the_arrow():
    button, spinner = detached_row()
    main.apply_preview_state(button, spinner, main.PREVIEW_LOADING)
    assert spinner.visible is True
    assert button.visible is False


def test_playing_shows_the_square_that_stops_it():
    button, spinner = detached_row()
    main.apply_preview_state(button, spinner, main.PREVIEW_PLAYING)
    assert button.icon == ft.Icons.STOP
    assert button.visible is True
    assert spinner.visible is False


def test_a_missing_widget_is_not_an_error():
    button, spinner = detached_row()
    main.apply_preview_state(None, spinner, main.PREVIEW_PLAYING)
    main.apply_preview_state(button, None, main.PREVIEW_IDLE)
