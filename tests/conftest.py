"""What every test here shares: audio made on the spot, nothing from outside.

No test but the marked ones may touch the network or a file outside its own
`tmp_path`, so a green run means the same thing on every machine: the checks
make their own files with ffmpeg (skipping when there is none), and the
network ones are deselected by default - see `pyproject.toml`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import engine


@pytest.fixture
def ffmpeg() -> str:
    """The ffmpeg the app itself would use, or a skip: a machine with no
    ffmpeg has no length or gap check to exercise."""
    path = engine.find_ffmpeg()
    if path is None:
        pytest.skip("this machine has no ffmpeg")
    return path


@pytest.fixture
def audio(ffmpeg, tmp_path):
    """A synthesized file: `audio(seconds=6, silence=(2, 3), rate=48000, ...)`.

    A tone is enough for everything under test - how long it plays, whether a
    hole in it is seen, whether a file cut short is refused - and it costs
    nothing to make. The name's extension picks the codec, as it does for
    ffmpeg anywhere else.
    """

    def make(
        name: str = "track.flac",
        seconds: float = 6.0,
        silence: tuple[float, float] | None = None,
        rate: int = 48000,
        channels: int = 2,
    ) -> Path:
        target = tmp_path / name
        command = [
            ffmpeg, "-y", "-v", "error", "-f", "lavfi",
            "-i", f"sine=frequency=440:duration={seconds}:sample_rate={rate}",
        ]
        if silence is not None:
            start, end = silence
            command += ["-af", f"volume=0:enable='between(t,{start},{end})'"]
        command += ["-ac", str(channels), str(target)]
        subprocess.run(command, check=True, capture_output=True)
        return target

    return make


@pytest.fixture
def cut():
    """A file with the tail of its bytes taken off: a truncation that its own
    header will not admit to (a FLAC or an MP4 keeps claiming the length it
    was written with, which is why the checks play the file instead)."""

    def take(path: Path, fraction: float) -> Path:
        shortened = path.with_name(f"short-{path.name}")
        shortened.write_bytes(path.read_bytes()[: int(path.stat().st_size * fraction)])
        return shortened

    return take


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """Point the app's config directory at the test's own `tmp_path`.

    Settings, the retry list and the log all live there. A test that starts the
    app must not leave a line of either in the real `~/.config`: that is the
    user's directory, not the test's.
    """
    import settings

    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setattr(settings, "config_dir", lambda: home)
    return home
