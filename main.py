"""Cadenza — search, confirm, then download music (Flet + yt-dlp + ffmpeg).

Run with `python main.py`, or `Cadenza --selftest` for a headless engine check.
"""

from __future__ import annotations

import asyncio
import os
import queue
import re
import sys
from pathlib import Path

import flet as ft
from flet.utils.platform_utils import is_android, is_mobile

import bundle
import engine
import i18n
import settings as config
from engine import AlbumInfo, Progress, SearchResult, Track

POLL_INTERVAL = 0.1
WINDOW_SIZE = (760, 640)
WINDOW_MIN_SIZE = (560, 460)


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


def selftest() -> int:
    """Headless check of the bundled engine; `--selftest` runs it.

    Verifies a frozen build end to end: ffmpeg, the JavaScript runtime, the
    yt-dlp extractors, the network path, and the tags and cover art written
    into the file. Downloads the default format, so a build that cannot embed
    art is caught here instead of in the user's download folder.
    """
    import tempfile

    lines: list[str] = []
    ok = True

    def report(name: str, value: str, passed: bool = True) -> None:
        nonlocal ok
        ok = ok and passed
        lines.append(f"{name:<12} {value}")

    ffmpeg = engine.find_ffmpeg()
    report("ffmpeg", ffmpeg or "NOT FOUND", ffmpeg is not None)
    report("js runtimes", ", ".join(engine.find_js_runtimes()))
    try:
        # Bundled through `--hidden-import`: yt-dlp needs it to put cover art
        # into FLAC files, and nothing imports it directly.
        import mutagen

        report("mutagen", mutagen.version_string)
    except ImportError as err:
        report("mutagen", f"MISSING ({err})", False)

    try:
        results = engine.search("Kevin MacLeod Sneaky Snitch", limit=2)
        report(
            "search",
            f"{len(results)} result(s): {results[0].title}" if results else "no results",
            bool(results),
        )
    except Exception as err:  # noqa: BLE001 - report, do not crash
        report("search", f"FAILED: {err}", False)
    else:
        try:
            with tempfile.TemporaryDirectory() as tmp:
                tracks = engine.download(results[0].url, engine.DEFAULT_FORMAT, Path(tmp))
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
                    # video thumbnail cropped to 4:3. What fails here is a
                    # build that embeds YouTube's 16:9 frame unmodified.
                    kind, width, height = art
                    ratio = width / height
                    report(
                        "cover art",
                        f"{kind} {width}x{height}",
                        abs(ratio - 4 / 3) < 0.01 or abs(ratio - 1) < 0.01,
                    )
        except Exception as err:  # noqa: BLE001
            report("download", f"FAILED: {err}", False)

    text = "\n".join(lines)
    if sys.stdout is None:  # windowed builds have no console
        Path("selftest.txt").write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0 if ok else 1


