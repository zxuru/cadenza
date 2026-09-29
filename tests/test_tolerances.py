"""The two allowances, and the one property both have to keep.

`SHORT_TOLERANCE` is what a produced file may be short of its own source by;
`EXISTING_TOLERANCE` is what a file already in the folder may be short of
Spotify by before it is fetched again. The property is the one that stops a
perfectly good file from being deleted and downloaded again on every run: a
file that passed the first check can never fail the second, because the
second allows for everything the matcher accepted plus everything the first
allows.
"""

from __future__ import annotations

import pytest

import engine
import metadata


def test_the_existing_allowance_is_everything_added_up():
    assert engine.EXISTING_TOLERANCE == (
        metadata.DURATION_TOLERANCE + engine.SHORT_TOLERANCE
    )
    assert engine.EXISTING_TOLERANCE > metadata.DURATION_TOLERANCE


def test_a_file_the_produce_check_accepted_is_never_refetched():
    """The invariant, as a sweep rather than an example.

    The matcher accepts an upload within `DURATION_TOLERANCE` of Spotify's
    length, the produce check accepts a file within `_produce_tolerance` of
    that upload's length, and every rounding in between is against the file.
    Nothing about that sum may ever reach the bar an existing file is held to.
    """
    spotify = 190.427
    for upload in (spotify - metadata.DURATION_TOLERANCE, spotify + metadata.DURATION_TOLERANCE):
        for lost in (0.0, 0.5, 1.0, engine.SHORT_TOLERANCE):
            measured = upload - lost
            produce_kept = (
                engine._short_of(
                    engine._Playing(measured),
                    upload,
                    engine._produce_tolerance(upload),
                )
                is None
            )
            refetched = (
                engine._short_of(
                    engine._Playing(measured),
                    spotify,
                    engine.EXISTING_TOLERANCE,
                )
                is not None
            )
            assert not (produce_kept and refetched), (
                f"upload={upload} lost={lost} would be downloaded again every run"
            )


def test_a_file_short_of_what_it_was_meant_to_hold_is_refetched():
    # What an earlier run was killed inside: well past anything the matcher
    # and the produce check could have let through.
    spotify = 190.427
    problem = engine._short_of(
        engine._Playing(spotify - 40), spotify, engine.EXISTING_TOLERANCE
    )

    assert problem is not None
    assert problem.startswith(engine.INCOMPLETE)


def test_the_allowance_a_file_is_measured_with_is_not_a_percentage():
    # The band is absolute at both ends and proportional in between: a value
    # that scaled with the track would let a ten-minute file hide a minute.
    assert engine._produce_tolerance(5.0) == engine.MIN_SHORT_TOLERANCE
    assert engine._produce_tolerance(60.0) == pytest.approx(1.2)
    assert engine._produce_tolerance(6000.0) == engine.SHORT_TOLERANCE
