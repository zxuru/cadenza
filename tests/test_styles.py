"""The tokens and the types they are handed to.

The bug this guards: `ButtonStyle.shadow_color` was given a `BoxShadow` instead
of a colour. Flet accepted it and the client laid the row out to an unbounded
height, so everything below the search row - the input, the list, the status,
the footer - collapsed off the window and the app looked finished while nothing
in it could be used.
"""

from __future__ import annotations

import ast
from pathlib import Path

import flet as ft

import components
import styles

PROJECT = Path(__file__).resolve().parents[1]


def test_no_shadow_is_handed_to_a_colour_field():
    """`glow()` builds the one glow, and it belongs in `shadow=`, never in a
    field whose name says colour."""
    bad: list[tuple[int, str]] = []
    for name in ("components.py", "main.py"):
        tree = ast.parse((PROJECT / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if not keyword.arg or "color" not in keyword.arg:
                    continue
                value = keyword.value
                if isinstance(value, ast.Call) and getattr(value.func, "id", "") == "glow":
                    bad.append((node.lineno, f"{name}:{keyword.arg}"))
    assert not bad, f"a shadow where a colour belongs: {bad}"


def _field() -> ft.TextField:
    return components.query_field("q", "hint", ft.IconButton(ft.Icons.CLEAR), None, None)


def test_the_filled_button_tints_its_shadow_with_a_colour():
    button = components.primary_button("Search", ft.Icons.SEARCH, None)

    assert isinstance(button.style.shadow_color, str)
    assert not isinstance(button.style.shadow_color, ft.BoxShadow)


def test_every_palette_entry_is_a_colour_string():
    for name, value in styles.PALETTE.items():
        assert isinstance(value, str), f"{name} is not a colour value"


def test_the_settings_panels_are_one_height():
    """The chip and the dropdown are different heights on their own; the panels
    around them must not be."""
    bar = components.settings_bar(ft.Container(), ft.Container(), ft.Dropdown())

    assert [panel.height for panel in bar.controls] == [styles.SIZE["panel"]] * 2


def test_the_input_and_the_button_are_sized_from_the_tokens():
    assert _field().height == styles.SIZE["field"]
    assert components.primary_button("go", None, None).height == styles.SIZE["button"]
    flush = components.primary_button("go", None, None, height=styles.SIZE["field"])
    assert flush.height == styles.SIZE["field"]


def test_the_input_wears_the_decoration_the_dropdown_wears():
    """The bug this guards: the field carried more padding than the dropdown
    beside it and a taller box, and the client spent the room it was left with
    above the value - the link sat low, under a hole its own label had made.
    The dropdown is the control in this shell that reads as centred, so the two
    are held to the same decoration."""
    field = _field()
    dropdown = components.format_dropdown("Format", [], None, None)

    assert field.dense == dropdown.dense
    assert field.content_padding == dropdown.content_padding


def test_the_input_is_the_height_the_client_centres_a_value_in():
    field = _field()
    padding = field.content_padding

    assert padding.top == padding.bottom, "a value off centre by its own padding"
    assert field.height == styles.SIZE["field"] >= 48, "Material's own minimum"