def main(page: ft.Page) -> None:
    t = i18n.Translator()

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
    format_dropdown = ft.Dropdown(
        label=t("format"),
        width=200,
        value=(
            state.format
            if state.format in formats
            else engine.DEFAULT_FORMAT if engine.DEFAULT_FORMAT in formats else None
        ),
        options=[ft.DropdownOption(key, t(f"format_{key}")) for key in formats],
        on_select=lambda _: remember_format(),
    )

    query_field = ft.TextField(
        label=t("query_label"),
        hint_text=t("query_hint"),
        expand=True,
        autofocus=True,
        on_submit=lambda _: start_search(),
    )
    search_btn = ft.FilledButton(
        content=t("search"),
        icon=ft.Icons.SEARCH,
        on_click=lambda _: start_search(),
    )

    results_list = ft.ListView(expand=True, spacing=2, padding=ft.Padding.only(top=8))
    progress_bar = ft.ProgressBar(visible=False, value=0, bar_height=6, border_radius=3)
    status_text = ft.Text(t("status_start"), size=13, selectable=True)

    row_buttons: list[ft.IconButton] = []

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

    def set_busy(busy: bool) -> None:
        query_field.disabled = busy
        search_btn.disabled = busy
        format_dropdown.disabled = busy
        for button in row_buttons:
            button.disabled = busy

    def safe_update() -> None:
        """Push UI changes from background tasks; the session dies if the window closes."""
        try:
            page.update()
        except RuntimeError:
            pass  # window closed mid-download: nothing left to update

    def subtitle_for(result: SearchResult) -> str:
        if result.kind == "album":
            parts = [t("subtitle_album")]
            if result.track_count:
                parts.append(t.plural("subtitle_tracks", result.track_count))
            if result.artist:
                parts.append(result.artist)
            return "  ·  ".join(parts)
        parts = [part for part in (result.artist, result.album) if part]
        if result.duration:
            parts.append(format_duration(result.duration))
        return "  ·  ".join(parts) or t("subtitle_track")

    def render_results(results: list[SearchResult]) -> None:
        results_list.controls.clear()
        row_buttons.clear()
        for result in results:
            button = ft.IconButton(
                icon=ft.Icons.DOWNLOAD,
                tooltip=t("tooltip_download_album" if result.kind == "album" else "tooltip_download_track"),
                on_click=lambda _, item=result: start_download(item),
            )
            row_buttons.append(button)
            results_list.controls.append(
                ft.ListTile(
                    leading=ft.Icon(
                        ft.Icons.ALBUM if result.kind == "album" else ft.Icons.MUSIC_NOTE,
                        color=ft.Colors.BLUE_300 if result.kind == "album" else ft.Colors.GREEN_300,
                    ),
                    title=ft.Text(result.title, size=14),
                    subtitle=ft.Text(subtitle_for(result), size=12, color=ft.Colors.GREY_400),
                    trailing=button,
                )
            )
        render_status(t.plural("status_results", len(results)), ft.Colors.GREY_300)

    def render_progress(progress: Progress, target_format: str, album_title: str | None) -> None:
        parts: list[str] = []
        if album_title:
            parts.append(album_title)
            if progress.track_index and progress.track_count:
                parts.append(f"{progress.track_index}/{progress.track_count}")
        if progress.title:
            parts.append(progress.title)

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

    def render_success(tracks: list[Track], album: AlbumInfo | None) -> None:
        progress_bar.value = 1
        render_badge(t("badge_done"), ft.Colors.GREEN_400)
        if album is not None:
            destination = tracks[0].path.parent if tracks else state.download_root
            render_status(
                t(
                    "status_saved_album",
                    count=len(tracks),
                    total=album.track_count,
                    folder=destination,
                ),
                ft.Colors.GREEN_300,
            )
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

    def render_error(message: str) -> None:
        progress_bar.value = 0
        render_badge(t("badge_failed"), ft.Colors.RED_400)
        render_status(t("status_error", message=message), ft.Colors.RED_300)

    # ------------------------------------------------------------------- dialogs

    def show_first_run_dialog() -> None:
        default_dir = config.suggested_music_dir()

        def use_default(_) -> None:
            page.pop_dialog()
            apply_folder(default_dir)

        def pick(_) -> None:
            page.run_task(choose_folder, True)

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
            page.run_task(use_system_folder)

    def remember_format() -> None:
        state.format = format_dropdown.value or engine.DEFAULT_FORMAT
        state.save()

    def show_album_dialog(album: AlbumInfo) -> None:
        target = state.download_root / album.folder if state.download_root else Path(album.folder)
        details = [value for value in (album.artist, album.year and str(album.year)) if value]
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

        def confirm(_) -> None:
            page.pop_dialog()
            page.run_task(run_download, album.url, album)

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
        safe_update()
        try:
            results = await asyncio.to_thread(engine.search, query)
        except Exception as err:  # noqa: BLE001 - surface any engine failure
            render_error(str(err))
        else:
            render_results(results)
            render_badge(t("badge_results"), ft.Colors.BLUE_300)
        finally:
            set_busy(False)
            safe_update()

    async def run_probe_then_confirm(result: SearchResult) -> None:
        set_busy(True)
        render_badge(t("badge_reading"), ft.Colors.BLUE_300)
        render_status(t("status_reading_album", title=result.title), ft.Colors.BLUE_200)
        safe_update()
        try:
            album = await asyncio.to_thread(engine.probe_album, result.url)
        except Exception as err:  # noqa: BLE001
            render_error(str(err))
        else:
            render_badge(t("badge_confirm"), ft.Colors.BLUE_300)
            render_status(t("status_confirm"), ft.Colors.GREY_300)
            show_album_dialog(album)
        finally:
            set_busy(False)
            safe_update()

    async def run_download(target: str, album: AlbumInfo | None) -> None:
        target_format = format_dropdown.value or engine.DEFAULT_FORMAT
        destination = state.download_root
        if destination is None:
            render_error(t("status_need_folder"))
            return

        render_badge(t("badge_downloading"), ft.Colors.BLUE_300)
        progress_bar.visible = True
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
        try:
            while True:
                # Worker threads must not touch controls: every update happens
                # here, on the event loop, driven by the event queue.
                if drain_events(events, target_format, album):
                    safe_update()
                if worker.done() and events.empty():
                    break
                await asyncio.sleep(POLL_INTERVAL)
            error = worker.exception()
            if error is not None:
                render_error(str(error))
        finally:
            set_busy(False)
            progress_bar.visible = False
            safe_update()

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
        page.run_task(run_search, query)

    def start_download(result: SearchResult) -> None:
        state.format = format_dropdown.value or engine.DEFAULT_FORMAT
        state.save()
        if result.kind == "album":
            page.run_task(run_probe_then_confirm, result)
        else:
            page.run_task(run_download, result.url, None)

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
        await page.window.wait_until_ready_to_show()
        page.window.visible = True
        page.update()

    # ---------------------------------------------------------------------- page

    render_folder()
    folder_row: list[ft.Control] = [
        ft.Icon(ft.Icons.FOLDER_OPEN, size=16, color=ft.Colors.GREY_400),
        folder_text,
        ft.TextButton(t("change_folder"), on_click=lambda _: page.run_task(choose_folder)),
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
        results_list,
        progress_bar,
        status_text,
    )

    if state.download_root is None:
        # Desktop asks where to put the music; a phone is told.
        if desktop:
            show_first_run_dialog()
        else:
            page.run_task(use_system_folder)

    # Everything is laid out: show the window at its real size.
    page.run_task(reveal_window)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    if sys.platform.startswith("linux"):
        # Gives the client window a stable app id, so a desktop entry (and thus
        # the app icon) can be matched by the shell.
        os.environ.setdefault("FLET_APP_ID", config.APP_ID)
    # The hidden-until-ready dance is for the desktop client; a bundle built by
    # `flet build` (Android) embeds the app and shows it itself.
    view = None if is_mobile() else ft.AppView.FLET_APP_HIDDEN
    ft.run(main, view=view, assets_dir=bundle.assets_dir())
