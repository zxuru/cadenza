"""yt-dlp engine: search YouTube Music, inspect albums, download tracks or whole albums.

Everything here is blocking and thread-safe: the Flet UI runs it through
`asyncio.to_thread` and renders progress from the callback. YouTube supplies
the audio; the tags and the cover come from a music database (see `metadata`),
because a video's channel, category and thumbnail are not release metadata.
"""

from __future__ import annotations

import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import quote

import yt_dlp
from yt_dlp.utils import DownloadError as YtDlpDownloadError

import bundle
import metadata
import transcode

MUSIC_SEARCH_URL = "https://music.youtube.com/search?q={query}"
SEARCH_LIMIT = 10
RESOLVE_WORKERS = 8

# Format key -> audio quality passed to FFmpegExtractAudio. `None` leaves the
# codec defaults alone, which is what the lossless and PCM targets want.
# Display names live in i18n.
FORMATS: dict[str, str | None] = {
    "flac": None,
    "mp3": "320",
    "m4a": "320",
    "wav": None,
}
DEFAULT_FORMAT = "flac"

# Containers that can hold a cover picture. WAV has no picture block, and yt-dlp
# fails the whole download when it is asked to embed one there.
ART_FORMATS = frozenset({"flac", "mp3", "m4a"})

# The picture is stored in the shape a cover is shown in, 4:3, rather than as
# the 16:9 video frame YouTube serves. A centre crop, never enlarged, so a wider
# picture loses its sides - the blurred margins YouTube pads artwork with - and
# a narrower one a little top and bottom: no distortion, no black bars.
ART_CROP_FILTER = "crop=w='min(iw,ih*4/3)':h='min(ih,iw*3/4)'"

# yt-dlp enables only deno by default. YouTube needs a JS runtime to decipher
# stream URLs - without one, extraction is deprecated and downloads fail with
# HTTP 403 - so opt in to every runtime we can find on PATH.
JS_RUNTIMES = ("deno", "node", "bun", "quickjs")
# Deno binary packaged next to the app by build.py, used when the machine has
# no JavaScript runtime of its own.
BUNDLED_RUNTIME_PATHS = ("jsrt/deno.exe", "jsrt/deno")
# An ffmpeg the bundle carries itself, looked up the same way. Desktop builds
# get theirs from imageio-ffmpeg; a platform whose wheels have none (Android)
# has to ship a binary, and this is where it is expected to sit.
BUNDLED_FFMPEG_PATHS = ("ffmpeg", "ffmpeg.exe")

# YouTube Music titles come prefixed with the result kind ("Album - Foo").
_TITLE_PREFIXES = ("Album - ", "Playlist - ", "Mix - ")
_ILLEGAL_PATH_CHARS = '<>:"/\\|?*'
_MAX_FOLDER_LENGTH = 120


class DownloadError(RuntimeError):
    """A search, inspection or download failed."""


