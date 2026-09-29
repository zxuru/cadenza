"""Silence inside a track: the one thing a length never shows.

A file can hold every second it ever had and still go quiet in the middle of
it, which is what a player mutes for and what a length check passes. What
this guards is the other side of the same coin too: a machine that cannot
play a file through has to *say* the check did not happen, instead of the run
passing quietly with nothing checked.
"""

from __future__ import annotations

import pytest

import engine


def test_a_hole_in_the_middle_is_found(audio):
    made = audio("hole.flac", seconds=10, silence=(4, 5))

    playing = engine._playing(made)

    assert playing.seconds == pytest.approx(10, abs=0.1)
    assert len(playing.gaps) == 1
    start, end = playing.gaps[0]
    assert start == pytest.approx(4, abs=0.1)
    assert end == pytest.approx(5, abs=0.1)


def test_silence_at_the_edges_is_the_intro_and_the_outro(audio):
    made = audio("edges.flac", seconds=10, silence=(0.2, 1.0))
    playing = engine._playing(made)
    assert playing.gaps == ()

    made = audio("tail.flac", seconds=10, silence=(9.2, 9.9))
    assert engine._playing(made).gaps == ()


def test_music_is_not_mistaken_for_a_hole(audio, ffmpeg):
    # A tone played all the way through is as far from silence as the file
    # gets, and no gap is reported - which is the false positive that would
    # make this note worthless if it happened.
    made = audio("plain.flac", seconds=12)

    assert engine._playing(made).gaps == ()


def test_the_note_says_how_many_and_how_long():
    note = engine._gap_note(((4.0, 5.0),))
    assert note == "1 silent gap inside the audio (1.00s)"

    note = engine._gap_note(((4.0, 5.0), (10.0, 10.5)))
    assert note == "2 silent gaps inside the audio (1.50s)"


def test_a_kept_track_is_told_but_never_refused(audio):
    made = audio("hole.flac", seconds=10, silence=(4, 5))
    playing = engine._playing(made)

    events = []
    engine._say_check(events.append, playing, "Two Faced")

    # The silence changes nothing about the track: it is on disk either way,
    # so what comes out is a note - never a skip, never a retry entry.
    assert len(events) == 1
    assert events[0].stage == "attention"
    assert events[0].title == "Two Faced"
    assert "silent gap" in events[0].note
    assert engine._short_of(playing, 10.0, engine._produce_tolerance(10.0)) is None


def test_something_worth_saying_comes_out_and_nothing_else():
    said = []
    engine._say_check(said.append, engine._Playing(None), "Intro")
    assert said[0].note == engine.UNMEASURED

    said = []
    engine._say_check(said.append, engine._Playing(190.0), "Intro")
    assert said == []  # a file with neither: nothing to hear


def test_a_machine_without_a_decoder_says_the_check_did_not_happen(
    monkeypatch, audio
):
    made = audio("track.flac", seconds=6)
    # A phone: no ffmpeg, and PyAV will not read this file either.
    monkeypatch.setattr(engine, "find_ffmpeg", lambda: None)
    monkeypatch.setattr(engine, "_pyav_length", lambda path: None)

    playing = engine._playing(made)
    assert playing.seconds is None

    events = []
    engine._say_check(events.append, playing, "Intro")

    # The check did not happen, and the run says so instead of reporting a
    # file it never looked at as fine.
    assert events[0].stage == "attention"
    assert events[0].note == engine.UNMEASURED
