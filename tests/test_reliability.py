"""The failures that used to be silent, one test each.

A retry pass whose bound was a name nothing defined, tags written to a staging
name no writer knows, a track an album never fetched disappearing from the
count, an empty run the UI answered by reaching for a track that was not there,
a decode failure read as "plays nothing", and a startup that hid its own error.
Every one of them was reachable in the shipped code and none of them was
covered.
"""

from __future__ import annotations

import os

import pytest

import engine
import main
import metadata
import settings
import spotify


def _track(title: str = "Song", artist: str = "Artist", duration: float = 200.0) -> spotify.Track:
    return spotify.Track(title=title, artist=artist, duration=duration)


# --------------------------------------------- a refused playlist lookup retried


def test_refused_lookups_are_retried_then_left_alone(monkeypatch):
    """`MATCH_RETRY_GIVE_UP` was used and never defined: the retry pass raised
    NameError and the whole playlist died with it."""
    calls: list[str] = []

    def refuse(track):
        calls.append(track.title)
        raise RuntimeError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(engine, "_match_track", refuse)
    monkeypatch.setattr(engine.time, "sleep", lambda _seconds: None)

    tracks = [(index, _track(f"Song {index}")) for index in range(1, 6)]
    with pytest.raises(engine.DownloadError, match="refused the request"):
        engine._match_tracks(tracks, len(tracks), None)

    assert len(calls) == len(tracks) + engine.MATCH_RETRY_GIVE_UP


def test_a_refused_lookup_that_answers_later_keeps_its_url(monkeypatch):
    attempts: list[str] = []

    def flaky(track):
        attempts.append(track.title)
        if len(attempts) == 1:
            raise RuntimeError("HTTP Error 403: Forbidden")
        return "https://example.invalid/song"

    monkeypatch.setattr(engine, "_match_track", flaky)
    monkeypatch.setattr(engine.time, "sleep", lambda _seconds: None)

    urls, reasons = engine._match_tracks([(1, _track())], 1, None)

    assert urls == ["https://example.invalid/song"]
    assert reasons == [""]


# ------------------------------------------------ tags on a staging name (.part)


def test_tags_reach_a_staging_name_with_the_container_spelled_out(tmp_path, monkeypatch):
    """`Song.flac.part` has no writer: without the suffix the write is skipped,
    which is how the no-ffmpeg path (Android) published untagged tracks."""
    seen: dict = {}

    def fake(path, tags, cover):
        seen["path"] = path
        return True

    monkeypatch.setitem(metadata._WRITERS, ".flac", fake)
    staging = tmp_path / "Song.flac.part"
    staging.write_bytes(b"audio")

    assert metadata.write_tags(staging, {"title": "Song"}, None, suffix=".flac") is True
    assert seen["path"] == staging
    assert metadata.write_tags(staging, {"title": "Song"}, None) is False


# ------------------------------------------- a track an album never fetched


class _FakeYdl:
    """A yt-dlp that hands back one playlist, failures included: what a real
    one returns for an entry it was told to ignore the failure of."""

    info: dict = {}

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add_post_processor(self, *args, **kwargs):
        pass

    def extract_info(self, url, download=False):
        return self.info


def test_a_track_an_album_could_not_fetch_is_reported(tmp_path, monkeypatch):
    good = tmp_path / "01 - One.flac"
    good.write_bytes(b"audio")
    _FakeYdl.info = {
        "_type": "playlist",
        "playlist_count": 3,
        "entries": [
            {
                "playlist_index": 1,
                "title": "One",
                "duration": 200,
                "requested_downloads": [{"filepath": str(good)}],
            },
            None,  # the entry the download was told to ignore
            {"playlist_index": 3, "title": "Three", "duration": 200},  # no file
        ],
    }
    album = engine.AlbumInfo(
        title="Album", url="https://example.invalid/album", folder="Album"
    )
    monkeypatch.setattr(engine.yt_dlp, "YoutubeDL", _FakeYdl)
    monkeypatch.setattr(engine, "_playing", lambda path: engine._Playing(200.0))
    monkeypatch.setattr(engine, "_enrich", lambda *args: None)
    monkeypatch.setattr(engine, "_require_converter", lambda _format: None)

    events: list[engine.Progress] = []
    tracks = engine.download(
        "https://example.invalid/album", "flac", tmp_path, album, events.append
    )

    assert [track.title for track in tracks] == ["One"]
    assert [
        (event.track_index, event.note) for event in events if event.stage == "skipped"
    ] == [(3, engine.MISSING_AUDIO), (2, engine.MISSING_AUDIO)]


