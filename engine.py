"""yt-dlp engine: search YouTube Music, inspect albums, download tracks or whole albums.

Everything here is blocking and thread-safe: the Flet UI runs it through
`asyncio.to_thread` and renders progress from the callback. YouTube supplies
the audio; the tags and the cover come from a music database (see `metadata`),
because a video's channel, category and thumbnail are not release metadata.
"""

from __future__ import annotations

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Literal, Sequence
from urllib.parse import quote

import yt_dlp
from yt_dlp.utils import DownloadError as YtDlpDownloadError

import bundle
import metadata
import settings
import spotify
import transcode

MUSIC_SEARCH_URL = "https://music.youtube.com/search?q={query}"
SEARCH_LIMIT = 10
RESOLVE_WORKERS = 8
# YouTube Music's own "Songs" filter, the `sp` its interface puts on the URL: a
# protobuf written down as base64. Matching a playlist wants the catalogue of
# releases, not the whole page - the general results for a song with a fandom
# behind it are its lyrics videos, its animatics and its covers - and asking
# for songs is how the same search that finds none of them finds it first. If
# Spotify's catalogue were to move under it, the unfiltered search is still
# there (see `_candidate_sources`).
SONGS_FILTER = "EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"
# Uploads resolved for one track before it is given up on. One is the usual
# case - the first candidate is the release - and the rest are for a catalogue
# where it is buried under covers.
MATCH_LOOKUPS = 6
# Playlist matching resolves an upload per track, and YouTube starts refusing a
# burst of those ("Sign in to confirm you're not a bot"). Fewer workers than a
# search page uses, a stagger between the requests and a second try for what was
# refused is what keeps a long playlist from being cut in half.
MATCH_WORKERS = 4
MATCH_STAGGER = 0.4  # seconds between the matching requests
MATCH_RETRY_PAUSE = 3.0  # seconds between the retries of one pass
MATCH_RETRY_GIVE_UP = 3  # refusals in a row that end the retry pass
# The one failure a retry can fix, and the one it cannot: a lookup YouTube
# refused says nothing about whether the track exists, a miss does.
REFUSED = "YouTube refused the request (rate limit or sign-in check)"
NO_MATCH = "no match on YouTube"
# Not a failure: the file is already where the download would put it (a run
# that was cut short and is being finished), so it is not remembered as missing.
ALREADY_THERE = "already in the folder"

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
# Name each runtime's binary may have on PATH; quickjs-ng installs `qjs`, which
# is what yt-dlp looks for inside the directory it is handed.
RUNTIME_BINARIES = {"quickjs": ("quickjs", "qjs")}
# The runtime packaged next to the app by build.py (quickjs-ng, 2.5 MB, where
# yt-dlp's default Deno is 96 MB), used when the machine has none of its own.
BUNDLED_RUNTIME_PATHS = ("jsrt/qjs.exe", "jsrt/qjs")
# An ffmpeg the bundle carries itself, looked up the same way. Desktop builds
# get theirs from imageio-ffmpeg; a platform whose wheels have none (Android)
# has to ship a binary, and this is where it is expected to sit.
BUNDLED_FFMPEG_PATHS = ("ffmpeg", "ffmpeg.exe")

# The browser whose cookies yt-dlp may send, when the machine says so (see
# `_cookie_opts`): the way past a YouTube sign-in wall that also hits `android`.
COOKIES_ENV = "CADENZA_COOKIES_FROM_BROWSER"
_COOKIES: dict | None = None

# YouTube Music titles come prefixed with the result kind ("Album - Foo").
_TITLE_PREFIXES = ("Album - ", "Playlist - ", "Mix - ")
_ILLEGAL_PATH_CHARS = '<>:"/\\|?*'
_MAX_FOLDER_LENGTH = 120