class _TagFixupPP(yt_dlp.postprocessor.PostProcessor):
    """Add the tags extractors leave out, before metadata is written.

    `meta_*` keys on the info dict become tags verbatim. This is a
    post-processor rather than a hook because yt-dlp hands hook callbacks a
    copy of the info dict, so hook-side changes never reach post-processing.
    """

    def run(self, info: dict) -> tuple[list, dict]:
        # `date` defaults to the upload date; a music library wants the release date.
        release = str(info.get("release_date") or "")
        if len(release) == 8 and release.isdigit():
            info["meta_date"] = f"{release[:4]}-{release[4:6]}-{release[6:8]}"
        # Album entries carry no track number - their position in the album does.
        if index := info.get("playlist_index"):
            info["meta_track"] = str(index)
        return [], info


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One candidate shown in the results list."""

    kind: Literal["track", "album"]
    title: str
    url: str
    artist: str | None = None
    duration: int | None = None  # seconds, tracks only
    track_count: int | None = None  # albums only
    album: str | None = None
    # The source is a music upload rather than a video: yt-dlp only fills in an
    # artist, an album and a release date for one of those. Search results are
    # ranked on this, because a plain re-upload of the same recording carries a
    # channel name where the artist belongs and a category where the genre does.
    music: bool = False


@dataclass(frozen=True, slots=True)
class AlbumInfo:
    """Everything the confirmation dialog shows before downloading an album."""

    title: str
    url: str
    folder: str
    artist: str | None = None
    year: int | None = None
    track_count: int = 0
    total_duration: int | None = None
    track_titles: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Track:
    """A finished download."""

    title: str
    path: Path
    format: str


@dataclass(frozen=True, slots=True)
class Progress:
    """Snapshot of an in-flight download, handed to the UI from a worker thread."""

    stage: Literal["downloading", "converting", "tagging"]
    percent: float | None
    downloaded_bytes: int
    total_bytes: int | None
    speed: float | None  # bytes/second
    eta: int | None  # seconds
    title: str | None = None
    track_index: int | None = None  # 1-based position inside an album
    track_count: int | None = None


def find_ffmpeg() -> str | None:
    """Return an ffmpeg binary, or None when this machine has none.

    The system one wins, then a copy shipped next to the app, then the static
    binary inside imageio-ffmpeg.
    """
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    for relative in BUNDLED_FFMPEG_PATHS:
        shipped = bundle.find(relative)
        if shipped is not None and os.access(shipped, os.X_OK):
            return str(shipped)
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    try:
        # Raises when the wheel carries no binary for this platform (Android,
        # iOS) - the same "no ffmpeg here" the caller already handles.
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def available_formats() -> dict[str, str | None]:
    """The formats this machine can actually produce, in `FORMATS` order.

    With ffmpeg behind them all of them work. Without it the in-process
    converter decides: it encodes FLAC, M4A and WAV, and MP3 only where an MP3
    encoder was built in - which the ffmpeg libraries Flet ships for Android
    are not.
    """
    if find_ffmpeg() is not None:
        return dict(FORMATS)
    return {
        name: quality
        for name, quality in FORMATS.items()
        if transcode.can_convert(name)
    }


def find_js_runtimes() -> dict[str, dict[str, str]]:
    """Return the JavaScript runtimes yt-dlp may use, as `js_runtimes` config.

    YouTube needs one to decipher stream URLs. Prefer whatever the machine has
    installed, fall back to the copy shipped inside the executable.
    """
    runtimes = {
        name: {"path": path}
        for name in JS_RUNTIMES
        if (path := shutil.which(name)) is not None
    }
    if runtimes:
        return runtimes
    if (deno := _bundled_runtime()) is not None:
        return {"deno": {"path": str(deno)}}
    return {"deno": {}}  # yt-dlp's own default: extraction stays degraded


def _bundled_runtime() -> Path | None:
    """Locate the runtime packaged next to the app, if there is one."""
    for relative in BUNDLED_RUNTIME_PATHS:
        candidate = bundle.find(relative)
        if candidate is not None:
            return candidate
    return None


def search(query: str, limit: int = SEARCH_LIMIT) -> list[SearchResult]:
    """Find tracks and albums for `query`, or inspect `query` when it is a URL.

    The whole result page is resolved before it is ranked and cut down to
    `limit`: a re-upload YouTube Music happens to rank higher must not hide the
    release that sits further down the same page.
    """
    query = query.strip()
    if not query:
        raise DownloadError("Enter a song, an album or a link.")

    if _looks_like_url(query):
        return [_inspect_url(query)]

    results = _resolve_all(_music_candidates(query))
    if not results:
        # YouTube Music had nothing usable for this query.
        results = _resolve_all(_video_candidates(query))
    return _rank(results)[:limit]


def probe_album(url: str) -> AlbumInfo:
    """Read an album's or playlist's track list without downloading it."""
    with _ydl(extract_flat="in_playlist") as ydl:
        info = ydl.extract_info(url, download=False)

    entries = [entry for entry in (info.get("entries") or []) if entry]
    if not entries:
        raise DownloadError(f"No tracks found in {url}")

    title = _strip_title_prefix(info.get("title") or "Album")
    artist = _clean_artist(info.get("channel") or info.get("uploader"))
    durations = [entry.get("duration") for entry in entries if entry.get("duration")]
    return AlbumInfo(
        title=title,
        url=info.get("webpage_url") or url,
        folder=_folder_name(title, artist),
        artist=artist,
        year=info.get("release_year"),
        track_count=len(entries),
        total_duration=sum(durations) if durations else None,
        track_titles=[entry.get("title") or "?" for entry in entries],
    )


def download(
    target: str,
    target_format: str,
    dest_root: Path,
    album: AlbumInfo | None = None,
    progress_callback: Callable[[Progress], None] | None = None,
) -> list[Track]:
    """Download `target` into `dest_root`, converting to `target_format`.

    With `album` set, every track of that album lands in one dedicated folder.
    Blocking: call it from a worker thread.
    """
    if target_format not in FORMATS:
        raise DownloadError(f"Unsupported format: {target_format}")

    ffmpeg = find_ffmpeg()
    if ffmpeg is None and not transcode.can_convert(target_format):
        raise DownloadError(
            "ffmpeg was not found. Install it or `pip install imageio-ffmpeg`."
        )

    dest_root = Path(dest_root).expanduser()
    if album is not None:
        out_dir = dest_root / album.folder
        # Numbered tracks keep albums sorted the way they were released.
        outtmpl = str(out_dir / "%(playlist_index)02d - %(title)s.%(ext)s")
        extra: dict = {"ignoreerrors": "only_download"}
    else:
        out_dir = dest_root
        outtmpl = str(out_dir / "%(title)s.%(ext)s")
        extra = {}
    out_dir.mkdir(parents=True, exist_ok=True)

    if ffmpeg is not None:
        ydl_opts = _base_opts() | {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "postprocessors": _postprocessors(target_format),
            "progress_hooks": [_make_progress_hook(progress_callback)],
        } | _art_opts(target_format) | extra
    else:
        # Nothing to spawn: yt-dlp saves the stream as it comes and the
        # conversion happens in this process afterwards (`_convert_downloads`),
        # so none of its postprocessors - every one of them an ffmpeg run - are
        # configured. The thumbnail is still worth downloading: the video frame
        # stands in for a cover until a music database supplies one, and a
        # container that cannot hold a picture goes without, exactly as it does
        # on the ffmpeg path.
        ydl_opts = _base_opts() | {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "progress_hooks": [_make_progress_hook(progress_callback)],
            "writethumbnail": target_format in ART_FORMATS,
        } | extra

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.add_post_processor(_TagFixupPP(), when="pre_process")
            info = ydl.extract_info(target, download=True)
    except YtDlpDownloadError as err:
        raise DownloadError(_clean_message(err)) from err

    if ffmpeg is None:
        _convert_downloads(info, target_format, album)

    tracks: list[Track] = []
    for entry in _entries(info):
        track = _track_of(entry, target_format)
        if track is None:
            continue
        _enrich(entry, track, album, progress_callback)
        tracks.append(track)
    if not tracks:
        raise DownloadError(f'No audio file was produced for "{target}".')
    return tracks


def _postprocessors(target_format: str) -> list[dict]:
    """The ffmpeg runs that turn the raw stream into the requested container."""
    steps: list[dict] = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": target_format,
            "preferredquality": FORMATS[target_format],
        },
        # Runs after extraction, so the tags end up on the final container
        # (title, artist, album, genre, date) instead of the raw stream.
        {"key": "FFmpegMetadata", "add_chapters": False, "add_infojson": False},
    ]
    if target_format in ART_FORMATS:
        # YouTube serves the cover as webp, which no audio container accepts, so
        # yt-dlp converts it to PNG and stores the picture in the file itself:
        # players show it with no sidecar image and no network. That conversion
        # is the only ffmpeg run touching the picture, so the 4:3 crop rides on
        # it, and asking for it by name makes it happen even for a source
        # yt-dlp could have embedded as it is. Both run after FFmpegMetadata -
        # its re-mux would otherwise drop the picture.
        steps += [
            {"key": "FFmpegThumbnailsConvertor", "format": "png"},
            {"key": "EmbedThumbnail"},
        ]
    return steps


def _art_opts(target_format: str) -> dict:
    """Download options the cover art needs, empty for containers without one."""
    if target_format not in ART_FORMATS:
        return {}
    return {
        "writethumbnail": True,
        # `postprocessor_args` is keyed "<postprocessor>+<executable>_o":
        # the lookup lowercases the name, and `_o` is what `real_run_ffmpeg`
        # asks for when it builds the converter's output options.
        "postprocessor_args": {
            "thumbnailsconvertor+ffmpeg_o": ["-vf", ART_CROP_FILTER]
        },
    }


def download_worker(
    target: str,
    target_format: str,
    dest_root: Path,
    album: AlbumInfo | None,
    on_success: Callable[[list[Track]], None],
    on_error: Callable[[str], None],
    on_progress: Callable[[Progress], None] | None = None,
) -> None:
    """Thread entry point: run the blocking download and report the outcome."""
    try:
        tracks = download(target, target_format, dest_root, album, on_progress)
    except Exception as err:  # noqa: BLE001 - a worker thread must never die silently
        on_error(_clean_message(err) or err.__class__.__name__)
    else:
        on_success(tracks)


def _entries(info: dict) -> list[dict]:
    """What one download produced: a playlist's entries, or the track itself."""
    if info.get("_type") == "playlist":
        return [entry for entry in (info.get("entries") or []) if entry]
    return [info]


