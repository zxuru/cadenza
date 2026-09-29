"""`transcode.convert`: the audio goes to a `.part`, the name comes last.

PyAV is stood in by the smallest thing that walks the same calls - what is
under test is that the encoder writes beside the track's name, that the name
is taken only once both files are closed, and that a failure in the middle
removes the `.part` instead of publishing it.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import transcode


class _Frame:
    samples = 1024


class _Packet:
    pass


class _Stream:
    def __init__(self, name: str = "stereo") -> None:
        self.name = name
        self.codec_context = types.SimpleNamespace(rate=48000, layout=self)
        self.format = None
        self.bit_rate = None
        self.layout = None

    def encode(self, frame=None):
        if frame is not None:
            yield _Packet()


class _Container:
    """File opened for reading or writing; writes its bytes when it closes."""

    def __init__(self, state, path, mode: str, format=None) -> None:
        self.state, self.path, self.mode = state, path, mode
        self.metadata = {}
        self.streams = types.SimpleNamespace(audio=[_Stream()])

    def __enter__(self):
        if self.mode == "w":
            self.state["written_to"].append(self.path)
        return self

    def __exit__(self, *args):
        if self.mode == "w":
            if self.state["fail"]:
                self.state["fail"] = False
                raise RuntimeError("the encoder died inside")
            Path(self.path).write_bytes(b"encoded audio")
        return False

    def decode(self, stream):
        for _ in range(4):
            yield _Frame()

    def mux(self, packet) -> None:
        pass

    def add_stream(self, codec, rate=None):
        return _Stream()


class _Resampler:
    def __init__(self, format=None, layout=None, rate=None) -> None:
        pass

    def resample(self, frame):
        if frame is not None:
            yield _Frame()


@pytest.fixture
def fake_av(monkeypatch):
    """The `av` module, reduced to what `convert` touches - PyAV's own DLL is
    blocked on some machines, and the flow is what needs proving."""
    state = {"written_to": [], "fail": False}
    module = types.ModuleType("av")
    module.open = lambda path, mode="r", format=None: _Container(state, path, mode, format)
    module.AudioResampler = _Resampler
    module.codec = types.SimpleNamespace(Codec=lambda name, direction: object())
    monkeypatch.setitem(sys.modules, "av", module)
    return state


def test_the_audio_lands_on_a_part_and_the_name_comes_later(tmp_path, fake_av):
    source, destination = tmp_path / "raw.webm", tmp_path / "track.flac"
    source.write_bytes(b"raw stream")

    transcode.convert(source, "flac", destination)

    assert fake_av["written_to"] == [str(destination) + ".part"]
    assert destination.read_bytes() == b"encoded audio"
    assert not (tmp_path / "track.flac.part").exists()


def test_it_replaces_a_track_that_was_already_there(tmp_path, fake_av):
    source, destination = tmp_path / "raw.webm", tmp_path / "track.flac"
    source.write_bytes(b"raw stream")
    destination.write_bytes(b"an older take")

    transcode.convert(source, "flac", destination)

    assert destination.read_bytes() == b"encoded audio"


def test_a_failure_inside_publishes_nothing(tmp_path, fake_av):
    source, destination = tmp_path / "raw.webm", tmp_path / "track.flac"
    source.write_bytes(b"raw stream")
    destination.write_bytes(b"the track that was already there")
    fake_av["fail"] = True

    with pytest.raises(RuntimeError, match="died inside"):
        transcode.convert(source, "flac", destination)

    assert destination.read_bytes() == b"the track that was already there"
    assert not (tmp_path / "track.flac.part").exists(), "the `.part` was left behind"