def test_a_short_entry_list_is_not_guessed_at():
    """Entries dropped before any of this leave no position to report, and a
    guess would name the wrong track."""
    info = {"_type": "playlist", "playlist_count": 5, "entries": [{"playlist_index": 1}]}
    assert engine._missing_entries(info, {1}) == []


def test_an_empty_run_is_a_contract_the_ui_has_to_answer(tmp_path, monkeypatch):
    """A Spotify link of one track, already in the folder, downloads nothing:
    the UI used to reach for `tracks[0]` and raise IndexError inside the event
    drain, which killed the run before it was accounted for."""
    url = "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    playlist = spotify.Playlist(
        name="Ya Fue", owner="WOS", tracks=(_track("Ya Fue", "WOS", 180.0),)
    )
    monkeypatch.setattr(engine, "_spotify_playlist", lambda _url: playlist)
    monkeypatch.setattr(engine, "_playing", lambda path: engine._Playing(180.0))
    monkeypatch.setattr(engine, "_require_converter", lambda _format: None)

    destination = engine._destination(tmp_path, 1, "Ya Fue", False, "flac")
    destination.write_bytes(b"audio")

    assert engine.download(url, "flac", tmp_path, None, None) == []


# ------------------------------------------- a decoder that will not read a file


def test_a_decoder_that_could_not_open_the_file_is_a_doubt():
    """A locked file, a machine out of memory or an ffmpeg build without the
    codec says nothing about the track: it used to be read as "plays nothing"
    and the file was deleted as incomplete."""
    assert engine._could_not_read("Song.flac: Permission denied")
    assert engine._could_not_read("[flac] Error opening input: Input/output error")
    assert engine._could_not_read(
        "[m4a] Decoder (codec aac) not found for input stream #0:0"
    )


def test_what_the_file_itself_says_is_still_a_verdict():
    """A file a decoder did open and found nothing in is refused, as before:
    the promise that nothing half-finished is kept stands."""
    assert not engine._could_not_read("Song.flac: Invalid data found when processing input")
    assert not engine._could_not_read("moov atom not found")


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="a file no one may read is readable by root",
)
def test_a_file_that_cannot_be_read_is_left_alone(tmp_path, ffmpeg):
    locked = tmp_path / "Song.flac"
    locked.write_bytes(b"audio, and nobody may look")
    locked.chmod(0o000)
    try:
        assert engine._playing(locked).seconds is None
    finally:
        locked.chmod(0o600)


def test_an_empty_file_plays_nothing(tmp_path, ffmpeg):
    empty = tmp_path / "empty.flac"
    empty.write_bytes(b"")

    assert engine._playing(empty).seconds == 0.0


# ------------------------------------------------------- settings and startup


def test_settings_that_are_not_settings_are_defaults(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")

    loaded = settings.Settings.load(path)

    assert loaded.download_root is None
    assert loaded.format is None


def test_a_settings_file_that_cannot_be_written_is_not_a_crash(tmp_path):
    """The save runs while the window is still being built: an OSError there
    used to leave the app with no window and no message."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("a file where the config directory should be", encoding="utf-8")
    loaded = settings.Settings(blocked / "config.json")
    loaded.download_root = tmp_path

    loaded.save()  # must not raise


class _FakeWindow:
    visible = False


class _FakePage:
    """As much of a `ft.Page` as the startup guard touches."""

    def __init__(self) -> None:
        self.window = _FakeWindow()
        self.dialogs: list = []
        self.updates = 0

    def show_dialog(self, dialog) -> None:
        self.dialogs.append(dialog)

    def update(self) -> None:
        self.updates += 1


def test_a_failure_while_the_window_is_hidden_is_shown(monkeypatch, config_home):
    def explode(page):
        raise PermissionError("the config directory is read-only")

    monkeypatch.setattr(main, "main", explode)
    page = _FakePage()

    main.start(page)

    assert page.window.visible is True
    assert page.dialogs, "the reason was not shown"
    assert page.updates == 1
    # The window said what the user needs; the log kept what a report needs.
    assert "startup failed" in (config_home / "cadenza.log").read_text(encoding="utf-8")


def test_a_startup_that_works_is_left_alone(monkeypatch, config_home):
    seen: list = []
    monkeypatch.setattr(main, "main", seen.append)
    page = _FakePage()

    main.start(page)

    assert seen == [page]
    assert page.dialogs == []
    assert page.updates == 0
    assert not (config_home / "cadenza.log").exists(), "nothing was logged"