def _convert_downloads(info: dict, target_format: str, album: AlbumInfo | None) -> None:
    """Re-encode what yt-dlp saved without ffmpeg, then label it.

    Whatever the stream was (an opus in webm, an m4a), it is decoded and
    encoded again into the container the user asked for. The tags and the cover
    written here are the video's own - `_enrich` replaces them when a music
    database knows the recording - so a track nothing is known about still
    comes out named and pictured. Each entry keeps pointing at the file that
    now holds the audio, so the caller reads the converted one.
    """
    for entry in _entries(info):
        downloads = entry.get("requested_downloads") or [{}]
        source = Path(downloads[0].get("filepath") or entry.get("filepath") or "")
        if not source.is_file():
            continue
        destination = source.with_suffix(transcode.extension(target_format))
        transcode.convert(source, target_format, destination)
        if destination != source:
            source.unlink(missing_ok=True)
        thumbnail = _thumbnail_of(source)
        cover = transcode.cover_from(thumbnail) if thumbnail is not None else None
        if metadata.write_tags(destination, _video_tags(entry, album), cover) and cover:
            thumbnail.unlink(missing_ok=True)
        downloads[0]["filepath"] = str(destination)
        entry["filepath"] = str(destination)


def _thumbnail_of(source: Path) -> Path | None:
    """The thumbnail yt-dlp wrote next to a download, if there is one."""
    for suffix in (".webp", ".jpg", ".jpeg", ".png"):
        candidate = source.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def _video_tags(entry: dict, album: AlbumInfo | None) -> dict[str, str]:
    """The tags a YouTube upload carries, under the names `metadata` writes."""
    tags: dict[str, str] = {}
    if title := entry.get("track") or entry.get("title"):
        tags["title"] = str(title)
    if artist := _clean_artist(entry.get("artist") or entry.get("uploader")):
        tags["artist"] = artist
    if name := entry.get("album"):
        tags["album"] = str(name)
    if genre := entry.get("genre"):
        tags["genre"] = str(genre)
    if date := _release_date(entry):
        tags["date"] = date
    return tags | _album_tags(entry, album)


