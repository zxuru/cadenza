"""Cadenza — search, confirm, then download music (Flet + yt-dlp + ffmpeg).

Run with `python main.py`, or `Cadenza --selftest` for a headless engine check.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import sys
from collections.abc import Callable
from pathlib import Path

import flet as ft
from flet.utils.platform_utils import is_android, is_mobile

try:
    import flet_audio as fta
except ImportError:  # preview player is optional: rows then play nothing
    fta = None  # type: ignore[assignment]

import bundle
import engine
import i18n
import logs
import pending
import settings as config
import update
import version
from engine import AlbumInfo, Progress, SearchResult, Track

POLL_INTERVAL = 0.1
WINDOW_SIZE = (760, 640)
WINDOW_MIN_SIZE = (560, 460)

# Version a `--updated` relaunch was performed to, so the new build can say so.
UPDATED_TO: str | None = None


def format_bytes(size: float | None) -> str:
    if not size:
        return "?"
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "--:--"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"

def query_has_text(value: str | None) -> bool:
    """A query worth acting on: the X shows and Buscar enables only then."""
    return bool((value or "").strip())


def refresh_query_widgets(
    field: ft.TextField,
    clear_btn: ft.IconButton,
    search_btn: ft.FilledButton,
    working: bool,
) -> bool:
    """Fade the X in and out, and enable Buscar; returns whether there is text.

    The X stays in the layout (opacity, never `visible`): adding or removing
    the suffix resizes the field on the keystroke that shows or hides it.
    """
    has_text = query_has_text(field.value)
    clear_btn.opacity = 1.0 if has_text else 0.0
    clear_btn.disabled = not has_text
    search_btn.disabled = working or not has_text
    return has_text


# The three states one row's play cell can be in.
PREVIEW_IDLE = "idle"
PREVIEW_LOADING = "loading"
PREVIEW_PLAYING = "playing"


def apply_preview_state(
    button: ft.IconButton | None,
    spinner: ft.ProgressRing | None,
    state: str,
) -> None:
    """Put one row's play cell into `state`, touching nothing but properties.

    A row is routinely off the page when this runs - a new search clears the
    list before it is replaced, and that is exactly when the previous rows are
    told to go back to rest - so this must never call `update()`: a detached
    control raises there, and the exception would escape through the caller
    (the render, or the download button's own handler) and kill it. Pushing
    what changed is the caller's `safe_update`.
    """
    if button is not None:
        button.icon = ft.Icons.STOP if state == PREVIEW_PLAYING else ft.Icons.PLAY_ARROW
        button.visible = state != PREVIEW_LOADING
    if spinner is not None:
        spinner.visible = state == PREVIEW_LOADING


def _read_tags(path: Path) -> dict[str, str]:
    """Read container tags back with the same ffmpeg the engine uses."""
    import subprocess

    ffmpeg = engine.find_ffmpeg()
    if ffmpeg is None:
        return {}
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    tags: dict[str, str] = {}
    for line in result.stderr.splitlines():
        stripped = line.strip()
        if not line.startswith("    ") or ":" not in stripped or stripped.startswith(":"):
            continue
        key, _, value = stripped.partition(":")
        tags.setdefault(key.strip(), value.strip())
    return tags


def _read_art(path: Path) -> tuple[str, int, int] | None:
    """Kind and pixel size of the cover picture in `path`, or None when there is none."""
    import subprocess

    ffmpeg = engine.find_ffmpeg()
    if ffmpeg is None:
        return None
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stderr.splitlines():
        match = re.search(r"Video: (\w+).*?(\d+)x(\d+).*\(attached pic\)", line)
        if match:
            return match.group(1), int(match.group(2)), int(match.group(3))
    return None


def selftest(download: bool = True) -> int:
    """Headless check of the bundled engine; `--selftest` runs it.

    Verifies a frozen build end to end: the version it was stamped with, ffmpeg,
    the JavaScript runtime, the yt-dlp extractors, the network path, and the
    tags and cover art written into the file. Downloads the default format, so a
    build that cannot embed art is caught here instead of in the user's download
    folder.

    `--no-download` stops before everything that needs YouTube to cooperate -
    a CI runner is answered with "Sign in to confirm you're not a bot", for
    single videos and often for the search as well, which says nothing about
    the build. What is left is what any machine can check: ffmpeg, the
    JavaScript runtime, the extractors, and that the frozen app starts.
    """
    lines: list[str] = []
    ok = True

    def report(name: str, value: str, passed: bool = True) -> None:
        nonlocal ok
        ok = ok and passed
        lines.append(f"{name:<12} {value}")

    ffmpeg = engine.find_ffmpeg()
    # What the build says it is: the workflow publishes a release per push and
    # checks this line against the version it stamped, so a build that lost its
    # stamp fails there instead of shipping as "1.0.0" forever.
    report("version", version.current())
    report("ffmpeg", ffmpeg or "NOT FOUND", ffmpeg is not None)
    report("js runtimes", ", ".join(engine.find_js_runtimes()))
    try:
        # Bundled through `--collect-all yt_dlp`: the registry is built by
        # scanning the package, so a build that ships the package without its
        # extractors would otherwise go unnoticed until the first search.
        from yt_dlp.extractor import gen_extractors

        report("extractors", str(len(gen_extractors())))
    except Exception as err:  # noqa: BLE001 - report, do not crash
        report("extractors", f"MISSING ({err})", False)
    try:
        # Bundled through `--hidden-import`: yt-dlp needs it to put cover art
        # into FLAC files, and nothing imports it directly.
        import mutagen

        report("mutagen", mutagen.version_string)
    except ImportError as err:
        report("mutagen", f"MISSING ({err})", False)

    if not download:
        report("search", "skipped (--no-download)")
        report("download", "skipped (--no-download)")
    else:
        try:
            results = engine.search("Kevin MacLeod Sneaky Snitch", limit=2)
            report(
                "search",
                f"{len(results)} result(s): {results[0].title}"
                if results
                else "no results",
                bool(results),
            )
        except Exception as err:  # noqa: BLE001 - report, do not crash
            report("search", f"FAILED: {err}", False)
        else:
            _selftest_download(results[0], report)

    text = "\n".join(lines)
    _emit(text, "selftest.txt")
    return 0 if ok else 1


def _emit(text: str, filename: str) -> None:
    """Print a headless report, or leave it in `filename` when there is no console.

    A windowed build (`--windowed` on Windows and macOS) has no stdout at all.
    """
    if sys.stdout is None:
        Path(filename).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def check_updates() -> int:
    """Headless update check; `--check-updates` runs it.

    The window is how the app updates, but this is how to see what its button
    would find without opening one: the feed, this build's version, and the
    artifact that would be installed. Nothing is downloaded.
    """
    token = update.env_token()
    found = update.check(token)
    lines = [
        f"{'version':<12} {version.current()}",
        f"{'asset':<12} {update.asset_name() or 'none for this platform'}",
        f"{'token':<12} {'given' if token else 'none'}",
    ]
    if found.release is not None:
        # A local build reads the feed like any other, but nothing is put in
        # its place: the line says what the button would do, not what it may.
        how = "the button installs it" if update.installable() else "this build does not replace itself"
        lines.append(f"{'release':<12} {found.release.tag} is newer: {how}")
    elif found.needs_token:
        lines.append(f"{'release':<12} unknown (private repository, no token)")
    elif found.error:
        lines.append(f"{'release':<12} check failed: {found.error}")
    else:
        lines.append(f"{'release':<12} none newer")
    _emit("\n".join(lines), "updatecheck.txt")
    return 1 if found.error else 0


def _selftest_download(result: SearchResult, report: Callable[..., None]) -> None:
    """Download one track and check what was written into it."""
    import tempfile

    try:
        with tempfile.TemporaryDirectory() as tmp:
            tracks = engine.download(result.url, engine.DEFAULT_FORMAT, Path(tmp))
            size = tracks[0].path.stat().st_size
            report("download", f"{tracks[0].path.name} ({format_bytes(size)})")
            tags = _read_tags(tracks[0].path)
            shown = " · ".join(
                f"{key}={tags[key]}"
                for key in ("artist", "album", "genre", "date")
                if tags.get(key)
            )
            report("tags", shown or "none", bool(tags.get("title") and tags.get("artist")))
            art = _read_art(tracks[0].path)
            if art is None:
                report("cover art", "none", False)
            else:
                # Either the square cover a music database served, or the
                # video thumbnail cropped to 4:3. What fails here is a build
                # that embeds YouTube's 16:9 frame unmodified.
                kind, width, height = art
                ratio = width / height
                report(
                    "cover art",
                    f"{kind} {width}x{height}",
                    abs(ratio - 4 / 3) < 0.01 or abs(ratio - 1) < 0.01,
                )
    except Exception as err:  # noqa: BLE001
        report("download", f"FAILED: {err}", False)


def main(page: ft.Page) -> None:
    t = i18n.Translator()

    def run_task(handler: Callable, *args: object) -> None:
        """`page.run_task`, with a traceback left behind when the task fails.

        A task that raises dies inside the event loop: the window keeps its
        last state and nothing on disk says what happened. Every background
        task in this window starts here for that reason - see `logs.watching`.
        """
        page.run_task(logs.watching(handler), *args)

    # A phone or a tablet has no window to size, reveal or give an icon: every
    # `page.window` property below is a desktop-client concern, and a browser
    # tab has no window of its own either.
    desktop = not page.web and not is_mobile()

    page.title = config.APP_NAME
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20
    if desktop:
        # The client opens its window at its own default size, so start hidden
        # and reveal it once the real geometry is known - otherwise it visibly
        # resizes.
        page.window.width, page.window.height = WINDOW_SIZE
        page.window.min_width, page.window.min_height = WINDOW_MIN_SIZE
        # Windows takes the window/taskbar icon from here; Linux uses the app
        # id plus the desktop entry (see packaging/).
        page.window.icon = "icon.ico"

    state = config.Settings.load()

    file_picker = ft.FilePicker()
    page.services.append(file_picker)
    # Mobile only: the directory the system gives this app for its files.
    storage_paths = ft.StoragePaths()
    page.services.append(storage_paths)
    # The 30s sample each track row can play: one player shared by the list,
    # built the first time a row is played. `src` is required by the control,
    # so a player made before there is anything to play has to be handed some
    # stand-in source - and every platform fails to open a different one, which
    # is an error printed on startup and on every update for a sound nobody
    # asked for. Made on the press, it is born with the sample already set.
    has_audio = fta is not None
    preview_player: fta.Audio | None = None

    badge_label = ft.Text(
        t("badge_ready"), size=12, weight=ft.FontWeight.W_600, color=ft.Colors.GREY_400
    )
    status_badge = ft.Container(
        content=badge_label,
        padding=ft.Padding.symmetric(vertical=4, horizontal=10),
        border_radius=12,
        bgcolor=ft.Colors.with_opacity(0.15, ft.Colors.GREY_400),
    )

    folder_text = ft.Text(size=12, color=ft.Colors.GREY_400, max_lines=1)
    if not desktop:
        # A phone puts a long path next to a button: let the path ellipsize
        # instead of pushing the button off the screen.
        folder_text.expand = True
        folder_text.overflow = ft.TextOverflow.ELLIPSIS
    # A phone has no ffmpeg executable: only the formats its bundled encoder
    # can produce are offered, so the dropdown never promises a conversion that
    # would fail halfway through a download. Nothing at all can be produced
    # when neither ffmpeg nor the in-process converter is present; the dropdown
    # then simply holds no choice.
    formats = engine.available_formats()
    saved_format = state.format if state.format in formats else None
    if state.format is not None and saved_format is None:
        # A format the machine can no longer produce (Android without MP3):
        # forget it now instead of offering a download that fails halfway.
        state.format = None
        state.save()
    format_dropdown = ft.Dropdown(
        label=t("format"),
        width=200,
        value=saved_format or (engine.DEFAULT_FORMAT if engine.DEFAULT_FORMAT in formats else None),
        options=[ft.DropdownOption(key, t(f"format_{key}")) for key in formats],
        on_select=lambda _: remember_format(),
    )

    clear_btn = ft.IconButton(
        icon=ft.Icons.CLEAR,
        icon_size=20,
        tooltip=t("clear_query"),
        # Always laid out, only faded: `visible=False` removes the suffix and
        # the field resizes on the keystroke that shows or hides it.
        opacity=0.0,
        disabled=True,
        on_click=lambda _: clear_query(),
    )
    query_field = ft.TextField(
        label=t("query_label"),
        hint_text=t("query_hint"),
        expand=True,
        autofocus=True,
        # The label floats above the text: without this the value sits low,
        # with a tall empty gap above it.
        text_vertical_align=ft.VerticalAlignment.CENTER,
        content_padding=ft.Padding.symmetric(vertical=14, horizontal=12),
        suffix=clear_btn,
        on_change=lambda _: refresh_query_state(),
        on_submit=lambda _: start_search(),
    )
    search_btn = ft.FilledButton(
        content=t("search"),
        icon=ft.Icons.SEARCH,
        disabled=True,
        on_click=lambda _: start_search(),
    )

    results_list = ft.ListView(expand=True, spacing=2, padding=ft.Padding.only(top=8))
    # Shown while a search or an album read runs: a spinner in the middle of the
    # list area, which is also where an empty search says it found nothing.
    busy_spinner = ft.ProgressRing(width=40, height=40, visible=False)
    busy_label = ft.Text("", size=13, color=ft.Colors.BLUE_200, visible=False)
    empty_text = ft.Text(t("status_empty"), size=13, color=ft.Colors.GREY_500, visible=False)
    # The overlay that centres the spinner over the list. It is kept out of the
    # layout unless it has something to say: a Container that fills the list
    # area sits above it and swallows the pointer events meant for the rows, so
    # while it is empty the play and download buttons stop answering.
    busy_overlay = ft.Container(
        content=ft.Column(
            [busy_spinner, busy_label, empty_text],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12,
        ),
        alignment=ft.Alignment(0, 0),
        expand=True,
        visible=False,
    )
    progress_bar = ft.ProgressBar(visible=False, value=0, bar_height=6, border_radius=3)
    status_text = ft.Text(t("status_start"), size=13, selectable=True)
    # A playlist that came out short has to say so: this is the line that names
    # the tracks left out and the reason, and it stays after the run ends.
    detail_text = ft.Text(
        size=12, color=ft.Colors.AMBER_300, selectable=True, visible=False
    )
    skipped: list[Progress] = []  # tracks this run could not fetch
    present: list[Progress] = []  # tracks it left alone: already in the folder
    # Tracks it kept, with something worth saying about them: silence inside
    # the audio, or a length nothing here could measure. Not a complaint and
    # not a miss - the file is in the folder - so it never counts as a skip.
    noted: list[Progress] = []

    row_buttons: list[ft.IconButton] = []

    # ------------------------------------------------------------------ updates

    version_label = ft.Text(
        f"{config.APP_NAME} {version.current()}",
        size=11,
        color=ft.Colors.GREY_500,
        selectable=True,
    )
    update_text = ft.Text(size=11, color=ft.Colors.GREY_500, visible=False, max_lines=3)
    update_button = ft.TextButton(
        t("update_check"),
        icon=ft.Icons.REFRESH,
        on_click=lambda event: press_update(event),
    )
    # Tracks an earlier run could not fetch: the button appears when there are
    # any, and finishing them costs one lookup each - what is on disk stays.
    retry_button = ft.TextButton(
        "",
        icon=ft.Icons.REPLAY,
        visible=False,
        on_click=lambda _: run_task(retry_pending),
    )
    # A phone installs nothing from inside the app: a newer release is offered
    # as its page, which the browser downloads the APK from - and the browser
    # is the one place that is already signed in to GitHub, which a private
    # repository needs.
    url_launcher = ft.UrlLauncher()
    if not desktop:
        page.services.append(url_launcher)
    release_page: dict[str, str] = {}  # release page waiting for the button
    # True while a search or a download runs: an update never restarts the app
    # out from under one.
    working = False

    # ---------------------------------------------------------------- rendering

    def render_badge(label: str, color: str) -> None:
        badge_label.value = label
        badge_label.color = color
        status_badge.bgcolor = ft.Colors.with_opacity(0.15, color)

    def render_status(message: str, color: str = ft.Colors.GREY_300) -> None:
        status_text.value = message
        status_text.color = color

    def render_folder() -> None:
        folder_text.value = (
            str(state.download_root) if state.download_root else t("folder_none")
        )
        folder_text.color = ft.Colors.GREY_400 if state.download_root else ft.Colors.AMBER_300

    def render_update(message: str, color: str = ft.Colors.GREY_500) -> None:
        """The footer line: what the updater is doing, or nothing at all."""
        update_text.value = message
        update_text.color = color
        update_text.visible = bool(message)

    def render_update_action(download: bool, page_url: str = "") -> None:
        """Make the footer button the next thing it can do."""
        release_page["page"] = page_url
        update_button.content = t("update_download") if download else t("update_check")
        update_button.icon = ft.Icons.OPEN_IN_NEW if download else ft.Icons.REFRESH

    def set_busy(busy: bool) -> None:
        nonlocal working
        working = busy
        query_field.disabled = busy
        search_btn.disabled = busy
        format_dropdown.disabled = busy
        retry_button.disabled = busy
        for button in row_buttons:
            button.disabled = busy

    def refresh_query_state() -> None:
        """The X, and whether Buscar can run: only with text, never while busy."""
        refresh_query_widgets(query_field, clear_btn, search_btn, working)
        safe_update()

    def clear_query() -> None:
        query_field.value = ""
        stop_preview()
        # Clearing triggers on_change → refresh_query_state on its own; the
        # direct call below is the backstop for programmatic clears.
        refresh_query_state()
        try:
            # Focus lives on the control (`TextField.focus` is a coroutine);
            # `Page` has no `focus`, so reaching for it crashed the app here.
            run_task(query_field.focus)
        except RuntimeError:
            pass  # window closed mid-click: nothing left to focus

    def sync_overlay() -> None:
        """Show the overlay only while it holds something.

        It fills the list area and sits above the rows: left in the layout
        while it is empty it takes every pointer event the play and download
        buttons were waiting for, and both stop answering.
        """
        busy_overlay.visible = (
            busy_spinner.visible or busy_label.visible or empty_text.visible
        )

    def show_busy(message: str) -> None:
        """Spinner in the middle of the list area, with what is happening."""
        results_list.controls.clear()
        empty_text.visible = False
        busy_label.value = message
        busy_spinner.visible = True
        busy_label.visible = True
        sync_overlay()
        safe_update()

    def hide_busy() -> None:
        busy_spinner.visible = False
        busy_label.visible = False
        sync_overlay()

    def safe_update() -> None:
        """Push UI changes from background tasks; the session dies if the window closes."""
        try:
            page.update()
        except RuntimeError:
            pass  # window closed mid-download: nothing left to update

    def subtitle_for(result: SearchResult) -> str:
        if result.kind == "album":
            # A linked Spotify playlist is read from Spotify, not searched for
            # here: say so, because its audio is still matched on YouTube.
            label = "subtitle_spotify" if result.source == engine.SPOTIFY else "subtitle_album"
            parts = [t(label)]
            if result.track_count:
                parts.append(t.plural("subtitle_tracks", result.track_count))
            # Spotify owns its own editorial playlists: "Spotify · Spotify" says
            # nothing, so the owner is only shown when it is someone else.
            if result.artist and result.artist != t(label):
                parts.append(result.artist)
            return "  ·  ".join(parts)
        parts = [part for part in (result.artist, result.album) if part]
        if result.duration:
            parts.append(format_duration(result.duration))
        return "  ·  ".join(parts) or t("subtitle_track")

    def render_results(results: list[SearchResult]) -> None:
        results_list.controls.clear()
        row_buttons.clear()
        stop_preview()
        # The rows just cleared are gone from the page: drop their widgets with
        # them, or the next `stop_preview` reaches for one that is off the page.
        play_buttons.clear()
        preview_spinners.clear()
        hide_busy()
        empty_text.visible = not results
        sync_overlay()
        for result in results:
            actions: list[ft.Control] = []
            if result.kind == "track" and has_audio:
                # Every track plays the song that was asked for: the first 30s
                # of its own audio, fetched when play is pressed so the search
                # never waits for it. The spinner covers the fetch; the square
                # stops it.
                play_btn = ft.IconButton(
                    icon=ft.Icons.PLAY_ARROW,
                    tooltip=t("tooltip_preview"),
                    on_click=lambda _, item=result: toggle_preview(item),
                )
                # The spinner rides on top of the arrow: same cell, so showing
                # it never moves the row, and it paints even before any update
                # reaches the client because it is in the layout from the start.
                spinner = ft.ProgressRing(width=20, height=20, stroke_width=2, visible=False)
                stack = ft.Stack([play_btn, spinner], width=40, height=40, alignment=ft.Alignment(0, 0))
                play_buttons[result.url] = play_btn
                preview_spinners[result.url] = spinner
                actions.append(stack)
            button = ft.IconButton(
                icon=ft.Icons.DOWNLOAD,
                tooltip=t("tooltip_download_album" if result.kind == "album" else "tooltip_download_track"),
                on_click=lambda _, item=result: start_download(item),
            )
            row_buttons.append(button)
            actions.append(button)
            results_list.controls.append(
                ft.ListTile(
                    leading=ft.Icon(
                        ft.Icons.ALBUM if result.kind == "album" else ft.Icons.MUSIC_NOTE,
                        color=ft.Colors.BLUE_300 if result.kind == "album" else ft.Colors.GREEN_300,
                    ),
                    title=ft.Text(result.title, size=14),
                    subtitle=ft.Text(subtitle_for(result), size=12, color=ft.Colors.GREY_400),
                    trailing=ft.Row(actions, spacing=0, tight=True),
                )
            )
        render_status(
            t.plural("status_results", len(results)) if results else t("status_empty"),
            ft.Colors.GREY_300,
        )

    def render_progress(progress: Progress, target_format: str, album_title: str | None) -> None:
        parts: list[str] = []
        if album_title:
            parts.append(album_title)
            if progress.track_index and progress.track_count:
                parts.append(f"{progress.track_index}/{progress.track_count}")
        if progress.title:
            parts.append(progress.title)

        if progress.stage == "skipped":
            if progress.note == engine.ALREADY_THERE:
                # Not a failure and not a complaint: the file is where the
                # download would have put it, which is what a retry is for. It
                # is counted, and said once, in the run's summary.
                present.append(progress)
                return
            # Which tracks were left out, and why. Kept after the download ends:
            # "saved 21 of 40" is only half an answer without this.
            skipped.append(progress)
            detail_text.value = t(
                "status_skipped",
                count=len(skipped),
                title=progress.title or "?",
                reason=progress.note,
            )
            detail_text.visible = True
            return

        if progress.stage == "attention":
            # Nothing was refused and nothing is missing: a track that was kept
            # has something about it worth saying - silence where a player
            # would go quiet, or a length nothing here could measure. Said the
            # same way as what was left out, and kept after the run ends,
            # because it is about a file that is still in the folder.
            noted.append(progress)
            detail_text.value = t(
                "status_noted",
                count=len(noted),
                title=progress.title or "?",
                note=progress.note,
            )
            detail_text.visible = True
            return

        if progress.stage == "matching":
            # A Spotify playlist arrives as titles: each one is looked up in
            # YouTube Music before anything is downloaded.
            progress_bar.value = None
            parts.append(t("status_matching"))
            render_status(" · ".join(parts), ft.Colors.BLUE_200)
            return

        if progress.stage == "converting":
            progress_bar.value = None  # indeterminate while ffmpeg runs
            parts.append(t("status_extracting", format=target_format.upper()))
            render_status(" · ".join(parts), ft.Colors.BLUE_200)
            return

        if progress.stage == "tagging":
            # The audio is on disk; the tags are being looked up in a database.
            progress_bar.value = None
            parts.append(t("status_tagging"))
            render_status(" · ".join(parts), ft.Colors.BLUE_200)
            return

        progress_bar.value = (progress.percent or 0) / 100
        if progress.percent is None:
            parts.append(t("status_downloading", size=format_bytes(progress.downloaded_bytes)))
        else:
            parts.append(
                t(
                    "status_progress",
                    percent=f"{progress.percent:.0f}",
                    done=format_bytes(progress.downloaded_bytes),
                    total=format_bytes(progress.total_bytes),
                    speed=format_bytes(progress.speed),
                    eta=format_duration(progress.eta),
                )
            )
        render_status(" · ".join(parts), ft.Colors.BLUE_200)

    def album_folder(tracks: list[Track], album: AlbumInfo) -> Path:
        """The folder a run wrote to, whether or not it had anything to write.

        An empty list is a run that found everything already there, and the
        folder is still the album's - not the download root, which is where a
        single track without an album lands.
        """
        if tracks:
            return tracks[0].path.parent
        return (state.download_root or Path()) / album.folder

    def render_success(tracks: list[Track], album: AlbumInfo | None) -> None:
        progress_bar.value = 1
        render_badge(t("badge_done"), ft.Colors.GREEN_400)
        if album is not None:
            destination = album_folder(tracks, album)
            if not tracks and present:
                # A retry of a playlist that was already complete: nothing was
                # missing, and "0 of 40 saved" would say the opposite.
                render_status(
                    t("status_all_present", total=album.track_count, folder=destination),
                    ft.Colors.GREEN_300,
                )
                return
            key = "status_saved_album_present" if present else "status_saved_album"
            render_status(
                t(
                    key,
                    count=len(tracks),
                    total=album.track_count,
                    folder=destination,
                    present=len(present),
                ),
                ft.Colors.GREEN_300,
            )
            return
        if not tracks:
            # A run with nothing to fetch: the track is already where the
            # download would put it (a Spotify link of one track, fetched
            # before). Reaching for `tracks[0]` here raised IndexError inside
            # the event drain, which killed the run before it was accounted
            # for (`remember`) and left the pending list untouched.
            render_status(t("status_present_track"), ft.Colors.GREEN_300)
            return
        track = tracks[0]
        render_status(
            t(
                "status_saved_track",
                title=track.title,
                format=track.format.upper(),
                path=track.path,
            ),
            ft.Colors.GREEN_300,
        )

    # Which result row is previewing, and its widgets: one sample at a time, so
    # starting one stops the other and the old row goes back to play.
    previewing: dict[str, str] = {}
    # Bumped every time the preview is stopped or switched. A fetch or a
    # countdown carries the number it started under, and one that no longer
    # matches is a preview the user has already left: it must not play over
    # what replaced it, and it must not write the status line either.
    preview_generation = 0
    play_buttons: dict[str, ft.IconButton] = {}
    preview_spinners: dict[str, ft.ProgressRing] = {}

    def row_state(url: str, state: str) -> None:
        """One row's play cell into `state`; never pushes, so it is safe on a
        row that a new search has already taken off the page."""
        apply_preview_state(
            play_buttons.get(url), preview_spinners.get(url), state
        )

    def stop_preview() -> None:
        """Silence the shared player, reset every row icon to play, and retire
        every fetch and countdown still in flight."""
        nonlocal preview_generation
        preview_generation += 1
        if previewing:
            previewing.clear()
            if preview_player is not None:
                run_task(_release_preview)
        for url in play_buttons:
            row_state(url, PREVIEW_IDLE)

    async def _release_preview() -> None:
        try:
            await preview_player.pause()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - leaving audio behind is worse
            pass

    def toggle_preview(result: SearchResult) -> None:
        if not has_audio or result.kind != "track":
            return
        if previewing.get("url") == result.url:
            stop_preview()
            render_status(t("status_preview_stopped"), ft.Colors.GREY_300)
            safe_update()
            return
        # The 30s of the song itself, fetched on press so the search never
        # waits for it. A database sample is instant when one matched; the
        # video's own audio is cut to 30s otherwise.
        stop_preview()
        token = preview_generation
        row_state(result.url, PREVIEW_LOADING)
        render_status(t("status_preview_loading", title=result.title), ft.Colors.BLUE_200)
        safe_update()
        run_task(_play_sample, result, token)

    async def _play_sample(result: SearchResult, token: int) -> None:
        try:
            # Always the track that was asked for, never a database's stand-in
            # for it: that URL is a different recording of the same song, and
            # it arrives in whatever container the database serves (MP3, AAC),
            # which the local player may not be able to decode at all.
            path = await asyncio.to_thread(engine.preview_sample, result.url)
            url = path.as_uri()
        except Exception as err:  # noqa: BLE001 - a sample must never break search
            if token != preview_generation:
                return  # stopped or switched while fetching: not this row's turn
            row_state(result.url, PREVIEW_IDLE)
            message = str(err)
            if wait := engine.retry_after(message):
                # A refusal, not a verdict: count the wait down on the
                # status line, then say it plainly like a search does.
                run_task(countdown_preview, result, token, wait)
            else:
                render_status(t("status_preview_failed", message=message), ft.Colors.AMBER_300)
                safe_update()
            return
        if token != preview_generation:
            # Stopped or switched while fetching: never play over the new one.
            return
        stop_preview()
        previewing["url"] = result.url
        row_state(result.url, PREVIEW_PLAYING)
        render_status(t("status_preview_playing", title=result.title), ft.Colors.BLUE_200)
        safe_update()
        await _play_preview(url)

    async def countdown_preview(result: SearchResult, token: int, wait: int) -> None:
        """A refused sample says when to come back: count it down, then retry.

        The count is dropped the moment the row is stopped or another one is
        started: replaying it afterwards silenced what the user was listening
        to and started a sample they had already put down.
        """
        for remaining in range(wait, 0, -1):
            if token != preview_generation:
                return
            render_status(t("status_rate_limited", seconds=remaining), ft.Colors.AMBER_300)
            safe_update()
            await asyncio.sleep(1)
        if token != preview_generation:
            return
        render_status(t("status_preview_loading", title=result.title), ft.Colors.BLUE_200)
        safe_update()
        run_task(_play_sample, result, token)

    async def _play_preview(url: str) -> None:
        nonlocal preview_player
        try:
            if preview_player is None:
                # Born with the sample already set: see where it is declared.
                preview_player = fta.Audio(src=url)  # type: ignore[union-attr]
                page.services.append(preview_player)
                # Let the client mount it before it is asked to play anything.
                safe_update()
            else:
                preview_player.src = url
                # Push the new source to the client *before* asking it to
                # play: without this the player resumes the previous source
                # instead of the sample just fetched.
                preview_player.update()
            # No position: `play()` defaults to 0, and the plugin turns that
            # into a `seek(0)` that waits for a "seek complete" the player
            # never sends before it has a source - the call then sits there
            # until it times out and the preview never starts.
            await preview_player.play(position=None)
        except Exception as err:  # noqa: BLE001 - a sample must never break search
            stop_preview()
            render_status(t("status_preview_failed", message=str(err)), ft.Colors.AMBER_300)
            safe_update()

    def render_error(message: str) -> None:
        progress_bar.value = 0
        hide_busy()
        empty_text.visible = False
        sync_overlay()
        render_badge(t("badge_failed"), ft.Colors.RED_400)
        if wait := engine.retry_after(message):
            run_task(countdown_retry, wait)
        else:
            render_status(t("status_error", message=message), ft.Colors.RED_300)

    async def countdown_retry(wait: int) -> None:
        """A refusal says when to come back: count it down, then say it plainly."""
        for remaining in range(wait, 0, -1):
            render_status(t("status_rate_limited", seconds=remaining), ft.Colors.AMBER_300)
            safe_update()
            await asyncio.sleep(1)
        render_status(t("status_rate_limited_now"), ft.Colors.AMBER_300)
        safe_update()

    # ------------------------------------------------------------------- dialogs

    def show_first_run_dialog() -> None:
        default_dir = config.suggested_music_dir()

        def use_default(_) -> None:
            page.pop_dialog()
            apply_folder(default_dir)

        def pick(_) -> None:
            run_task(choose_folder, True)

        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(t("first_run_title")),
                content=ft.Column(
                    [
                        ft.Text(t("first_run_body"), size=13),
                        ft.Text(
                            t("first_run_suggested", path=default_dir),
                            size=12,
                            color=ft.Colors.GREY_400,
                        ),
                    ],
                    tight=True,
                    spacing=8,
                    width=420,
                ),
                actions=[
                    ft.TextButton(t("first_run_use_suggested"), on_click=use_default),
                    ft.FilledButton(t("first_run_choose"), on_click=pick),
                ],
            )
        )

    async def choose_folder(first_run: bool = False) -> None:
        try:
            chosen = await file_picker.get_directory_path(
                dialog_title=t("picker_title"),
                initial_directory=str(
                    state.download_root or config.suggested_music_dir()
                ),
            )
        except Exception:  # noqa: BLE001 - a platform without a directory picker
            chosen = None
        if not chosen:
            if first_run and state.download_root is None:
                show_first_run_dialog()
            return
        if first_run:
            page.pop_dialog()
        apply_folder(Path(chosen))

    async def use_system_folder() -> None:
        """First run on a phone: download where the system lets the app write.

        Mobile systems hand an app one directory and require a permission the
        user grants in settings for anything else, and there is no free-form
        directory picker to ask with - so the app takes the directory it is
        given and shows it in the header. "Change folder" can still move it
        somewhere the user picked.
        """
        try:
            if is_android():
                root = await storage_paths.get_external_storage_directory()
            else:
                root = await storage_paths.get_application_documents_directory()
        except Exception:  # noqa: BLE001 - the service is best effort
            root = None
        if not root:
            render_error(t("status_need_folder"))
            return
        apply_folder(Path(root))

    def apply_folder(root: Path) -> None:
        if not root.is_dir() or not os.access(root, os.W_OK):
            # Android hands out content:// trees and read-only paths that look
            # like directories; a download there would fail halfway through.
            render_status(t("status_folder_unusable", path=root), ft.Colors.AMBER_300)
            return
        state.download_root = root
        state.save()
        render_folder()
        page.update()

    def ensure_folder() -> None:
        """Ask for a download folder when there is none yet."""
        render_status(t("status_need_folder"), ft.Colors.AMBER_300)
        if desktop:
            show_first_run_dialog()
        else:
            run_task(use_system_folder)

    def remember_format() -> None:
        state.format = format_dropdown.value or engine.DEFAULT_FORMAT
        state.save()

    def show_album_dialog(album: AlbumInfo) -> None:
        target = state.download_root / album.folder if state.download_root else Path(album.folder)
        source = t("subtitle_spotify") if album.source == engine.SPOTIFY else None
        # "Spotify · Spotify · 50 tracks" says nothing: Spotify owns its own
        # editorial playlists, so the owner is only shown when it is someone else.
        artist = album.artist if album.artist and album.artist != source else None
        details = [
            value for value in (source, artist, album.year and str(album.year)) if value
        ]
        details.append(t.plural("subtitle_tracks", album.track_count))
        if album.total_duration:
            details.append(format_duration(album.total_duration))

        preview = album.track_titles[:12]
        track_lines = [
            ft.Text(f"{index:02d}.  {title}", size=12, color=ft.Colors.GREY_300)
            for index, title in enumerate(preview, start=1)
        ]
        if album.track_count > len(preview):
            track_lines.append(
                ft.Text(
                    t("album_more", count=album.track_count - len(preview)),
                    size=12,
                    color=ft.Colors.GREY_500,
                )
            )

        if album.truncated:
            # Spotify's own list stops at 100 tracks: what was read is what the
            # download has to work with.
            track_lines.append(
                ft.Text(t("album_truncated"), size=12, color=ft.Colors.AMBER_300)
            )

        def confirm(_) -> None:
            page.pop_dialog()
            run_task(run_download, album.url, album)

        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(album.title),
                content=ft.Column(
                    [
                        ft.Text("  ·  ".join(details), size=12, color=ft.Colors.BLUE_200),
                        ft.Text(
                            t("album_folder", path=target), size=12, color=ft.Colors.GREY_400
                        ),
                        ft.Divider(),
                        *track_lines,
                    ],
                    tight=True,
                    spacing=6,
                    width=520,
                ),
                actions=[
                    ft.TextButton(t("cancel"), on_click=lambda _: page.pop_dialog()),
                    ft.FilledButton(
                        t("download_album"), icon=ft.Icons.DOWNLOAD, on_click=confirm
                    ),
                ],
            )
        )

    # ------------------------------------------------------------------- workers

    async def run_search(query: str) -> None:
        set_busy(True)
        render_badge(t("badge_searching"), ft.Colors.BLUE_300)
        render_status(t("status_searching", query=query), ft.Colors.BLUE_200)
        show_busy(t("status_searching", query=query))
        try:
            results = await asyncio.to_thread(engine.search, query)
        except Exception as err:  # noqa: BLE001 - surface any engine failure
            logs.failure("search", err)
            render_error(str(err))
        else:
            render_results(results)
            render_badge(t("badge_results"), ft.Colors.BLUE_300)
        finally:
            set_busy(False)
            hide_busy()
            refresh_query_state()

    async def run_probe_then_confirm(result: SearchResult) -> None:
        set_busy(True)
        render_badge(t("badge_reading"), ft.Colors.BLUE_300)
        render_status(t("status_reading_album", title=result.title), ft.Colors.BLUE_200)
        show_busy(t("status_reading_album", title=result.title))
        try:
            album = await asyncio.to_thread(engine.probe_album, result.url)
        except Exception as err:  # noqa: BLE001
            logs.failure("album", err)
            render_error(str(err))
        else:
            hide_busy()
            render_badge(t("badge_confirm"), ft.Colors.BLUE_300)
            render_status(t("status_confirm"), ft.Colors.GREY_300)
            show_album_dialog(album)
        finally:
            set_busy(False)
            refresh_query_state()


    async def run_download(
        target: str,
        album: AlbumInfo | None,
        root: Path | None = None,
        target_format: str | None = None,
    ) -> None:
        """Download one link.

        A retry passes the folder and the format that link was asked for with,
        because the ones in the footer belong to what is being asked for now.
        """
        target_format = target_format or format_dropdown.value or engine.DEFAULT_FORMAT
        destination = root or state.download_root
        if destination is None:
            render_error(t("status_need_folder"))
            return

        render_badge(t("badge_downloading"), ft.Colors.BLUE_300)
        progress_bar.visible = True
        skipped.clear()
        present.clear()
        noted.clear()
        detail_text.visible = False
        set_busy(True)
        safe_update()

        events: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        worker = asyncio.create_task(
            asyncio.to_thread(
                engine.download_worker,
                target,
                target_format,
                destination,
                album,
                lambda tracks: events.put(("done", tracks)),
                lambda message: events.put(("error", message)),
                lambda progress: events.put(("progress", progress)),
            )
        )
        reported = False
        try:
            while True:
                # Worker threads must not touch controls: every update happens
                # here, on the event loop, driven by the event queue.
                if drain_events(events, target_format, album):
                    reported = True
                    safe_update()
                if worker.done() and events.empty():
                    break
                await asyncio.sleep(POLL_INTERVAL)
            error = worker.exception()
            if error is not None:
                logs.failure("download task", error)
                render_error(str(error))
        finally:
            set_busy(False)
            progress_bar.visible = False
            safe_update()
        remember(target, destination, target_format, reported)

    def drain_events(
        events: queue.SimpleQueue[tuple[str, object]],
        target_format: str,
        album: AlbumInfo | None,
    ) -> bool:
        updated = False
        album_title = album.title if album else None
        while not events.empty():
            kind, payload = events.get()
            if kind == "progress":
                render_progress(payload, target_format, album_title)
            elif kind == "done":
                render_success(payload, album)
            else:
                render_error(str(payload))
            updated = True
        return updated

    def remember(
        target: str, destination: Path, target_format: str, reported: bool
    ) -> None:
        """Write down what this run did not fetch, so a later one can finish it.

        Everything reported as left out is kept, except the tracks that are
        already on disk - those are not failures. A run that got as far as
        reporting anything also settles the whole playlist for that folder and
        format: what it did not complain about is in, so nothing from it stays
        on the list. A run that failed before reading the link (a playlist
        Spotify would not serve, a machine with no network) settles nothing.
        """
        failures = [
            pending.Item(
                url=target,
                root=str(destination),
                format=target_format,
                index=event.track_index,
                title=event.title or "?",
                reason=event.note,
            )
            for event in skipped
            if event.track_index is not None and event.note != engine.ALREADY_THERE
        ]
        items = pending.load()
        if reported:
            items = pending.without(items, (target, str(destination), target_format))
        pending.save(pending.merged(items, failures))
        render_pending()

    def render_pending() -> None:
        """The footer's retry button, or nothing when there is nothing to retry."""
        count = len(pending.load())
        retry_button.content = t.plural("retry_pending", count)
        retry_button.visible = count > 0
        safe_update()

    async def retry_pending(auto: bool = False) -> None:
        """Finish the tracks earlier runs left behind, one link at a time.

        Each entry carries the folder and the format it was asked for with, and
        `run_download` leaves what is already on disk alone - so a retry costs
        one lookup per missing track and nothing else.
        """
        if working:
            return
        items = pending.due(pending.load()) if auto else pending.load()
        if not items:
            render_pending()
            return
        if state.download_root is None:
            ensure_folder()
            return
        for url, root, target_format in dict.fromkeys(item.place for item in items):
            try:
                album = await asyncio.to_thread(engine.probe_album, url)
            except Exception as err:  # noqa: BLE001 - the link itself did not read
                render_error(str(err))
                return  # nothing was tried: the list stays exactly as it was
            await run_download(url, album, root=Path(root), target_format=target_format)
        render_pending()

    # ------------------------------------------------------------------- updates

    async def check_for_update() -> None:
        """Look for a newer release because the button was pressed, and act on it.

        Nothing here runs by itself: the app checks when asked, and updates only
        then. It does refuse to restart out from under a download in progress -
        it waits for it and says so - because that is the one way this ends in
        lost work.
        """
        if not update.installable() and not is_mobile():
            render_update(t("update_local", version=version.current()), ft.Colors.GREY_500)
            safe_update()
            return

        render_update(t("update_checking"), ft.Colors.GREY_300)
        safe_update()
        token = state.update_token or update.env_token()
        try:
            found = await asyncio.to_thread(update.check, token)
        except Exception as err:  # noqa: BLE001 - a feed answering with nonsense
            found = update.Result(error=str(err))

        if found.release is None:
            if found.needs_token:
                render_update(t("update_private"), ft.Colors.AMBER_300)
            elif found.error:
                render_update(t("update_error", message=found.error), ft.Colors.AMBER_300)
            else:
                render_update(t("update_current", version=version.current()))
            safe_update()
            return

        release = found.release
        if not update.installable():
            render_update(
                t("update_available_mobile", version=release.version), ft.Colors.BLUE_200
            )
            # The APK built for this phone's ABI, or the release page when the
            # release has none for it.
            render_update_action(True, update.download_url(release))
            safe_update()
            return

        while working:
            await asyncio.sleep(1)

        render_update(t("update_found", version=release.version), ft.Colors.BLUE_200)
        safe_update()
        # The download runs in a thread and reports through the queue: nothing
        # but this coroutine touches a control.
        events: queue.SimpleQueue[tuple[int, int]] = queue.SimpleQueue()
        worker = asyncio.create_task(
            asyncio.to_thread(
                update.apply,
                release,
                token,
                lambda written, expected: events.put((written, expected)),
            )
        )
        try:
            while True:
                while not events.empty():
                    written, expected = events.get()
                    render_update(
                        t(
                            "update_downloading",
                            version=release.version,
                            done=format_bytes(written),
                            total=format_bytes(expected),
                        ),
                        ft.Colors.BLUE_200,
                    )
                    safe_update()
                if worker.done():
                    break
                await asyncio.sleep(POLL_INTERVAL)
            executable = worker.result()
        except Exception as err:  # noqa: BLE001 - report, keep running the old build
            render_update(t("update_failed", message=str(err)), ft.Colors.RED_300)
            safe_update()
            return

        try:
            update.relaunch(executable, release.version)
        except OSError as err:
            # The new build is on disk, the old one is in memory: all that is
            # left to do is to ask for a restart.
            render_update(
                t("update_restart_manual", version=release.version, message=err),
                ft.Colors.AMBER_300,
            )
            safe_update()
            return
        render_update(t("update_restarting", version=release.version), ft.Colors.GREEN_300)
        safe_update()
        try:
            await page.window.close()  # the instance just started takes it from here
        except Exception:  # noqa: BLE001 - an already-closed window, or a phone
            pass

    async def open_release_page(url: str) -> None:
        """Hand a release page to the browser: how a phone updates itself."""
        try:
            await url_launcher.launch_url(url)
        except Exception as err:  # noqa: BLE001 - a platform with no browser
            render_update(t("update_failed", message=str(err)), ft.Colors.RED_300)
            safe_update()

    def press_update(_) -> None:
        """The one entry point: the button, and what it does next."""
        if page_url := release_page.get("page"):
            run_task(open_release_page, page_url)
        else:
            run_task(check_for_update)

    # ------------------------------------------------------------------ handlers

    def start_search() -> None:
        query = (query_field.value or "").strip()
        if not query:
            render_badge(t("badge_input"), ft.Colors.AMBER_400)
            render_status(t("status_need_input"), ft.Colors.AMBER_300)
            return
        if state.download_root is None:
            ensure_folder()
            return
        stop_preview()
        run_task(run_search, query)

    def start_download(result: SearchResult) -> None:
        state.format = format_dropdown.value or engine.DEFAULT_FORMAT
        state.save()
        stop_preview()
        if result.kind == "album":
            run_task(run_probe_then_confirm, result)
        else:
            run_task(run_download, result.url, None)

    async def reveal_window() -> None:
        """Show the window, at the size set above, once the client is up.

        `FLET_APP_HIDDEN` starts the window hidden, and the client syncs its
        own state back as soon as it connects - hidden, at the client's default
        size - overwriting every property set above. Asking for the window only
        after the client is ready is what makes it appear, and it appears at
        the geometry it was given, without a visible resize. A page in a
        browser tab, or a phone, has no window of its own to reveal.
        """
        if not desktop:
            return
        try:
            await page.window.wait_until_ready_to_show()
        except Exception:  # noqa: BLE001 - reveal it anyway: a window that never
            pass  # comes up is the one failure the user cannot report
        page.window.visible = True
        try:
            page.update()
        except RuntimeError:
            pass  # window closed before it was shown: nothing left to show

    # ---------------------------------------------------------------------- page

    render_folder()
    folder_row: list[ft.Control] = [
        ft.Icon(ft.Icons.FOLDER_OPEN, size=16, color=ft.Colors.GREY_400),
        folder_text,
        ft.TextButton(t("change_folder"), on_click=lambda _: run_task(choose_folder)),
    ]
    if desktop:
        # One line, the desktop way: the folder on the left, the format on the right.
        folder_row += [ft.Container(expand=True), format_dropdown]
        settings_rows = [ft.Row(folder_row, vertical_alignment=ft.CrossAxisAlignment.CENTER)]
    else:
        # A phone has no room for both, so the format gets its own line and
        # stretches across it, and the path takes the room the button leaves.
        format_dropdown.width = None
        settings_rows = [
            ft.Row(folder_row, vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([format_dropdown]),
        ]

    center_stack = ft.Stack(
        [
            results_list,
            busy_overlay,
        ],
        expand=True,
    )
    page.add(
        ft.Row(
            [
                ft.Icon(ft.Icons.GRAPHIC_EQ, size=28),
                ft.Text(config.APP_NAME, size=22, weight=ft.FontWeight.BOLD),
                ft.Container(expand=True),
                status_badge,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        *settings_rows,
        ft.Row([query_field, search_btn], spacing=12),
        ft.Divider(),
        center_stack,
        progress_bar,
        status_text,
        detail_text,
        ft.Row(
            [version_label, update_button, retry_button],
            wrap=True,
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        update_text,
    )
    refresh_query_state()

    if state.download_root is None:
        # Desktop asks where to put the music; a phone is told.
        if desktop:
            show_first_run_dialog()
        else:
            run_task(use_system_folder)

    if UPDATED_TO:
        render_update(t("updated_to", version=UPDATED_TO), ft.Colors.GREEN_300)

    render_pending()
    if pending.due(pending.load()) and state.download_root is not None:
        # Tracks an earlier run could not fetch: finishing them is what that
        # download was asked for, so nobody has to ask a second time.
        run_task(retry_pending, True)

    # Everything is laid out: show the window at its real size.
    run_task(reveal_window)


def start(page: ft.Page) -> None:
    """`ft.run` entry point: a startup failure must not hide the window.

    The window is started hidden and only revealed once everything is laid out
    (`main`), so anything that fails before that leaves a process nobody can
    see and nothing that says why - a config directory that cannot be written,
    a settings file holding something that is not settings. This catches it,
    shows the window, and says what happened.
    """
    logs.setup()
    try:
        main(page)
    except Exception as err:  # noqa: BLE001 - report it, never hide it
        _startup_failure(page, err)


def _startup_failure(page: ft.Page, err: Exception) -> None:
    """Show the window and say what stopped the app from starting."""
    logs.failure("startup", err)
    message = f"{err.__class__.__name__}: {err}"
    try:
        title = i18n.Translator()("startup_failed")
    except Exception:  # noqa: BLE001 - even the translation is not worth hiding for
        title = f"{config.APP_NAME} could not start"
    try:
        page.window.visible = True
    except Exception:  # noqa: BLE001 - a page with no window of its own (phone, browser)
        pass
    try:
        page.show_dialog(
            ft.AlertDialog(
                modal=True,
                title=ft.Text(title),
                content=ft.Text(message, selectable=True),
            )
        )
    except Exception:  # noqa: BLE001 - the dialog is a nicety, the window is not
        try:
            page.add(ft.Text(message, selectable=True))
        except Exception:  # noqa: BLE001
            pass
    try:
        page.update()
    except Exception:  # noqa: BLE001
        pass


def pin_client_flavor() -> None:
    """Ask for the Flet desktop client this app ships: the `full` one.

    `flet_desktop` resolves the flavor *at startup*: an environment variable,
    else `[tool.flet].desktop_flavor` in the `pyproject.toml` of the current
    directory, else `light` on Linux. So the same executable, launched from the
    project directory, got the full client and its audio plugin, while launched
    from anywhere else - a launcher, a file manager, another shell - it got the
    light one, which ships without `libaudioplayers_linux_plugin.so`: every call
    on the audio service then waits for an answer that never comes, and the 30s
    preview dies on a ten-second timeout. Pinned here, it is the same client
    wherever the app is run from.
    """
    os.environ["FLET_DESKTOP_FLAVOR"] = "full"


if __name__ == "__main__":
    pin_client_flavor()
    if "--selftest" in sys.argv:
        raise SystemExit(selftest(download="--no-download" not in sys.argv))
    if "--check-updates" in sys.argv:
        raise SystemExit(check_updates())
    if "--updated" in sys.argv:
        index = sys.argv.index("--updated")
        UPDATED_TO = sys.argv[index + 1] if index + 1 < len(sys.argv) else ""
    if update.installable():
        # Whatever the update that installed this build could not delete: on
        # Windows the displaced executable stays mapped until its process ends.
        update.cleanup()
    if sys.platform.startswith("linux"):
        # Gives the client window a stable app id, so a desktop entry (and thus
        # the app icon) can be matched by the shell.
        os.environ.setdefault("FLET_APP_ID", config.APP_ID)
    # The hidden-until-ready dance is for the desktop client; a bundle built by
    # `flet build` (Android) embeds the app and shows it itself.
    view = None if is_mobile() else ft.AppView.FLET_APP_HIDDEN
    ft.run(start, view=view, assets_dir=bundle.assets_dir())
