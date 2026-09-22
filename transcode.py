"""Audio conversion for a machine that has no ffmpeg executable.

The desktop builds run ffmpeg (see `engine.find_ffmpeg`): one process per
track, more formats and more options than anything else offers. A phone does
not get to: Android refuses to execute a binary an app shipped, and what the
APK carries is ffmpeg's *libraries*, not its CLI. There PyAV - the same
libraries bound into Python - does the work in-process: the stream yt-dlp saved
is decoded and re-encoded into the container the user asked for, and the tags
and the cover are written afterwards with mutagen and Pillow.

PyAV and Pillow are imported only when a conversion is actually asked for, so a
desktop build carries neither.
"""

from __future__ import annotations

import io
from pathlib import Path

# Target format -> (PyAV encoder, container format, file extension, sample
# format the encoder wants). The sample formats are the ones the ffmpeg command
# line ends up with on the desktop path: FLAC keeps 24 bits (ffmpeg's encoder
# takes s32 from a lossy source), PCM is 16-bit, and the lossy encoders get
# their own planar floats. `libmp3lame` is the only MP3 encoder ffmpeg has, and
# the ffmpeg Flet builds for Android ships without it, so `can_convert` reports
# "mp3" as unavailable there.
CODECS: dict[str, tuple[str, str, str, str]] = {
    "flac": ("flac", "flac", ".flac", "s32"),
    "mp3": ("libmp3lame", "mp3", ".mp3", "s16p"),
    "m4a": ("aac", "mp4", ".m4a", "fltp"),
    "wav": ("pcm_s16le", "wav", ".wav", "s16"),
}
# Lossy targets are asked for at the same bitrate the ffmpeg command line uses;
# the lossless and PCM ones keep the encoder's own default.
BITRATES = {"mp3": 320_000, "m4a": 320_000}
# The shape a cover is shown in, the crop `engine.ART_CROP_FILTER` applies with
# ffmpeg.
ART_ASPECT = 4 / 3


def extension(target_format: str) -> str:
    """File extension a converted track ends up with."""
    return CODECS[target_format][2]


def can_convert(target_format: str) -> bool:
    """True when this machine can produce `target_format` without ffmpeg."""
    return target_format in CODECS and _encoder(CODECS[target_format][0]) is not None


def convert(source: Path, target_format: str, destination: Path) -> None:
    """Re-encode `source` into `destination` as `target_format`.

    Raises RuntimeError when there is no encoder for it here - the caller has
    already asked `can_convert`.
    """
    import av

    codec_name, container, _, sample_format = CODECS[target_format]
    encoder = _encoder(codec_name)
    if encoder is None:
        raise RuntimeError(f"no {target_format} encoder is available")

    with av.open(str(source)) as incoming:
        stream = incoming.streams.audio[0]
        rate = stream.codec_context.rate
        layout = stream.codec_context.layout
        with av.open(str(destination), "w", format=container) as outgoing:
            outgoing.metadata["encoder"] = f"Cadenza ({codec_name})"
            out_stream = outgoing.add_stream(codec_name, rate=rate)
            out_stream.layout = layout.name if layout is not None else "stereo"
            out_stream.format = sample_format
            if bitrate := BITRATES.get(target_format):
                out_stream.bit_rate = bitrate
            resampler = av.AudioResampler(
                format=sample_format, layout=out_stream.layout, rate=rate
            )
            # Decode and re-encode frame by frame, then drain both ends: the
            # encoder holds samples back until it has a full block.
            for frame in incoming.decode(stream):
                for resampled in resampler.resample(frame):
                    for packet in out_stream.encode(resampled):
                        outgoing.mux(packet)
            for resampled in resampler.resample(None):
                for packet in out_stream.encode(resampled):
                    outgoing.mux(packet)
            for packet in out_stream.encode(None):
                outgoing.mux(packet)


def cover_from(thumbnail: Path) -> bytes | None:
    """The thumbnail as a cover picture, or None when it cannot be read.

    Centre-cropped to 4:3, the shape a cover is shown in and the crop the
    desktop build asks ffmpeg for: a wide frame loses the margins it was padded
    with, a tall one a little top and bottom - never stretched, never enlarged.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(thumbnail) as picture:
            image = picture.convert("RGB")
            width, height = image.size
            if width * 3 > height * 4:  # wider than a cover: trim the sides
                cropped = int(round(height * ART_ASPECT))
                left = (width - cropped) // 2
                box = (left, 0, left + cropped, height)
            else:  # narrower: trim top and bottom
                cropped = int(round(width / ART_ASPECT))
                top = (height - cropped) // 2
                box = (0, top, width, top + cropped)
            buffer = io.BytesIO()
            image.crop(box).save(buffer, "PNG")
            return buffer.getvalue()
    except Exception:  # noqa: BLE001 - a cover is a bonus, never a failure
        return None


def _encoder(name: str):
    """PyAV's encoder called `name`, or None when this build has no such one."""
    try:
        import av
    except ImportError:
        return None
    try:
        return av.codec.Codec(name, "w")
    except Exception:  # noqa: BLE001 - unknown or decode-only codec
        return None