def _release_date(entry: dict) -> str | None:
    """Release date - else upload date - as the ISO date a tag holds."""
    for key in ("release_date", "upload_date"):
        value = str(entry.get(key) or "")
        if len(value) == 8 and value.isdigit():
            return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return None


def _base_opts() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ffmpeg_location": find_ffmpeg(),
        "js_runtimes": find_js_runtimes(),
    }


def _ydl(extract_flat: bool | str = False, **overrides):
    return yt_dlp.YoutubeDL(_base_opts() | {"extract_flat": extract_flat} | overrides)


def _flat_entries(url: str) -> list[dict]:
    with _ydl(extract_flat=True) as ydl:
        info = ydl.extract_info(url, download=False)
    return [entry for entry in (info.get("entries") or []) if entry]


def _is_playlist_entry(entry: dict) -> bool:
    url = str(entry.get("url") or "")
    if entry.get("ie_key") != "YoutubeTab":
        return False
    if "/browse/UC" in url:  # artist page, not an album
        return False
    return "/browse/MPREb_" in url or "playlist?list=" in url


def _music_candidates(query: str) -> list[dict]:
    """Search YouTube Music, in its own relevance order."""
    entries = _flat_entries(MUSIC_SEARCH_URL.format(query=quote(query)))
    candidates = [
        entry
        for entry in entries
        if _is_playlist_entry(entry) or entry.get("ie_key") == "Youtube"
    ]
    return candidates[:SEARCH_LIMIT]


