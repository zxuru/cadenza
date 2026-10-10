"""Styled controls for the Cadenza shell, built from the tokens in `styles.py`.

Everything here takes data - a string, a control already built, a callback - and
returns a control that is already themed: hover, press and disabled states, a
radius, a border and an animation. `main.py` owns behavior and translation, this
module owns the look, and neither holds a style value of its own.
"""

from __future__ import annotations

from collections.abc import Callable

import flet as ft

from styles import (
    ANIM_MS,
    PALETTE as P,
    RADIUS as R,
    SIZE as Z,
    SPACE as S,
    body,
    glow,
    hairline,
    label,
    readout,
    spine,
    tint,
    title,
)

# One animation for every implicit transition, so the shell moves as one piece.
ANIM = ft.Animation(ANIM_MS, ft.AnimationCurve.EASE_OUT)


# --------------------------------------------------------------------- backdrop


def _grid_cell() -> ft.Container:
    return ft.Container(
        expand=True,
        border=hairline(alpha=0.05),
        ignore_interactions=True,
    )


def _grid_row(columns: int) -> ft.Row:
    return ft.Row(
        [_grid_cell() for _ in range(columns)],
        spacing=0,
        expand=True,
        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
    )


def grid_backdrop(rows: int = 8, columns: int = 14) -> ft.Control:
    """The grid behind the result list.

    Every cell ignores pointer events: a backdrop that takes a click is a button
    nothing can press.
    """
    return ft.Column(
        [_grid_row(columns) for _ in range(rows)],
        spacing=0,
        expand=True,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )


# ----------------------------------------------------------------------- header


def app_mark() -> ft.Container:
    """The glowing plate on the left of the title: decoration, dropped when the
    window is too narrow for decoration."""
    return ft.Container(
        content=ft.Icon(ft.Icons.GRAPHIC_EQ, size=22, color=P["primary"]),
        width=40,
        height=40,
        alignment=ft.Alignment(0, 0),
        border=hairline(alpha=0.35),
        border_radius=R["control"],
        bgcolor=tint(P["primary"], 0.08),
        shadow=glow(P["primary"], blur=18, alpha=0.30),
    )