# A linked Spotify playlist is a list of queries, not a list of URLs: nothing in
# it can be handed to yt-dlp (see `spotify`), so every track is matched on
# YouTube Music first and what is downloaded is that upload. This is the
# `source` a result, an album and the UI carry for one.
SPOTIFY = "spotify"


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
    # Where the candidate came from, when it is not YouTube: `SPOTIFY` for a
    # linked playlist or track, whose audio comes from YouTube on download.
    source: str = ""


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
    source: str = ""
    # The source carries more tracks than it published (`SPOTIFY`): what is
    # here is all it gave, and the dialog says so before the download starts.
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Track:
    """A finished download."""

    title: str
    path: Path
    format: str


@dataclass(frozen=True, slots=True)
class Progress:
    """Snapshot of an in-flight download, handed to the UI from a worker thread."""

    stage: Literal["matching", "downloading", "converting", "tagging", "skipped"]
    percent: float | None
    downloaded_bytes: int
    total_bytes: int | None
    speed: float | None  # bytes/second
    eta: int | None  # seconds
    title: str | None = None
    track_index: int | None = None  # 1-based position inside an album
    track_count: int | None = None
    # Why a track will not be downloaded, for the `skipped` stage: a reason the
    # UI shows next to the count, because a playlist that came out short has to
    # say which tracks are missing and why.
    note: str = ""


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
    runtimes: dict[str, dict[str, str]] = {}
    for name in JS_RUNTIMES:
        for binary in RUNTIME_BINARIES.get(name, (name,)):
            if (path := shutil.which(binary)) is not None:
                runtimes[name] = {"path": path}
                break
    if runtimes:
        return runtimes
    if (bundled := _bundled_runtime()) is not None:
        return {"quickjs": {"path": str(bundled)}}
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


def _spotify_playlist(url: str) -> spotify.Playlist:
    """Read one linked playlist, as one error type for the UI to show."""
    try:
        return spotify.playlist(url)
    except spotify.SpotifyError as err:
        raise DownloadError(str(err)) from err


def _spotify_result(url: str) -> SearchResult:
    """One linked playlist, album or track, as the results list shows it."""
    playlist = _spotify_playlist(url)
    single = len(playlist.tracks) == 1
    track = playlist.tracks[0]
    return SearchResult(
        kind="track" if single else "album",
        title=playlist.name,
        url=url,
        artist=playlist.owner,
        duration=int(track.duration) if single and track.duration else None,
        track_count=None if single else len(playlist.tracks),
        source=SPOTIFY,
    )


def _spotify_album(url: str) -> AlbumInfo:
    """What the confirmation dialog shows for a Spotify link."""
    playlist = _spotify_playlist(url)
    durations = [track.duration for track in playlist.tracks if track.duration]
    return AlbumInfo(
        title=playlist.name,
        url=url,
        folder=_folder_name(playlist.name, playlist.owner),
        artist=playlist.owner,
        track_count=len(playlist.tracks),
        total_duration=int(sum(durations)) if durations else None,
        track_titles=[track.title for track in playlist.tracks],
        source=SPOTIFY,
        truncated=playlist.truncated,
    )


def probe_album(url: str) -> AlbumInfo:
    """Read an album's, playlist's or Spotify link's track list, without downloading it."""
    if spotify.handles(url):
        return _spotify_album(url)
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