def _video_candidates(query: str) -> list[dict]:
    """Plain YouTube fallback for when the music search comes up empty."""
    with _ydl(extract_flat=True, playlistend=SEARCH_LIMIT) as ydl:
        info = ydl.extract_info(f"ytsearch{SEARCH_LIMIT}:{query}", download=False)
    return [entry for entry in (info.get("entries") or []) if entry]


def _resolve_all(entries: list[dict]) -> list[SearchResult]:
    """Resolve every candidate in parallel; one bad entry must not kill the search."""
    if not entries:
        return []
    results: list[SearchResult] = []
    with ThreadPoolExecutor(max_workers=min(RESOLVE_WORKERS, len(entries))) as pool:
        futures = [pool.submit(_resolve_entry, entry) for entry in entries]
        for future in futures:
            try:
                result = future.result()
            except Exception:  # noqa: BLE001 - one bad result must not kill the search
                continue
            if result is not None:
                results.append(result)
    return results


def _rank(results: list[SearchResult]) -> list[SearchResult]:
    """Music uploads and albums first, each group in the order YouTube ranked it.

    YouTube Music ranks by popularity, so the top hit for a song is often a
    re-upload by an unrelated channel. What is worth downloading is the release
    - the upload that carries music metadata - or the album, never the video.
    """
    return sorted(results, key=lambda result: not (result.music or result.kind == "album"))


def _resolve_entry(entry: dict) -> SearchResult | None:
    """Fill in the metadata the flat search result does not carry."""
    url = str(entry.get("url") or "")
    if not url:
        return None

    with _ydl(extract_flat="in_playlist") as ydl:
        info = ydl.extract_info(url, download=False)

    entries = [item for item in (info.get("entries") or []) if item]
    if entries:  # album or playlist
        title = _strip_title_prefix(info.get("title") or entry.get("title") or "Album")
        artist = _clean_artist(info.get("channel") or info.get("uploader"))
        return SearchResult(
            kind="album",
            title=title,
            url=info.get("webpage_url") or url,
            artist=artist,
            track_count=len(entries),
            music=_is_music(info),
        )

    title = info.get("title") or entry.get("title")
    if not title:
        return None
    return SearchResult(
        kind="track",
        title=title,
        url=info.get("webpage_url") or url,
        artist=_clean_artist(info.get("artist") or info.get("uploader")),
        duration=info.get("duration"),
        album=info.get("album"),
        music=_is_music(info),
    )


def _inspect_url(url: str) -> SearchResult:
    """Turn a pasted link into a candidate: album/playlist, or a single track."""
    result = _resolve_entry({"url": url})
    if result is None:
        raise DownloadError(f"Nothing to download at {url}")
    return result


def _track_of(entry: dict, target_format: str) -> Track | None:
    downloads = entry.get("requested_downloads") or [{}]
    filepath = downloads[0].get("filepath") or entry.get("filepath")
    if not filepath:
        return None
    path = Path(filepath)
    return Track(
        title=entry.get("title") or path.stem,
        path=path,
        format=target_format,
    )


