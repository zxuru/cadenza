"""The plumbing behind the behaviour: options, reasons and the retry list.

Nothing here downloads anything - what is under test is that the pieces are
connected: the abort reaches the downloader that would otherwise skip a
fragment, a reason as long and as accented as the real ones survives
`pending.json`, a rejected track is remembered by its position, and the
logger keeps the console quiet while it keeps the messages.
"""

from __future__ import annotations

import contextlib
import io

import pytest
import yt_dlp
from yt_dlp.downloader.fragment import FragmentFD

import engine
import pending


def test_the_abort_reaches_the_fragment_downloader():
    opts = engine._base_opts()

    with yt_dlp.YoutubeDL(opts) as ydl:
        from_downloader = FragmentFD(ydl, ydl.params).params.get(
            "skip_unavailable_fragments"
        )
        from_params = ydl.params.get("skip_unavailable_fragments")

    assert opts["skip_unavailable_fragments"] is False
    assert from_params is False
    assert from_downloader is False, "the downloader would still skip the fragment"


def test_a_long_accented_reason_survives_pending_json(tmp_path, monkeypatch):
    monkeypatch.setattr(pending, "path", lambda: tmp_path / "pending.json")
    reason = (
        'incomplete file (180.4s of 190.4s) [[youtube] Some android client https '
        'formats have been skipped - se corta "Canción (En Vivo)" ✨'
    )
    item = pending.Item(
        url="https://open.spotify.com/album/5QfFvOMOJ0CrIDmu33RmSJ",
        root=str(tmp_path),
        format="flac",
        index=7,
        title="From Zero (Intro)",
        reason=reason,
    )

    pending.save(pending.merged(pending.merged([], [item]), [item]))
    reloaded = pending.load()
    back = next(entry for entry in reloaded if entry.index == 7)

    assert back.reason == reason, "the reason was rewritten on the way out"
    assert back.attempts == 2, "a second failed run counted up"
    assert pending.due(reloaded), "still worth trying by itself"


def test_a_rejected_track_is_remembered_by_its_position():
    seen = []
    engine._reject(
        seen.append,
        {"title": "Two Faced", "playlist_index": 8, "playlist_count": 14},
        "incomplete file (180.4s of 190.4s)",
    )

    progress = seen[0]
    assert progress.stage == "skipped"
    assert (progress.track_index, progress.track_count) == (8, 14)
    assert progress.note != engine.ALREADY_THERE  # so `remember` keeps it


def test_a_single_track_has_no_position_so_it_is_only_reported():
    seen = []
    engine._reject(seen.append, {"title": "One Song"}, "incomplete file")

    assert seen[0].track_index is None  # reported, not remembered: no playlist


def test_the_logger_keeps_the_console_quiet_while_it_keeps_the_messages():
    log = engine._YtDlpLog()
    console = io.StringIO()

    with contextlib.redirect_stdout(console), contextlib.redirect_stderr(console):
        log.debug("chatty progress")
        log.info("chatty info")
        log.warning("WARNING: something YouTube did")
        log.error("ERROR: something worse")

    assert console.getvalue() == "", "yt-dlp's chatter reached the console"
    assert log.warnings == [
        "WARNING: something YouTube did",
        "ERROR: something worse",
    ]


def test_the_logger_keeps_only_the_last_few():
    log = engine._YtDlpLog()
    for index in range(engine.KEPT_WARNINGS + 5):
        log.warning(f"warning {index}")

    assert len(log.warnings) == engine.KEPT_WARNINGS
    assert log.warnings[-1] == f"warning {engine.KEPT_WARNINGS + 4}"


def test_a_warning_rides_along_with_the_failure_it_explains():
    log = engine._YtDlpLog()

    assert engine._told(log, "boom") == "boom"  # nothing was warned
    assert engine._told(None, "boom") == "boom"

    log.warning("formats were skipped")
    assert engine._told(log, "boom") == "boom [formats were skipped]"
    assert engine._told(log, "") == "formats were skipped"


def test_each_call_gets_its_own_log():
    assert engine._base_opts()["logger"] is not engine._base_opts()["logger"]


@pytest.mark.parametrize(
    ("expected", "tolerance", "rejected"),
    [
        (190.0, engine.SHORT_TOLERANCE, False),  # exactly its source's length
        (190.0, engine.SHORT_TOLERANCE, True),  # below the allowance
        (None, engine.SHORT_TOLERANCE, False),  # no length to fall short of
    ],
)
def test_a_file_is_only_refused_when_it_is_shorter(expected, tolerance, rejected):
    measured = 187.0 if rejected else 190.0  # a second and a half, or exactly it
    problem = engine._short_of(engine._Playing(measured), expected, tolerance)

    assert (problem is not None) is rejected
