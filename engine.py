"""yt-dlp engine: search YouTube Music, inspect albums, download tracks or whole albums.

Everything here is blocking and thread-safe: the Flet UI runs it through
`asyncio.to_thread` and renders progress from the callback.
"""

from __future__ import annotations

import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal
from urllib.parse import quote

import yt_dlp
from yt_dlp.utils import DownloadError as YtDlpDownloadError

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

# yt-dlp enables only deno by default. YouTube needs a JS runtime to decipher
# stream URLs - without one, extraction is deprecated and downloads fail with
# HTTP 403 - so opt in to every runtime we can find on PATH.
JS_RUNTIMES = ("deno", "node", "bun", "quickjs")
# Deno binary packaged into the executable by build.py, used when the machine
# has no JavaScript runtime of its own.
BUNDLED_RUNTIME_PATHS = ("jsrt/deno.exe", "jsrt/deno")

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

    stage: Literal["downloading", "converting"]
    percent: float | None
    downloaded_bytes: int
    total_bytes: int | None
    speed: float | None  # bytes/second
    eta: int | None  # seconds
    title: str | None = None
    track_index: int | None = None  # 1-based position inside an album
    track_count: int | None = None


def find_ffmpeg() -> str | None:
    """Return an ffmpeg binary: the system one first, then the bundled fallback."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
    except ImportError:
        return None
    return imageio_ffmpeg.get_ffmpeg_exe()


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
    """Locate the runtime packaged next to a frozen app, if there is one."""
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return None
    for relative in BUNDLED_RUNTIME_PATHS:
        candidate = Path(base) / relative
        if candidate.is_file():
            return candidate
    return None


def search(query: str, limit: int = SEARCH_LIMIT) -> list[SearchResult]:
    """Find tracks and albums for `query`, or inspect `query` when it is a URL."""
    query = query.strip()
    if not query:
        raise DownloadError("Enter a song, an album or a link.")

    if _looks_like_url(query):
        return [_inspect_url(query)]

    entries = _flat_entries(MUSIC_SEARCH_URL.format(query=quote(query)))
    if not entries:
        return _video_search(query, limit)  # YouTube Music had nothing: fall back

    candidates: list[tuple[int, dict]] = []
    for position, entry in enumerate(entries):
        if len(candidates) >= limit:
            break
        if _is_playlist_entry(entry) or entry.get("ie_key") == "Youtube":
            candidates.append((position, entry))
    if not candidates:
        return _video_search(query, limit)

    results: list[SearchResult | None] = [None] * len(candidates)
    with ThreadPoolExecutor(max_workers=min(RESOLVE_WORKERS, len(candidates))) as pool:
        futures = [pool.submit(_resolve_entry, entry) for _, entry in candidates]
        for index, future in enumerate(futures):
            try:
                results[index] = future.result()
            except Exception:  # noqa: BLE001 - one bad result must not kill the search
                results[index] = None

    found = [result for result in results if result is not None]
    return found or _video_search(query, limit)


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
    if ffmpeg is None:
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

    quality = FORMATS[target_format]
    with_art = target_format in ART_FORMATS
    postprocessors: list[dict] = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": target_format,
            "preferredquality": quality,
        },
        # Runs after extraction, so the tags end up on the final container
        # (title, artist, album, genre, date) instead of the raw stream.
        {"key": "FFmpegMetadata", "add_chapters": False, "add_infojson": False},
    ]
    if with_art:
        # YouTube serves the cover as webp, which no audio container accepts, so
        # yt-dlp converts it to PNG and stores the picture in the file itself:
        # players show it with no sidecar image and no network. Deliberately
        # after FFmpegMetadata - its re-mux would otherwise drop the picture.
        postprocessors.append({"key": "EmbedThumbnail"})
    ydl_opts = _base_opts() | {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "writethumbnail": with_art,
        "postprocessors": postprocessors,
        "progress_hooks": [_make_progress_hook(progress_callback)],
    } | extra

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.add_post_processor(_TagFixupPP(), when="pre_process")
            info = ydl.extract_info(target, download=True)
    except YtDlpDownloadError as err:
        raise DownloadError(_clean_message(err)) from err

    entries = (
        [entry for entry in (info.get("entries") or []) if entry]
        if info.get("_type") == "playlist"
        else [info]
    )
    tracks = [_track_of(entry, target_format) for entry in entries]
    tracks = [track for track in tracks if track is not None]
    if not tracks:
        raise DownloadError(f'No audio file was produced for "{target}".')
    return tracks


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
    )


def _inspect_url(url: str) -> SearchResult:
    """Turn a pasted link into a candidate: album/playlist, or a single track."""
    result = _resolve_entry({"url": url})
    if result is None:
        raise DownloadError(f"Nothing to download at {url}")
    return result


def _video_search(query: str, limit: int) -> list[SearchResult]:
    """Plain YouTube fallback for when the music search comes up empty."""
    with _ydl(extract_flat=True, playlistend=limit) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    results = []
    for entry in info.get("entries") or []:
        if not entry or not entry.get("title"):
            continue
        results.append(
            SearchResult(
                kind="track",
                title=entry["title"],
                url=entry.get("url") or entry.get("webpage_url") or "",
                artist=_clean_artist(entry.get("uploader") or entry.get("channel")),
                duration=entry.get("duration"),
            )
        )
    return results


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
