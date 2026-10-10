"""Design tokens for the Cadenza shell: every colour, space and shape value.

The app is styled retro-futuristic: a near-black ground, cyan primary, magenta
accent, monospace readouts, hairline borders and glow only on accents. Nothing
outside this module and `components.py` holds a raw style value, so the look is
changed by changing this file.
"""

from __future__ import annotations

import flet as ft

# Flutter resolves `monospace` through the platform font manager and falls back
# to the default family where it cannot: no font file is shipped, and no text is
# worse off for asking.
MONO = "monospace"

PALETTE: dict[str, str] = {
    # Ground, and the two steps of raised surface above it.
    "bg": "#080910",
    "surface": "#141520",
    "surface_2": "#1B1D2B",
    "surface_3": "#232637",
    # Accents. `primary` is the one that glows; `accent` marks, never floods.
    "primary": "#00F2FE",
    "primary_dim": "#0B6E76",
    "accent": "#FF0055",
    "ok": "#39FF14",
    "warn": "#FFB020",
    "error": "#FF3B4E",
    # Type.
    "text": "#E0E0E0",
    "text_dim": "#8B93A7",
    "text_faint": "#5A6172",
    # A glow is an accent at a fraction of its strength, never a colour of its
    # own: the keys below are the ones the shell reaches for by name.
    "glow": ft.Colors.with_opacity(0.35, "#00F2FE"),
    "glow_accent": ft.Colors.with_opacity(0.35, "#FF0055"),
}

SPACE: dict[str, int] = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32}

# Fixed heights for the two things that sit next to each other: the settings
# panels and the primary button. A row of controls whose heights differ reads as
# a mistake, not as a hierarchy. `field` is the natural height of a filled
# Material text field with its label floating.
SIZE: dict[str, int] = {"panel": 64, "button": 44, "field": 60}

RADIUS: dict[str, int] = {"panel": 0, "card": 2, "control": 2, "chip": 10, "pill": 999}

# Hover, press and fade all run at this speed: fast enough to feel mechanical.
ANIM_MS = 160


def _style(
    size: int,
    color: str,
    *,
    weight: ft.FontWeight | None = None,
    spaced: float | None = None,
    mono: bool = False,
    glow_color: str | None = None,
    blur: int = 12,
) -> ft.TextStyle:
    return ft.TextStyle(
        size=size,
        color=color,
        weight=weight,
        letter_spacing=spaced,
        font_family=MONO if mono else None,
        shadow=(
            ft.BoxShadow(
                blur_radius=blur,
                color=ft.Colors.with_opacity(0.5, glow_color),
            )
            if glow_color
            else None
        ),
    )


def display(color: str | None = None, *, glow_color: str | None = None) -> ft.TextStyle:
    """The app name, and nothing else."""
    return _style(
        24,
        color or PALETTE["text"],
        weight=ft.FontWeight.BOLD,
        spaced=3,
        mono=True,
        glow_color=glow_color or PALETTE["primary"],
    )


def title(color: str | None = None, *, glow_color: str | None = None) -> ft.TextStyle:
    """A dialog title, or the heading of a panel."""
    return _style(
        16,
        color or PALETTE["text"],
        weight=ft.FontWeight.W_600,
        spaced=1.2,
        mono=True,
        glow_color=glow_color,
    )


def body(size: int = 13, color: str | None = None) -> ft.TextStyle:
    """Running text and result titles: the only sans in the ramp."""
    return _style(size, color or PALETTE["text"])


def label(color: str | None = None, *, spaced: float = 1.5) -> ft.TextStyle:
    """Small uppercase captions, chips and counters."""
    return _style(
        11,
        color or PALETTE["text_dim"],
        weight=ft.FontWeight.W_600,
        spaced=spaced,
        mono=True,
    )


def readout(size: int = 12, color: str | None = None) -> ft.TextStyle:
    """Numbers and paths: a machine's answer, so monospace."""
    return _style(size, color or PALETTE["text_dim"], mono=True)


def glow(
    color: str,
    *,
    blur: int = 16,
    alpha: float = 0.55,
    spread: int = 0,
    offset: tuple[int, int] = (0, 0),
) -> ft.BoxShadow:
    """The one glow in the app, tinted by whoever is asking for it."""
    return ft.BoxShadow(
        blur_radius=blur,
        spread_radius=spread,
        offset=ft.Offset(*offset),
        color=ft.Colors.with_opacity(alpha, color),
    )


def hairline(color: str | None = None, *, alpha: float = 0.28) -> ft.Border:
    """A 1-px border on all four sides: the default edge of every panel."""
    side = ft.BorderSide(1, ft.Colors.with_opacity(alpha, color or PALETTE["primary"]))
    return ft.Border(left=side, top=side, right=side, bottom=side)


def spine(color: str | None = None, *, width: int = 2) -> ft.Border:
    """A single accent edge on the left of a row: the result-list marker."""
    return ft.Border(
        left=ft.BorderSide(width, color or PALETTE["primary"]),
        top=ft.BorderSide(1, ft.Colors.with_opacity(0.12, PALETTE["primary"])),
        right=ft.BorderSide(1, ft.Colors.with_opacity(0.12, PALETTE["primary"])),
        bottom=ft.BorderSide(1, ft.Colors.with_opacity(0.12, PALETTE["primary"])),
    )


def tint(color: str, alpha: float) -> str:
    """A translucent wash of a palette colour; derived, never a new literal."""
    return ft.Colors.with_opacity(alpha, color)


def wash() -> ft.BoxDecoration:
    """What the root view wears: a cyan glow at the top fading into the ground.

    It rides on the page decoration rather than on a Stack layer, so the shell's
    own controls are the page's direct children and nothing sits between them
    and the window's height.
    """
    return ft.BoxDecoration(
        gradient=ft.LinearGradient(
            begin=ft.Alignment.TOP_CENTER,
            end=ft.Alignment.BOTTOM_CENTER,
            colors=[tint(PALETTE["primary"], 0.12), tint(PALETTE["primary"], 0.0)],
        )
    )


def theme() -> ft.Theme:
    """The page theme: what a control nobody restyled still falls back to."""
    return ft.Theme(
        color_scheme=ft.ColorScheme(
            primary=PALETTE["primary"],
            on_primary=PALETTE["bg"],
            secondary=PALETTE["accent"],
            on_secondary=PALETTE["text"],
            surface=PALETTE["surface"],
            on_surface=PALETTE["text"],
            surface_container_highest=PALETTE["surface_3"],
            outline=PALETTE["text_faint"],
            error=PALETTE["error"],
            on_error=PALETTE["text"],
            shadow=PALETTE["bg"],
            scrim=PALETTE["bg"],
        ),
        visual_density=ft.VisualDensity.COMPACT,
        scaffold_bgcolor=PALETTE["bg"],
        canvas_color=PALETTE["bg"],
        card_bgcolor=PALETTE["surface"],
        divider_color=tint(PALETTE["primary"], 0.18),
        hint_color=PALETTE["text_faint"],
        splash_color=tint(PALETTE["primary"], 0.12),
        highlight_color=tint(PALETTE["primary"], 0.08),
        hover_color=tint(PALETTE["primary"], 0.08),
        focus_color=tint(PALETTE["primary"], 0.16),
        unselected_control_color=PALETTE["text_dim"],
        disabled_color=PALETTE["text_faint"],
    )
