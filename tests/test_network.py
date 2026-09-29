"""The whole path with real downloads: three states of a file, and a refusal.

Marked `network` and deselected by default (see `pyproject.toml`) - run with
`pytest -m network` when a network and YouTube's patience are available. What
it proves is what no unit can: that the checks are wired into the runs that
produce and keep files, in that order.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import engine

pytestmark = pytest.mark.network

TRACK_URL = "https://open.spotify.com/track/23m5rUpKrW85DNikaGm7j8"  # 22s intro
YOUTUBE_URL = "https://www.youtube.com/watch?v=KBV_zpMm_0Q"


def run(url: str, folder: Path, events: list) -> list:
    folder.mkdir(parents=True, exist_ok=True)
    return engine.download(url, "flac", folder, None, events.append)


def staged(folder: Path) -> list[str]:
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*.part"))


def notes(events: list) -> list[str]:
    return [event.note for event in events if event.stage == "skipped"]


def test_a_file_moves_through_all_three_states(tmp_path):
    folder = tmp_path / "music"

    # 1. a fresh download: passes the check and leaves nothing half-written
    events: list = []
    tracks = run(TRACK_URL, folder, events)
    path = tracks[0].path
    assert path.is_file() and path.stat().st_size > 0
    assert notes(events) == []
    assert staged(folder) == []

    # 2. whole, it is left alone: no lookup, no download
    events = []
    run(TRACK_URL, folder, events)
    assert notes(events) == [engine.ALREADY_THERE]
    assert not any(event.stage == "downloading" for event in events)

    # 3. cut short, the check catches it and the same run fetches it back
    whole_size = path.stat().st_size
    data = path.read_bytes()
    path.write_bytes(data[: len(data) // 2])
    events = []
    tracks = run(TRACK_URL, folder, events)
    assert engine.ALREADY_THERE not in notes(events), "a short file was trusted"
    assert tracks[0].path.stat().st_size > whole_size * 0.9
    assert staged(folder) == []


def test_a_produced_file_judged_short_is_dropped_and_said(monkeypatch, tmp_path):
    monkeypatch.setattr(
        engine, "_short_of", lambda *args, **kwargs: "incomplete file (forced)"
    )
    events: list = []

    with pytest.raises(engine.DownloadError, match="incomplete file \\(forced\\)"):
        run(YOUTUBE_URL, tmp_path / "forced", events)

    folder = tmp_path / "forced"
    assert not any(p.suffix == ".flac" for p in folder.iterdir()), "the file was kept"
    assert any("incomplete file (forced)" in note for note in notes(events))


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses an open file")
def test_a_held_file_ends_as_a_skip_instead_of_a_crash(tmp_path):
    # A first download to copy from, so the held file is a real one.
    whole = run(TRACK_URL, tmp_path / "downloads", [])[0].path
    folder = tmp_path / "held"
    folder.mkdir()
    short = folder / "From Zero (Intro).flac"
    shutil.copyfile(whole, short)
    data = short.read_bytes()
    short.write_bytes(data[: len(data) // 10])  # short, and about to be held

    handle = short.open("rb")  # a player with the track in it
    events: list = []
    try:
        tracks = run(TRACK_URL, folder, events)
    except OSError as error:
        raise AssertionError(f"the run died on a file it could not remove: {error}")
    finally:
        handle.close()

    skipped = notes(events)
    assert skipped, "nothing was said about a file it could not replace"
    assert skipped[0].startswith(engine.INCOMPLETE), skipped
    assert tracks == []  # nothing was fetched into a file that would not go