def _enrich(
    entry: dict,
    track: Track,
    album: AlbumInfo | None,
    progress_callback: Callable[[Progress], None] | None,
) -> None:
    """Replace the video-derived tags with real music metadata, when there is any.

    Best effort by design: the file on disk is already correct without this, so
    a database that is down or does not know the track leaves the tags yt-dlp
    wrote in place, and the download still succeeds.
    """
    if track.format not in ART_FORMATS:  # no picture block to put a cover in
        return
    if progress_callback is not None:
        progress_callback(
            Progress(
                stage="tagging",
                percent=None,
                downloaded_bytes=0,
                total_bytes=None,
                speed=None,
                eta=None,
                title=entry.get("title"),
                track_index=entry.get("playlist_index"),
                track_count=entry.get("playlist_count"),
            )
        )
    match = metadata.lookup(
        title=str(entry.get("track") or entry.get("title") or track.path.stem),
        artist=entry.get("artist") or entry.get("uploader"),
        duration=entry.get("duration"),
    )
    if match is not None:
        metadata.apply(track.path, match, keep=_album_tags(entry, album))


def _album_tags(entry: dict, album: AlbumInfo | None) -> dict[str, str]:
    """The tags the download owns: the album the user picked and its ordering.

    A database match can land on a different release of the same recording, so
    for an album download the release the user chose keeps its name, its artist
    and the position the track has in it.
    """
    if album is None:
        return {}
    tags = {"album": album.title}
    if album.artist:
        tags["album_artist"] = album.artist
    if album.year:
        tags["date"] = str(album.year)
    if index := entry.get("playlist_index"):
        tags["track"] = str(index)
    return tags


def _is_music(info: dict) -> bool:
    """True when the source is a music upload rather than a video.

    yt-dlp fills in an artist, an album and a release date only for one of
    those - it parses the credits block a label's upload carries - so their
    absence is what a plain re-upload looks like.
    """
    return bool(info.get("artist") or info.get("album") or info.get("release_year"))


def _make_progress_hook(
    progress_callback: Callable[[Progress], None] | None,
) -> Callable[[dict], None]:
    """Translate yt-dlp progress dicts into `Progress` snapshots."""

    def hook(status: dict) -> None:
        if progress_callback is None:
            return
        info = status.get("info_dict") or {}
        shared = {
            "title": info.get("title"),
            "track_index": info.get("playlist_index"),
            "track_count": info.get("playlist_count"),
        }
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate")
            downloaded = status.get("downloaded_bytes") or 0
            progress_callback(
                Progress(
                    stage="downloading",
                    percent=downloaded * 100 / total if total else None,
                    downloaded_bytes=downloaded,
                    total_bytes=total,
                    speed=status.get("speed"),
                    eta=status.get("eta"),
                    **shared,
                )
            )
        elif status.get("status") == "finished":
            # The stream is on disk; ffmpeg still has to extract and re-encode it.
            progress_callback(
                Progress(
                    stage="converting",
                    percent=None,
                    downloaded_bytes=status.get("total_bytes") or 0,
                    total_bytes=status.get("total_bytes"),
                    speed=None,
                    eta=None,
                    **shared,
                )
            )

    return hook


def _looks_like_url(query: str) -> bool:
    return query.lower().startswith(("http://", "https://"))


def _strip_title_prefix(title: str) -> str:
    for prefix in _TITLE_PREFIXES:
        if title.startswith(prefix):
            return title[len(prefix) :].strip()
    return title.strip()


def _clean_artist(artist: str | None) -> str | None:
    if not artist:
        return None
    # YouTube auto-generates "<Artist> - Topic" channels for music uploads.
    return artist.removesuffix(" - Topic").strip() or None


def _folder_name(title: str, artist: str | None) -> str:
    """Cross-platform safe folder name for one album."""
    name = f"{artist} - {title}" if artist else title
    return _sanitize_path(name)


def _sanitize_path(name: str) -> str:
    cleaned = "".join("_" if char in _ILLEGAL_PATH_CHARS or ord(char) < 32 else char for char in name)
    cleaned = " ".join(cleaned.split()).strip(" .")
    cleaned = cleaned[:_MAX_FOLDER_LENGTH].strip(" .")
    return cleaned or "Album"


def _clean_message(error: Exception) -> str:
    message = str(error).strip()
    for prefix in ("ERROR: ", "error: "):
        if message.startswith(prefix):
            message = message[len(prefix) :]
    return message
