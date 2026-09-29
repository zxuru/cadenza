"""A file another program has open: the one thing that can refuse to go.

Windows does not remove a file with an open handle, and the checks remove
files. A track someone is listening to - or an indexer or an antivirus
holding - must not be able to break the run that touched it, and the failure
path that sweeps a failed track's leftovers must not replace the original
failure with a complaint about a locked file.
"""

from __future__ import annotations

import os

import pytest

import engine

pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="only Windows refuses to delete an open file"
)


def test_a_file_that_is_free_goes(tmp_path):
    path = tmp_path / "track.flac"
    path.write_bytes(b"audio")

    assert engine._forget(path) is True
    assert not path.exists()


def test_a_file_that_is_held_says_so_instead_of_raising(tmp_path):
    path = tmp_path / "track.flac"
    path.write_bytes(b"audio")
    held = path.open("rb")
    try:
        assert engine._forget(path) is False, "it claimed to delete a file it could not"
        assert path.exists()
    finally:
        held.close()


def test_forgetting_something_that_is_not_there_is_not_a_failure(tmp_path):
    assert engine._forget(tmp_path / "never existed.flac") is True


def test_the_failure_path_sweeps_without_raising_over_a_held_file(tmp_path):
    held = tmp_path / "01 - Track.flac"
    held.write_bytes(b"audio")
    (tmp_path / "01 - Track.webp").write_bytes(b"picture")
    open_handle = held.open("rb")
    try:
        engine._discard(tmp_path, 1, "Track", True)
    finally:
        open_handle.close()

    # The cover went, the file that would not go stayed - and nothing raised,
    # so the failure this sweep was cleaning up after is still the one shown.
    assert not (tmp_path / "01 - Track.webp").exists()
    assert held.exists()
