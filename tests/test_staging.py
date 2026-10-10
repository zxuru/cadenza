"""Staging in `_convert_downloads`: published in one step, or not at all.

PyAV cannot be run on every machine (its DLL is blocked on one of them), so
the converter is stood in by one that writes what it is asked to write. What
is under test is the staging around it: the tags land on the `.part`, the
finished track takes its name in a single step, and a failure in the middle
publishes nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import engine
import metadata
import transcode


@pytest.fixture
def made(tmp_path, monkeypatch):
    """A download as `_convert_downloads` finds it, converter and tag writer
    replaced by ones that only do what they are asked to do."""
    source = tmp_path / "Song.webm"
    source.write_bytes(b"raw stream")
    (tmp_path / "Song.webp").write_bytes(b"picture")
    state: dict = {"tagged": None, "converted": None}

    def fake_convert(source: Path, target_format: str, destination: Path) -> None:
        state["converted"] = Path(destination)
        Path(destination).write_bytes(b"converted audio")

    def fake_tags(path, tags, cover, **kwargs) -> bool:
        state["tagged"] = Path(path)
        state["suffix"] = kwargs.get("suffix")
        Path(path).write_bytes(Path(path).read_bytes() + b"+tags")
        return True

    monkeypatch.setattr(transcode, "convert", fake_convert)
    monkeypatch.setattr(transcode, "cover_from", lambda thumbnail: b"png cover")
    monkeypatch.setattr(metadata, "write_tags", fake_tags)
    entry = {"title": "Song", "requested_downloads": [{"filepath": str(source)}]}
    return tmp_path, entry, state


def test_it_publishes_in_one_step(made):
    root, entry, state = made

    engine._convert_downloads(entry, "flac", None)

    assert state["converted"] == root / "Song.flac.part", "the audio went to the name"
    assert state["tagged"] == root / "Song.flac.part", "the tags went to the name"
    assert state["suffix"] == ".flac", "the writer was told which container it is"
    assert (root / "Song.flac").read_bytes() == b"converted audio+tags"
    assert not (root / "Song.flac.part").exists(), "the staging file was left behind"
    assert not (root / "Song.webm").exists(), "the source outlives the track"
    assert not (root / "Song.webp").exists(), "the cover was embedded and swept"
    assert entry["requested_downloads"][0]["filepath"] == str(root / "Song.flac")
    assert entry["filepath"] == str(root / "Song.flac")


def test_a_tag_write_that_fails_publishes_nothing(made, monkeypatch):
    root, entry, _state = made

    def angry_tags(path, tags, cover, **kwargs) -> bool:
        Path(path).write_bytes(b"half tagged")
        raise RuntimeError("tagging died")

    monkeypatch.setattr(metadata, "write_tags", angry_tags)

    with pytest.raises(RuntimeError, match="tagging died"):
        engine._convert_downloads(entry, "flac", None)

    assert not (root / "Song.flac").exists(), "a failed run published a track"
    assert not (root / "Song.flac.part").exists(), "the staging file was left behind"
    assert (root / "Song.webm").exists(), "the source went before the track existed"


def test_a_conversion_that_fails_publishes_nothing(made, monkeypatch):
    root, entry, _state = made

    def angry_convert(source, target_format, destination) -> None:
        Path(destination).write_bytes(b"half an encode")
        raise RuntimeError("encode died")

    monkeypatch.setattr(transcode, "convert", angry_convert)

    with pytest.raises(RuntimeError, match="encode died"):
        engine._convert_downloads(entry, "flac", None)

    assert not (root / "Song.flac").exists()
    assert not (root / "Song.flac.part").exists()
    assert (root / "Song.webm").exists()
