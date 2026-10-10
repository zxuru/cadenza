"""The failures the window shows: a code, and a sentence the user can act on.

The bug this guards: yt-dlp's own line reached the status bar verbatim - its
terminal colour, the same sentence twice, and the reason nowhere - so the user
read `ERROR: unable to download video data: HTTP Error 403: Forbidden
[ERROR: ...]` where "YouTube refused the download" belonged.
"""

from __future__ import annotations

import json
from pathlib import Path

import engine
import errors

PROJECT = Path(__file__).resolve().parents[1]

# The line a refused download actually arrived as, escape sequences included.
REFUSED = (
    "\x1b[0;31mERROR:\x1b[0m unable to download video data: HTTP Error 403: Forbidden"
)
SAID = "unable to download video data: HTTP Error 403: Forbidden"


def test_the_tools_colour_never_reaches_the_window():
    assert errors.clean(REFUSED) == SAID
    # The same code with its escape byte already lost on the way here.
    assert errors.clean("[0;31mERROR:[0m boom") == "boom"


def test_a_message_is_one_line():
    assert errors.clean("boom\n  went  wrong\n") == "boom went wrong"


def test_a_sentence_repeated_beside_itself_is_said_once():
    assert errors.clean(f"{SAID} [{SAID}]") == SAID


def test_a_warning_that_says_something_else_is_kept():
    assert errors.clean("boom [formats were skipped]") == "boom [formats were skipped]"


def test_a_refusal_is_named_by_its_status_not_by_the_sentence_around_it():
    """`unable to download` reads as a network failure; the 403 in the same
    line is the reason, so the statuses are looked for first."""
    failure = errors.describe(REFUSED)

    assert failure.code == "E-403"
    assert failure.key == "error_403"
    assert failure.detail == ""


def test_the_line_is_the_code_and_then_the_sentence():
    line = errors.describe(REFUSED).line(lambda key, **values: f"<{key}>")

    assert line == "E-403 · <error_403>"


def test_the_reasons_a_run_leaves_a_track_out_for_are_each_named():
    """Every one of these reaches the skip list, and none of them is a tool's
    line: a code apiece is what stops the list reading as one long complaint."""
    reasons = {
        engine.INCOMPLETE + " (12.3s of 200.0s)": "E-SHORT",
        engine.NO_MATCH: "E-NOMATCH",
        engine.MISSING_AUDIO: "E-EMPTY",
        engine.REFUSED: "E-403",
        "Spotify will not show 37i9: private, deleted or not published": "E-SPOTIFY",
        "Unsupported format: ogg": "E-LINK",
        "ffmpeg was not found. Install it or `pip install imageio-ffmpeg`.": "E-CONV",
    }

    for reason, code in reasons.items():
        assert errors.describe(reason).code == code, reason


def test_an_unmatched_failure_is_still_named_and_keeps_a_readable_tail():
    failure = errors.describe("a failure nobody mapped: " + "x" * 400)

    assert failure.code == errors.OTHER
    assert failure.key == "error_other_detail"
    assert len(failure.detail) <= errors.DETAIL_LIMIT
    assert failure.detail.endswith("…")


def test_an_empty_failure_has_no_tail_to_show():
    failure = errors.describe("   ")

    assert failure.code == errors.OTHER
    assert failure.key == "error_other"


def test_the_codes_are_a_closed_set_of_names():
    codes = errors.codes()

    assert len(set(codes)) == len(codes)
    assert all(code.startswith("E-") for code in codes)


def test_every_code_has_a_sentence_in_every_language():
    wanted = {key for _, key, _ in errors._TABLE} | {
        "error_other",
        "error_other_detail",
    }
    files = sorted((PROJECT / "locales").glob("*.json"))
    assert files, "no locale files to check"

    for path in files:
        catalog = json.loads(path.read_text(encoding="utf-8"))
        assert not wanted - set(catalog), (
            f"{path.name} is missing {wanted - set(catalog)}"
        )


def test_the_engine_shows_that_cleaned_line_and_not_the_raw_one():
    assert engine._clean_message(Exception(REFUSED)) == SAID