def header(mark: ft.Control, app_name: ft.Text, badge: ft.Control) -> ft.Control:
    """The app mark, the name, and the state chip on the right."""
    return ft.Row(
        [
            mark,
            app_name,
            ft.Container(expand=True),
            badge,
        ],
        spacing=S["md"],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def status_chip(badge: ft.Text) -> ft.Container:
    """The chip that says what the app is doing; `badge` is recoloured in place."""
    return ft.Container(
        content=badge,
        padding=ft.Padding.symmetric(vertical=S["xs"], horizontal=S["md"]),
        border_radius=R["chip"],
        border=hairline(alpha=0.30),
        bgcolor=tint(P["primary"], 0.10),
        animate=ANIM,
    )


# -------------------------------------------------------------- settings strip


def settings_bar(
    folder_chip: ft.Control,
    change_folder: ft.Control,
    format_dropdown: ft.Dropdown,
) -> ft.Control:
    """The folder and the format, one line when there is room for two.

    Both panels are one token tall whatever they hold: the folder's chip and the
    format's label are different heights on their own, and a row of two boxes
    that disagree reads as a bug.

    A phone stacks them: the path takes the first row on its own and the format
    stretches across the second.
    """
    folder_panel = ft.Container(
        content=ft.Row(
            [folder_chip, change_folder],
            spacing=S["xs"],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        height=Z["panel"],
        padding=ft.Padding.symmetric(horizontal=S["sm"]),
        alignment=ft.Alignment(-1, 0),
        border=hairline(alpha=0.16),
        border_radius=R["card"],
        bgcolor=tint(P["surface_2"], 0.55),
        col={"xs": 12, "md": 8},
    )
    format_panel = ft.Container(
        content=format_dropdown,
        height=Z["panel"],
        padding=ft.Padding.symmetric(horizontal=S["sm"]),
        border=hairline(alpha=0.16),
        border_radius=R["card"],
        bgcolor=tint(P["surface_2"], 0.55),
        col={"xs": 12, "md": 4},
        alignment=ft.Alignment(0, 0),
    )
    return ft.ResponsiveRow(
        [folder_panel, format_panel],
        spacing=S["md"],
        run_spacing=S["sm"],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


def _hovering(event: ft.ControlEvent) -> bool:
    """Flet 1.x sends the hover flag as a bool, older clients as "true"/"false"."""
    return event.data is True or str(event.data).lower() == "true"


def _tint_on_hover(event: ft.ControlEvent, color: str) -> None:
    """Light a control up under the pointer; a detached one is left alone."""
    event.control.bgcolor = color if _hovering(event) else None  # type: ignore[attr-defined]
    try:
        event.control.update()
    except RuntimeError:
        pass  # the window closed under the pointer


def folder_chip(
    icon: ft.Control,
    path_text: ft.Text,
    on_open: Callable | None = None,
    tooltip: str = "",
) -> ft.Container:
    """The download folder, as a chip that opens it in the file manager.

    `on_open` is None where there is no file manager to hand the path to: the
    chip then shows the path and does nothing.
    """
    # The path ellipsizes instead of pushing the button off a narrow screen.
    path_text.max_lines = 1
    path_text.overflow = ft.TextOverflow.ELLIPSIS
    path_text.expand = True
    row = ft.Row(
        [icon, path_text],
        spacing=S["sm"],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        expand=True,
    )
    padding = ft.Padding.symmetric(vertical=S["xs"], horizontal=S["sm"])
    if on_open is None:
        return ft.Container(content=row, expand=True, padding=padding)
    return ft.Container(
        content=row,
        expand=True,
        padding=padding,
        border_radius=R["control"],
        tooltip=tooltip,
        ink=True,
        ink_color=tint(P["primary"], 0.10),
        on_click=lambda _: on_open(),
        on_hover=lambda event: _tint_on_hover(event, tint(P["primary"], 0.10)),
        animate=ANIM,
    )


def change_folder_button(text: str, on_click: Callable | None) -> ft.TextButton:
    return ghost_button(text, ft.Icons.DRIVE_FILE_MOVE_OUTLINED, on_click)


def folder_icon_badge() -> ft.Control:
    return ft.Container(
        content=ft.Icon(ft.Icons.FOLDER_OPEN, size=15, color=P["primary"]),
        width=26,
        height=26,
        alignment=ft.Alignment(0, 0),
        border_radius=R["control"],
        bgcolor=tint(P["primary"], 0.10),
    )


def _field_border(color: str, alpha: float, radius: int) -> ft.OutlineInputBorder:
    """The 1-px frame a field wears at rest; the caller adds the focused one."""
    return ft.OutlineInputBorder(
        side=ft.BorderSide(1, tint(color, alpha)),
        border_radius=radius,
    )


def format_dropdown(label_text: str, options: list[ft.DropdownOption], value: str | None, on_select: Callable) -> ft.Dropdown:
    """The output format picker, styled and left to fill its column."""
    return ft.Dropdown(
        label=label_text,
        value=value,
        options=options,
        on_select=on_select,
        width=None,
        dense=True,
        filled=True,
        fill_color=tint(P["surface_3"], 0.55),
        bgcolor=tint(P["surface_3"], 0.55),
        border={
            ft.ControlState.DEFAULT: _field_border(P["primary"], 0.22, R["control"]),
            ft.ControlState.FOCUSED: _field_border(P["primary"], 0.85, R["control"]),
        },
        text_style=readout(13, P["text"]),
        label_style=label(P["text_dim"]),
        hint_style=label(P["text_faint"]),
        content_padding=ft.Padding.symmetric(vertical=S["sm"], horizontal=S["md"]),
        color=P["text"],
    )


# ------------------------------------------------------------------- search bar


def query_field(label_text: str, hint_text: str, suffix: ft.Control, on_change: Callable, on_submit: Callable) -> ft.TextField:
    return ft.TextField(
        label=label_text,
        hint_text=hint_text,
        expand=True,
        autofocus=True,
        # One height for the field and the button beside it: the label floats
        # above the text, so the field is taller than a plain input by design.
        height=Z["field"],
        # The label floats above the text: without this the value sits low, with
        # a tall empty gap above it.
        text_vertical_align=ft.VerticalAlignment.CENTER,
        content_padding=ft.Padding.symmetric(vertical=14, horizontal=S["md"]),
        suffix=suffix,
        on_change=on_change,
        on_submit=on_submit,
        filled=True,
        fill_color=tint(P["surface_2"], 0.70),
        bgcolor=tint(P["surface_2"], 0.70),
        border={
            ft.ControlState.DEFAULT: _field_border(P["primary"], 0.25, R["control"]),
            ft.ControlState.FOCUSED: _field_border(P["primary"], 1.0, R["control"]),
        },
        cursor_color=P["primary"],
        cursor_height=20,
        selection_color=tint(P["primary"], 0.22),
        text_style=body(14),
        label_style=label(P["text_dim"]),
        hint_style=body(13, P["text_faint"]),
        color=P["text"],
    )


def search_bar(field: ft.TextField, button: ft.FilledButton) -> ft.Control:
    return ft.Row(
        [field, button],
        spacing=S["md"],
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )


# ---------------------------------------------------------------- results panel


def results_panel(list_view: ft.ListView, overlay: ft.Container) -> ft.Container:
    """The scrolling list over the grid, with the busy spinner stacked on top."""
    return ft.Container(
        content=ft.Stack(
            [grid_backdrop(), list_view, overlay],
            expand=True,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        ),
        expand=True,
        padding=ft.Padding.all(S["sm"]),
        border=hairline(alpha=0.18),
        border_radius=R["card"],
        bgcolor=tint(P["surface"], 0.55),
    )


def results_list(spacing: int = 4, top_padding: int = 4) -> ft.ListView:
    return ft.ListView(
        expand=True,
        spacing=spacing,
        padding=ft.Padding.only(top=top_padding),
    )


def leading_badge(icon: str, color: str) -> ft.Container:
    return ft.Container(
        content=ft.Icon(icon, size=18, color=color),
        width=34,
        height=34,
        alignment=ft.Alignment(0, 0),
        border_radius=R["control"],
        bgcolor=tint(color, 0.12),
        border=hairline(color, alpha=0.30),
    )


def result_tile(
    icon: str,
    icon_color: str,
    title_control: ft.Text,
    subtitle_control: ft.Text,
    trailing: list[ft.Control],
) -> ft.Control:
    """One search result: accent spine on the left, actions on the right."""
    tile = ft.ListTile(
        leading=leading_badge(icon, icon_color),
        title=title_control,
        subtitle=subtitle_control,
        trailing=ft.Row(trailing, spacing=0, tight=True),
        dense=True,
        min_height=58,
        content_padding=ft.Padding.symmetric(vertical=S["sm"], horizontal=S["md"]),
        bgcolor=tint(P["surface_2"], 0.55),
        hover_color=tint(P["primary"], 0.10),
        splash_color=tint(P["primary"], 0.14),
        icon_color=icon_color,
        shape=ft.RoundedRectangleBorder(radius=R["card"]),
    )
    return ft.Container(
        content=tile,
        border=spine(tint(P["primary"], 0.45)),
        border_radius=R["card"],
        margin=ft.Margin.only(bottom=S["xs"]),
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
    )


def busy_overlay(spinner: ft.ProgressRing, message: ft.Text, empty: ft.Text) -> ft.Container:
    """Centred over the list. Kept out of the layout by `sync_overlay` while it
    holds nothing: it fills the list area and would swallow every pointer event
    meant for the rows."""
    return ft.Container(
        content=ft.Column(
            [spinner, message, empty],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=S["md"],
        ),
        alignment=ft.Alignment(0, 0),
        expand=True,
        visible=False,
    )


def spinner(size: int = 40, stroke: int = 3) -> ft.ProgressRing:
    """A ring that starts hidden: `set_busy`, `show_busy` and `apply_preview_state`
    are the only things that show one."""
    return ft.ProgressRing(
        width=size,
        height=size,
        stroke_width=stroke,
        color=P["primary"],
        bgcolor=tint(P["primary"], 0.12),
        visible=False,
    )


def play_cell(button: ft.IconButton, ring: ft.ProgressRing) -> ft.Stack:
    """The spinner rides on top of the arrow: same cell, so showing it never
    moves the row."""
    return ft.Stack([button, ring], width=40, height=40, alignment=ft.Alignment(0, 0))


def progress_bar(bar_height: int = 6) -> ft.ProgressBar:
    return ft.ProgressBar(
        visible=False,
        value=0,
        bar_height=bar_height,
        border_radius=R["control"],
        color=P["primary"],
        bgcolor=tint(P["primary"], 0.14),
    )


def status_line(text_control: ft.Text) -> ft.Container:
    """The one line that says what is happening: a readout, so monospace."""
    return ft.Container(
        content=text_control,
        padding=ft.Padding.symmetric(vertical=S["xs"], horizontal=S["sm"]),
        border=ft.Border(left=ft.BorderSide(2, tint(P["primary"], 0.45))),
    )


# ----------------------------------------------------------------------- pieces


def divider() -> ft.Divider:
    return ft.Divider(
        height=1,
        thickness=1,
        color=tint(P["primary"], 0.18),
    )


def footer(controls: list[ft.Control]) -> ft.Control:
    """The bottom strip: version, update button, retry button.

    It wraps rather than scrolls sideways: a phone shows the version and the
    buttons on two lines instead of pushing the last one off the screen.
    """
    return ft.Container(
        content=ft.Row(
            controls,
            spacing=S["md"],
            run_spacing=S["xs"],
            wrap=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.Padding.only(top=S["xs"]),
        border=ft.Border(top=ft.BorderSide(1, tint(P["primary"], 0.14))),
    )


# ---------------------------------------------------------------------- buttons


def icon_button(
    icon: str,
    tooltip: str,
    on_click: Callable | None,
    *,
    color: str | None = None,
    size: int = 20,
    disabled: bool = False,
) -> ft.IconButton:
    """A quiet icon button: no plate until the pointer is over it."""
    accent = color or P["primary"]
    state_color = {
        ft.ControlState.DEFAULT: tint(accent, 0.85),
        ft.ControlState.HOVERED: P["text"],
        ft.ControlState.PRESSED: accent,
        ft.ControlState.DISABLED: P["text_faint"],
    }
    return ft.IconButton(
        icon=icon,
        icon_size=size,
        tooltip=tooltip,
        on_click=on_click,
        disabled=disabled,
        icon_color=state_color,
        hover_color=tint(accent, 0.14),
        highlight_color=tint(accent, 0.20),
        splash_color=tint(accent, 0.20),
        splash_radius=R["pill"],
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=R["control"]),
            animation_duration=ANIM_MS,
            overlay_color={
                ft.ControlState.HOVERED: tint(accent, 0.14),
                ft.ControlState.PRESSED: tint(accent, 0.22),
                ft.ControlState.DISABLED: ft.Colors.TRANSPARENT,
            },
        ),
    )


def primary_button(
    text: str,
    icon: str | None,
    on_click: Callable | None,
    *,
    disabled: bool = False,
    height: int | None = None,
) -> ft.FilledButton:
    """The one filled button in the shell: the search, and the dialog confirms.

    `shadow_color` is a colour, not a shadow: this field feeds the Material
    elevation's tint, and a `BoxShadow` there left the client laying the row out
    to an unbounded height (the whole shell below the search row collapsed).
    """
    foreground = {
        ft.ControlState.DEFAULT: P["primary"],
        ft.ControlState.HOVERED: P["text"],
        ft.ControlState.PRESSED: P["text"],
        ft.ControlState.DISABLED: P["text_faint"],
    }
    return ft.FilledButton(
        content=text,
        icon=icon,
        on_click=on_click,
        disabled=disabled,
        height=height or Z["button"],
        style=ft.ButtonStyle(
            bgcolor={
                ft.ControlState.DEFAULT: tint(P["primary"], 0.12),
                ft.ControlState.HOVERED: tint(P["primary"], 0.26),
                ft.ControlState.PRESSED: tint(P["primary"], 0.38),
                ft.ControlState.DISABLED: tint(P["surface_3"], 0.55),
            },
            color=foreground,
            icon_color=foreground,
            side=ft.BorderSide(1, tint(P["primary"], 0.60)),
            shape=ft.RoundedRectangleBorder(radius=R["control"]),
            elevation=2,
            shadow_color=tint(P["primary"], 0.45),
            animation_duration=ANIM_MS,
            padding=ft.Padding.symmetric(vertical=S["sm"], horizontal=S["lg"]),
        ),
    )


def ghost_button(
    text: str,
    icon: str | None,
    on_click: Callable | None,
    *,
    color: str | None = None,
    disabled: bool = False,
) -> ft.TextButton:
    """A text button: the folder, the updater, the dialog cancels."""
    accent = color or P["text_dim"]
    return ft.TextButton(
        content=text,
        icon=icon,
        on_click=on_click,
        disabled=disabled,
        style=ft.ButtonStyle(
            color={
                ft.ControlState.DEFAULT: accent,
                ft.ControlState.HOVERED: P["primary"],
                ft.ControlState.PRESSED: P["primary"],
                ft.ControlState.DISABLED: P["text_faint"],
            },
            icon_color={
                ft.ControlState.DEFAULT: accent,
                ft.ControlState.HOVERED: P["primary"],
                ft.ControlState.DISABLED: P["text_faint"],
            },
            overlay_color={
                ft.ControlState.HOVERED: tint(P["primary"], 0.10),
                ft.ControlState.PRESSED: tint(P["primary"], 0.16),
            },
            shape=ft.RoundedRectangleBorder(radius=R["control"]),
            animation_duration=ANIM_MS,
            padding=ft.Padding.symmetric(vertical=S["sm"], horizontal=S["md"]),
        ),
    )


# ---------------------------------------------------------------------- dialogs


def dialog(
    title_control: ft.Control,
    content: ft.Control,
    actions: list[ft.Control],
) -> ft.AlertDialog:
    """A modal panel: hairline border, flat corners, no elevation flood."""
    return ft.AlertDialog(
        modal=True,
        bgcolor=P["surface"],
        elevation=0,
        barrier_color=tint(P["bg"], 0.72),
        inset_padding=ft.Padding.all(S["xl"]),
        actions_padding=ft.Padding.only(
            left=S["xl"], right=S["xl"], bottom=S["lg"], top=S["sm"]
        ),
        content_padding=ft.Padding.only(
            left=S["xl"], right=S["xl"], top=S["sm"], bottom=S["sm"]
        ),
        title_padding=ft.Padding.only(left=S["xl"], right=S["xl"], top=S["lg"]),
        shape=ft.RoundedRectangleBorder(
            radius=R["card"],
            side=ft.BorderSide(1, tint(P["primary"], 0.35)),
        ),
        title=ft.Container(content=title_control, border=ft.Border(
            bottom=ft.BorderSide(1, tint(P["primary"], 0.25))
        )),
        content=content,
        actions=actions,
        actions_alignment=ft.MainAxisAlignment.END,
        title_text_style=title(),
        content_text_style=body(),
    )
