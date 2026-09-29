"""The length check: what is kept, what is refused, and what is never assumed.

Every case here is one the app stakes a file on: a track that plays for as
long as its source said is whole and stays, one that comes up short is
refused, and anything that cannot be said is left alone rather than deleted.
"""

from __future__ import annotations

import pytest

import engine
import metadata

FORMATS = ("flac", "m4a", "mp3", "wav")


@pytest.mark.parametrize("extension", FORMATS)
def test_a_file_that_plays_its_length_is_kept(audio, extension):
    made = audio(f"track.{extension}", seconds=6)

    assert engine._short_of(engine._playing(made), 6.0, engine._produce_tolerance(6.0)) is None


@pytest.mark.parametrize("extension", FORMATS)
def test_a_truncated_file_is_refused(audio, cut, extension):
    made = audio(f"track.{extension}", seconds=6)
    shortened = cut(made, 0.5)

    problem = engine._short_of(
        engine._playing(shortened), 6.0, engine._produce_tolerance(6.0)
    )

    assert problem is not None
    assert problem.startswith(engine.INCOMPLETE)


def test_a_file_that_is_not_audio_at_all_is_refused(audio):
    made = audio("track.flac", seconds=6)
    made.write_bytes(b"this is not a file a player would open")

    problem = engine._short_of(engine._playing(made), 6.0, engine._produce_tolerance(6.0))

    assert problem is not None
    assert "0.0s of" in problem


def test_only_short_is_wrong_longer_stays(audio):
    made = audio("track.flac", seconds=8)

    # The upload says six seconds and the file plays eight: the two disagree
    # about where the recording ends, and fetching it again would not change it.
    assert engine._short_of(engine._playing(made), 6.0, engine._produce_tolerance(6.0)) is None


def test_a_doubt_never_deletes_a_track():
    # Nothing could measure it: no decoder here, or one that would not answer.
    assert engine._short_of(engine._Playing(None), 180.0, engine.EXISTING_TOLERANCE) is None
    # No length was ever stated, so there is nothing to fall short of.
    assert engine._short_of(engine._Playing(180.0), None, engine.EXISTING_TOLERANCE) is None
    assert engine._short_of(engine._Playing(180.0), 0, engine.EXISTING_TOLERANCE) is None


def test_a_missing_file_is_refused(tmp_path):
    problem = engine._short_of(engine._playing(tmp_path / "never made.flac"), 6.0, 2.0)

    assert problem is not None  # nothing plays of a file that is not there


def test_the_allowance_grows_with_the_track():
    assert engine._produce_tolerance(5.0) == engine.MIN_SHORT_TOLERANCE
    assert engine._produce_tolerance(22.174) == engine.MIN_SHORT_TOLERANCE
    assert engine._produce_tolerance(60.0) == pytest.approx(1.2)
    assert engine._produce_tolerance(190.427) == engine.SHORT_TOLERANCE
    assert engine._produce_tolerance(3600.0) == engine.SHORT_TOLERANCE
    # A length nobody could read gets the looser end of the band.
    assert engine._produce_tolerance(None) == engine.SHORT_TOLERANCE
    assert engine._produce_tolerance("not a length") == engine.SHORT_TOLERANCE


def test_a_short_track_can_still_be_a_ninth_short(audio, cut):
    # The floor is what makes this possible: one number for every track would
    # be far too much of a twenty-second intro.
    made = audio("intro.flac", seconds=22)
    shortened = cut(made, 0.85)

    problem = engine._short_of(engine._playing(shortened), 22.0, engine._produce_tolerance(22.0))

    assert problem is not None
