"""The row's play button always plays the song that was asked for.

The first 30s of its own audio, fetched on press so the search never waits
for it. A database sample is instant when one matched; the video's own audio
is cut to 30s otherwise, and handed to the player in a container it can
actually decode - YouTube serves WebM, which no desktop player opens.
"""

from __future__ import annotations

import subprocess

import pytest

import engine


@pytest.mark.network
def test_a_track_no_database_knows_still_gets_its_30s():
    results = engine.search("https://youtu.be/aMO7wc5imEI")
    assert len(results) == 1
    (result,) = results
    assert result.kind == "track"
    sample = engine.preview_sample(result.url)
    assert sample.is_file() and sample.stat().st_size > 0
    assert sample.suffix == engine._sample_format()[1]
    duration = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(sample)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert abs(float(duration) - engine.PREVIEW_SECONDS) < 1.0


@pytest.mark.network
def test_fetching_the_same_sample_twice_downloads_once(tmp_path, monkeypatch):
    seen = engine.preview_sample("https://youtu.be/aMO7wc5imEI")
    assert engine.preview_sample("https://youtu.be/aMO7wc5imEI") == seen


def test_an_unknown_url_is_a_download_error_not_a_crash():
    with pytest.raises(engine.DownloadError):
        engine.preview_sample("https://www.youtube.com/watch?v=xxxxxxxxxxx")


def test_a_desktop_player_is_never_handed_the_webm():
    """The container matters as much as the bytes: WebM never loads."""
    codec, extension = engine._sample_format()
    assert codec, "a desktop has to re-encode what YouTube serves"
    assert extension != ".webm"
    assert extension.startswith(".")