def _require_converter(target_format: str) -> None:
    """Refuse a format this machine cannot produce, before anything is fetched."""
    if target_format not in FORMATS:
        raise DownloadError(f"Unsupported format: {target_format}")
    if find_ffmpeg() is None and not transcode.can_convert(target_format):
        raise DownloadError(
            "ffmpeg was not found. Install it or `pip install imageio-ffmpeg`."
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
    A Spotify link is not a URL yt-dlp can take, so it is matched track by track
    instead (`_download_spotify`).
    Blocking: call it from a worker thread.
    """
    if spotify.handles(target):
        return _download_spotify(
            target, target_format, dest_root, album, progress_callback
        )

    _require_converter(target_format)
    ffmpeg = find_ffmpeg()

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

    ydl_opts = _download_opts(target_format, outtmpl, progress_callback, extra, ffmpeg)

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


def _download_spotify(
    target: str,
    target_format: str,
    dest_root: Path,
    album: AlbumInfo | None,
    progress_callback: Callable[[Progress], None] | None,
) -> list[Track]:
    """Download a Spotify playlist by matching each of its tracks on YouTube.

    Spotify serves no audio to a caller without a login - the embed carries
    titles, artists and lengths - so what lands on disk is the YouTube Music
    upload that matches. Every track is fetched on its own, in playlist order,
    and one that cannot be matched or downloaded is left out instead of failing
    the rest: the UI is told which ones and why (`_report_skip`), because a
    playlist that came out short has to account for it. A track whose file is
    already in the folder is left alone - no lookup, no download - so a playlist
    that was cut short can simply be run again.
    """
    _require_converter(target_format)
    ffmpeg = find_ffmpeg()
    playlist = _spotify_playlist(target)

    dest_root = Path(dest_root).expanduser()
    numbered = album is not None
    out_dir = dest_root / album.folder if album is not None else dest_root
    out_dir.mkdir(parents=True, exist_ok=True)

    total = len(playlist.tracks)
    pending: list[tuple[int, spotify.Track]] = []
    for index, entry in enumerate(playlist.tracks, start=1):
        if _destination(out_dir, index, entry.title, numbered, target_format).is_file():
            _report_skip(progress_callback, entry, index, total, ALREADY_THERE)
            continue
        pending.append((index, entry))

    urls, reasons = (
        _match_tracks(pending, total, progress_callback) if pending else ([], [])
    )
    tracks: list[Track] = []
    failure = f"No track of {target} could be downloaded"
    for (index, entry), url, reason in zip(pending, urls, reasons):
        if url is None:
            _report_skip(progress_callback, entry, index, total, reason)
            failure = f'Nothing matched "{entry.title}" on YouTube'
            continue
        callback = _numbered(progress_callback, index, total, entry.title)
        opts = _download_opts(
            target_format,
            _outtmpl(out_dir, index, entry.title, numbered),
            callback,
            {},
            ffmpeg,
        )
        try:
            info = _extract(url, opts, ffmpeg, target_format, album, index, total)
        except YtDlpDownloadError as err:
            failure = f'"{entry.title}": {_clean_message(err)}'
            _report_skip(progress_callback, entry, index, total, _clean_message(err))
            _discard(out_dir, index, entry.title, numbered)
            continue
        track = _track_of(info, target_format)
        if track is None:
            _report_skip(
                progress_callback, entry, index, total, "no audio file was produced"
            )
            continue
        _enrich(info, track, album, callback)
        tracks.append(track)

    if pending and not tracks:
        raise DownloadError(failure)
    return tracks


def _extract(
    url: str,
    opts: dict,
    ffmpeg: str | None,
    target_format: str,
    album: AlbumInfo | None,
    index: int,
    total: int,
) -> dict:
    """One track of a playlist: fetch it, then treat it as a numbered entry.

    The position is written onto the info dict the way a playlist URL carries
    it, so the tags - and `_album_tags`, which reads it - see what they see for
    an album YouTube itself provided.
    """
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.add_post_processor(_TagFixupPP(), when="pre_process")
        info = ydl.extract_info(url, download=True)
    info["playlist_index"] = index
    info["playlist_count"] = total
    if ffmpeg is None:
        _convert_downloads(info, target_format, album)
    return info


def _match_tracks(
    tracks: Sequence[tuple[int, spotify.Track]],
    total: int,
    progress_callback: Callable[[Progress], None] | None,
) -> tuple[list[str | None], list[str]]:
    """The YouTube Music upload for every track, and why the rest has none.

    One search per track - `tracks` holds each one with its position in the
    playlist, `total` is how long that playlist is, which is what the UI counts
    against - looked up in parallel but at a walking pace, because YouTube
    starts refusing a burst of them. A refusal says nothing about the track, so
    the ones it hit are tried once more; a pass that keeps being refused is left
    alone instead of hammered, and what is still missing is reported rather than
    quietly dropped.
    """
    urls: list[str | None] = [None] * len(tracks)
    reasons = [""] * len(tracks)

    def match(position: int) -> None:
        index, track = tracks[position]
        _report_matching(progress_callback, track, index, total)
        urls[position] = _match_track(track)

    refused: list[int] = []
    with ThreadPoolExecutor(max_workers=min(MATCH_WORKERS, len(tracks))) as pool:
        futures = {}
        for position in range(len(tracks)):
            futures[pool.submit(match, position)] = position
            # The stagger is the pacing: submitting as fast as the pool takes
            # them is what makes YouTube ask for a sign-in half way through.
            time.sleep(MATCH_STAGGER)
        for future, position in futures.items():
            try:
                future.result()
            except Exception as err:  # noqa: BLE001 - one track must not stop the rest
                refused.append(position)
                reasons[position] = _lookup_reason(err)

    for position, reason in enumerate(reasons):
        if urls[position] is None and not reason:
            reasons[position] = NO_MATCH

    streak = 0
    for position in refused:
        if streak >= MATCH_RETRY_GIVE_UP:
            break  # YouTube is refusing everything: asking again only makes it worse
        time.sleep(MATCH_RETRY_PAUSE)
        index, track = tracks[position]
        _report_matching(progress_callback, track, index, total)
        try:
            urls[position] = _match_track(track)
        except Exception as err:  # noqa: BLE001
            streak += 1
            reasons[position] = _lookup_reason(err)
            continue
        streak = 0
        reasons[position] = "" if urls[position] else NO_MATCH

    if not any(urls):
        raise DownloadError(
            f"{REFUSED}: try again in a few minutes"
            if all(reason == REFUSED for reason in reasons)
            else "No track of this playlist could be matched on YouTube"
        )
    return urls, reasons


def _report_matching(
    progress_callback: Callable[[Progress], None] | None,
    track: spotify.Track,
    index: int,
    total: int,
) -> None:
    """Tell the UI which track is being looked up, and where it sits."""
    if progress_callback is None:
        return
    progress_callback(
        Progress(
            stage="matching",
            percent=None,
            downloaded_bytes=0,
            total_bytes=None,
            speed=None,
            eta=None,
            title=track.title,
            track_index=index,
            track_count=total,
        )
    )


def _report_skip(
    progress_callback: Callable[[Progress], None] | None,
    track: spotify.Track,
    index: int,
    total: int,
    note: str,
) -> None:
    """Tell the UI about a track that will not be downloaded, and why."""
    if progress_callback is None:
        return
    progress_callback(
        Progress(
            stage="skipped",
            percent=None,
            downloaded_bytes=0,
            total_bytes=None,
            speed=None,
            eta=None,
            title=track.title,
            track_index=index,
            track_count=total,
            note=note,
        )
    )


def _lookup_reason(error: Exception) -> str:
    """Why a lookup failed, as the UI will show it.

    YouTube asking for a sign-in is the one worth naming: it is a rate limit on
    this machine rather than anything about the track, and the same playlist
    works again later.
    """
    message = _clean_message(error)
    if "Sign in to confirm" in message or "not a bot" in message:
        return REFUSED
    return message or error.__class__.__name__


def _match_track(track: spotify.Track) -> str | None:
    """The upload that is this track, or None when nothing close enough exists.

    A search hit carries nothing but a title, so candidates are resolved - the
    song catalogue first, then the whole of YouTube Music, then plain YouTube -
    until one turns out to be this recording: the artist the playlist names,
    under the title it names, at a length it cannot argue with (`_recording_key`).
    Taking the first hit whose length fits is what downloads a cover, and a
    catalogue with a fandom behind it has those in front of the release.
    """
    query = " ".join(part for part in (track.title, track.artist) if part)
    best: SearchResult | None = None
    best_key: tuple[int, int, int, int, float] | None = None
    lookups = 0
    for entries in _candidate_sources(query):
        for candidate in _ranked(entries, track):
            if lookups == MATCH_LOOKUPS:
                return best.url if best is not None else None
            lookups += 1
            resolved = _resolve_entry(candidate)
            if resolved is None:
                continue
            key = _recording_key(resolved, track)
            if key is None:
                continue
            if best_key is None or key > best_key:
                best, best_key = resolved, key
                if key[0] > 0 and key[2] == 3:
                    # The playlist's own artist, under the title it names: there
                    # is nothing further down that could be a better answer.
                    return best.url
        if best_key is not None and best_key[0] > 0:
            break  # right artist: no reason to go looking in the next catalogue
    return best.url if best is not None else None


def _candidate_sources(query: str):
    """Search hits for one track, in the order worth trying them.

    Lazy on purpose: the second search is never made when the release was the
    first hit of the first one, which is the usual case.
    """
    yield _music_candidates(query, songs_only=True)
    yield _music_candidates(query)
    yield _video_candidates(query)


def _ranked(candidates: list[dict], track: spotify.Track) -> list[dict]:
    """The candidates whose title matches, best first, YouTube breaking the ties."""
    scored = [
        (rank, entry)
        for entry in candidates
        if (rank := _title_rank(entry, track))
    ]
    scored.sort(key=lambda pair: -pair[0])
    return [entry for _, entry in scored]


def _title_rank(candidate: dict, track: spotify.Track) -> int:
    """How well a candidate's title matches, in `metadata.title_score`'s terms."""
    return metadata.title_score(
        metadata.normalize(track.title),
        metadata.normalize(str(candidate.get("title") or "")),
    )


def _recording_key(
    resolved: SearchResult, track: spotify.Track
) -> tuple[int, int, int, int, float] | None:
    """Rank a resolved upload against one playlist track; None when it is not it.

    The artist comes first, and the artist the playlist names before the ones
    it features - because that is what tells a release from the many recordings
    that copy it. A label's upload of a song carries its artist, its album and
    its release date, which `SearchResult.music` reports; a cover, an animatic
    or a re-upload carries a channel name and nothing else, and its channel
    name is not the artist. The length is the last word, the way it is for the
    database lookup: a difference no re-upload explains is another recording.
    """
    if resolved.kind != "track":
        return None
    rank = metadata.title_score(
        metadata.normalize(track.title), metadata.normalize(resolved.title)
    )
    if not rank:
        return None
    delta = 0.0
    if track.duration and resolved.duration:
        delta = abs(track.duration - resolved.duration)
        if delta > metadata.DURATION_TOLERANCE:
            return None
    return (
        metadata.artist_score(_lead_artist(resolved.artist), _lead_artist(track.artist)),
        metadata.artist_score(resolved.artist, track.artist or ""),
        rank,
        int(resolved.music),
        -delta,
    )


def _lead_artist(artist: str | None) -> str | None:
    """The first name of a credit list: "A, B & C" is A's recording.

    Spotify and YouTube both list the main artist first, and comparing the
    whole lists would count words like "music" or "cast" as a match.
    """
    if not artist:
        return None
    return artist.split(",")[0].strip() or None


def _stem(index: int, title: str, numbered: bool) -> str:
    """What one track's file is called: the playlist's own name for it.

    The YouTube upload's title is not what the user picked - a playlist link
    names its tracks - so the file is named from the playlist, numbered when it
    belongs to an album folder.
    """
    name = _sanitize_path(title)
    return f"{index:02d} - {name}" if numbered else name


def _outtmpl(out_dir: Path, index: int, title: str, numbered: bool) -> str:
    """Output template for one track of a playlist, inside its folder.

    `%` starts a field in yt-dlp's template, so a title carrying one is escaped,
    and the folder is part of the template: a bare name would land in the
    process's working directory instead.
    """
    return str(out_dir / f"{_stem(index, title, numbered).replace('%', '%%')}.%(ext)s")


def _destination(
    out_dir: Path, index: int, title: str, numbered: bool, target_format: str
) -> Path:
    """The file one track ends up as - what a second run of the playlist finds."""
    return out_dir / f"{_stem(index, title, numbered)}{transcode.extension(target_format)}"


def _discard(out_dir: Path, index: int, title: str, numbered: bool) -> None:
    """Remove what a track that failed left behind: its thumbnail, its part file.

    yt-dlp writes the picture before the audio, so a download that ends in an
    error leaves a cover for a file that does not exist - which is litter in a
    folder the user is meant to read as their music.
    """
    prefix = f"{_stem(index, title, numbered)}."
    for leftover in out_dir.iterdir():
        if leftover.name.startswith(prefix):
            leftover.unlink(missing_ok=True)


def _numbered(
    progress_callback: Callable[[Progress], None] | None,
    index: int,
    total: int,
    title: str,
) -> Callable[[Progress], None] | None:
    """The download callback for one track of a playlist.

    yt-dlp reports a single video's progress: it does not know where that video
    sits in the playlist, and it names the upload rather than the track. Both
    come from here, so the UI counts and names what the playlist holds.
    """
    if progress_callback is None:
        return None
    return lambda progress: progress_callback(
        replace(progress, title=title, track_index=index, track_count=total)
    )


def _download_opts(
    target_format: str,
    outtmpl: str,
    progress_callback: Callable[[Progress], None] | None,
    extra: dict,
    ffmpeg: str | None,
) -> dict:
    """yt-dlp options for one download, with or without an ffmpeg to run."""
    if ffmpeg is not None:
        return _base_opts() | {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "postprocessors": _postprocessors(target_format),
            "progress_hooks": [_make_progress_hook(progress_callback)],
        } | _art_opts(target_format) | extra
    # Nothing to spawn: yt-dlp saves the stream as it comes and the conversion
    # happens in this process afterwards (`_convert_downloads`), so none of its
    # postprocessors - every one of them an ffmpeg run - are configured. The
    # thumbnail is still worth downloading: the video frame stands in for a
    # cover until a music database supplies one, and a container that cannot
    # hold a picture goes without, exactly as it does on the ffmpeg path.
    return _base_opts() | {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "progress_hooks": [_make_progress_hook(progress_callback)],
        "writethumbnail": target_format in ART_FORMATS,
    } | extra


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
        # YouTube challenges its web clients long before its app ones. The
        # default set is asked first, so nothing changes while it answers, and
        # `android` stands behind it for the moment YouTube asks this address
        # to sign in - measured: the default refused a request that android
        # served, media and all. Not a preference: a fallback.
        "extractor_args": {"youtube": {"player_client": ["default", "android"]}},
    } | _cookie_opts()


def _cookie_opts() -> dict:
    """yt-dlp's `cookiesfrombrowser`, when this machine asked for one.

    A browser signed in to YouTube is what gets past a wall the app's own
    requests hit - that is the escape hatch yt-dlp's error message points at -
    but the app never reads a browser's cookie store by itself: it does when
    `CADENZA_COOKIES_FROM_BROWSER` names the browser (`firefox`, `chrome`,
    `chromium`, `brave`, `edge`, `opera`, `vivaldi`, `safari`) or when
    `cookies_from_browser` says so in the settings file. Read once, because
    every yt-dlp call asks for these options.
    """
    global _COOKIES
    if _COOKIES is None:
        browser = (
            os.environ.get(COOKIES_ENV, "").strip()
            or (settings.Settings.load().cookies_from_browser or "")
        )
        _COOKIES = {"cookiesfrombrowser": (browser,)} if browser else {}
    return _COOKIES


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


def _music_candidates(query: str, songs_only: bool = False) -> list[dict]:
    """Search YouTube Music, in its own relevance order.

    `songs_only` asks for the song catalogue instead of the whole page, which is
    what matching a playlist track wants: the release, not its lyrics video.
    """
    url = MUSIC_SEARCH_URL.format(query=quote(query))
    if songs_only:
        url += f"&sp={SONGS_FILTER}"
    entries = _flat_entries(url)
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
    if spotify.handles(url):
        return _spotify_result(url)
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
