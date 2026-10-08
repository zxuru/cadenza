"""yt-dlp engine: search YouTube Music, inspect albums, download tracks or whole albums.

Everything here is blocking and thread-safe: the Flet UI runs it through
`asyncio.to_thread` and renders progress from the callback. YouTube supplies
the audio; the tags and the cover come from a music database (see `metadata`),
because a video's channel, category and thumbnail are not release metadata.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable, Literal, NamedTuple, Sequence
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
REFUSED = "YouTube refused the request (rate limit or sign-in check)"
NO_MATCH = "no match on YouTube"
# A lookup YouTube refused because this address asked too often, in any of the
# shapes yt-dlp reports it: the request itself went through, what came back is
# a refusal - 403, "Forbidden", or the sign-in wall - rather than a miss.
RATE_LIMITED = (
    "HTTP Error 403",
    "403",
    "Forbidden",
    "Sign in to confirm",
    "not a bot",
)
#: Seconds before a refused run may be asked for again, counted down in the UI.
RETRY_AFTER = 90
# Not a failure: the file is already where the download would put it (a run
# that was cut short and is being finished), so it is not remembered as missing.
ALREADY_THERE = "already in the folder"

# Why a track is left out: what was produced is not the whole track (see
# `_short_of`). The two lengths ride along with it, so the file that was
# rejected can be told apart from the source it was supposed to hold.
INCOMPLETE = "incomplete file"
# How much shorter than its own source a produced file may be without being
# incomplete, as a band rather than a number: two seconds are nothing on a
# seven-minute track and a ninth of a twenty-second intro, so the allowance
# grows with the track (`_produce_tolerance`) between what a length counted in
# whole seconds can be off by and the cap it is held to.
SHORT_TOLERANCE = 2.0  # the cap
MIN_SHORT_TOLERANCE = 1.0  # the floor: YouTube counts its seconds whole
SHORT_TOLERANCE_SHARE = 0.02  # two percent of the track
# Seconds one playback check may spend on a file before it gives up on it.
DECODE_TIMEOUT = 120
# Warnings one yt-dlp call keeps around to show with a failure (`_YtDlpLog`).
KEPT_WARNINGS = 10
# How far an *existing* file may be from the length Spotify gives the track
# before it is fetched again: everything the matcher accepts a file for, added
# to the most a produced file may be short by (the cap - a file allowed a
# shorter allowance is a fortiori inside this one). Any file that was measured
# when it was produced cannot be further from Spotify's length than this, so
# this check never deletes and fetches again what the check at produce time
# already took as whole - which is what a tighter number does to it, on every
# run, for as long as the two catalogues keep disagreeing by what they are
# given.
EXISTING_TOLERANCE = metadata.DURATION_TOLERANCE + SHORT_TOLERANCE

# ffmpeg describing the stream it opened ("Audio: flac, 48000 Hz, ..."), the
# count `astats` gives once the whole file has been played through, and the
# silences `silencedetect` marks on the way.
_AUDIO_RATE = re.compile(r"Audio: [^,\n]+, (\d+) Hz")
_AUDIO_SAMPLES = re.compile(r"Number of samples: (\d+)")
_SILENCE = re.compile(r"silence_(start|end): (-?[\d.]+)")

# Silence inside a track that is worth saying out loud: at least this long,
# this far from both ends - an intro and an outro are silence by design - and
# at a level music never sits at, so a quiet passage is not mistaken for a
# hole. What this finds is a track a player goes quiet in the middle of; a
# track with audio *missing* never gets here, because that shows up as length
# first (`_short_of`).
GAP_NOISE = "-70dB"
GAP_SECONDS = 0.08
GAP_EDGE = 2.0
# Both answers from one play: the length and the quiet cost the same decode.
PLAY_FILTER = f"astats,silencedetect=noise={GAP_NOISE}:d={GAP_SECONDS}"

# What is said when a file could not be measured at all (`_say_check`): the
# check did not happen, and that is worth more than knowing nothing. A note,
# not a skip - nothing is refused and nothing is missing, so `pending` never
# hears about it.
UNMEASURED = "length could not be measured here"

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
    # 30s sample a music database served for this recording, when one matched.
    preview: str | None = None


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

    stage: Literal[
        "matching", "downloading", "converting", "tagging", "skipped", "attention"
    ]
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


@lru_cache(maxsize=None)
def find_ffmpeg() -> str | None:
    """Return an ffmpeg binary, or None when this machine has none.

    The system one wins, then a copy shipped next to the app, then the static
    binary inside imageio-ffmpeg. Remembered after the first look: it costs
    three searches to find, it is asked for once for every file the app
    measures and once for every yt-dlp call it makes, and the answer cannot
    change while the app is running.
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
    log: _YtDlpLog = ydl_opts["logger"]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.add_post_processor(_TagFixupPP(), when="pre_process")
            info = ydl.extract_info(target, download=True)
        if ffmpeg is None:
            _convert_downloads(info, target_format, album)
    except (YtDlpDownloadError, OSError) as err:
        # The second one is the file system (a file another program holds, a
        # converter that will not start); either way this download failed and
        # the UI is shown one reason for it, not a traceback.
        raise DownloadError(_told(log, _clean_message(err))) from err

    tracks: list[Track] = []
    rejected = ""
    for entry in _entries(info):
        track = _track_of(entry, target_format)
        if track is None:
            continue
        # A file is only a track if it plays for as long as the upload it came
        # from says it does: what is short goes, is said out loud, and the retry
        # list picks it up like any other track left out (`_reject`).
        expected = entry.get("duration")
        playing = _playing(track.path)
        problem = _short_of(playing, expected, _produce_tolerance(expected))
        if problem is not None:
            rejected = _told(log, problem)
            _forget(track.path)
            _reject(progress_callback, entry, rejected)
            continue
        _say_check(progress_callback, playing, entry.get("title"))
        _enrich(entry, track, album, progress_callback)
        tracks.append(track)
    if not tracks:
        raise DownloadError(rejected or f'No audio file was produced for "{target}".')
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
    already in the folder and plays for as long as the playlist says the track
    lasts is left alone - no lookup, no download - so a playlist that was cut
    short can simply be run again; one that is there but comes up short is
    fetched again rather than trusted (`_short_of`).
    """
    _require_converter(target_format)
    ffmpeg = find_ffmpeg()
    playlist = _spotify_playlist(target)

    dest_root = Path(dest_root).expanduser()
    numbered = album is not None
    out_dir = dest_root / album.folder if album is not None else dest_root
    out_dir.mkdir(parents=True, exist_ok=True)

    total = len(playlist.tracks)
    # What is already in the folder is measured before anything else is
    # decided: one file played through each, and a folder can hold a hundred
    # of them, so the plays happen together - they are independent of each
    # other, while the decisions below are not, and those stay in playlist
    # order, which is the order the UI and `pending` are shown them in. What is
    # there has to be the whole track, not merely a file under the right name:
    # a run killed inside the converter once left one, and taking it for
    # finished keeps it forever, so its length is measured rather than
    # believed, at the allowance `EXISTING_TOLERANCE` sets out.
    present: list[tuple[int, spotify.Track, Path]] = []
    for index, entry in enumerate(playlist.tracks, start=1):
        destination = _destination(out_dir, index, entry.title, numbered, target_format)
        if destination.is_file():
            present.append((index, entry, destination))
    checked: dict[int, _Playing] = {}
    if present:
        with ThreadPoolExecutor(max_workers=min(VERIFY_WORKERS, len(present))) as pool:
            plays = pool.map(lambda item: _playing(item[2]), present)
            for (index, _entry, _path), playing in zip(present, plays):
                checked[index] = playing

    pending: list[tuple[int, spotify.Track]] = []
    for index, entry in enumerate(playlist.tracks, start=1):
        destination = _destination(out_dir, index, entry.title, numbered, target_format)
        if destination.is_file():
            playing = checked.get(index)
            if playing is None:  # a file that turned up while the scan ran
                playing = _playing(destination)
            problem = _short_of(playing, entry.duration, EXISTING_TOLERANCE)
            if problem is None:
                _report_skip(progress_callback, entry, index, total, ALREADY_THERE)
                _say_check(progress_callback, playing, entry.title)
                continue
            if not _forget(destination):
                # Something has the file open, so it cannot be replaced either:
                # fetching it now would only fail on the same file. Say what is
                # wrong with it instead, which the retry list keeps, and come
                # back when whatever is holding it has let go.
                _report_skip(progress_callback, entry, index, total, problem)
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
        except (YtDlpDownloadError, OSError) as err:
            # The second one is the file system: a track whose file another
            # program is holding, a converter that will not start. One track
            # failing on any of that is still one track, not the playlist -
            # which is what this loop is for - so it is left out and said,
            # never raised at the run.
            message = _told(opts["logger"], _clean_message(err))
            failure = f'"{entry.title}": {message}'
            _report_skip(progress_callback, entry, index, total, message)
            _discard(out_dir, index, entry.title, numbered)
            continue
        track = _track_of(info, target_format)
        if track is None:
            _report_skip(
                progress_callback, entry, index, total, "no audio file was produced"
            )
            continue
        # The upload's own length is what a file from it is measured against -
        # it is the same source, so only the rounding of its duration is
        # allowance. Only when the upload did not say, is Spotify's length used
        # instead, and then with the disagreement the two catalogues are given.
        own = info.get("duration")
        playing = _playing(track.path)
        problem = _short_of(
            playing,
            own or entry.duration,
            _produce_tolerance(own) if own else metadata.DURATION_TOLERANCE,
        )
        if problem is not None:
            problem = _told(opts["logger"], problem)
            _forget(track.path)
            _report_skip(progress_callback, entry, index, total, problem)
            failure = f'"{entry.title}": {problem}'
            continue
        _say_check(progress_callback, playing, entry.title)
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


def _reject(
    progress_callback: Callable[[Progress], None] | None,
    entry: dict,
    note: str,
) -> None:
    """Tell the UI about a file that was produced but is not the whole track.

    The same stage a playlist uses for a track it could not match, so a run's
    summary counts it the same way - and when the entry carries its position in
    a playlist, `pending` remembers it like any other track left out, which is
    what turns a file that would have played with holes in it into a fetch on
    the next run.
    """
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
            title=entry.get("title"),
            track_index=entry.get("playlist_index"),
            track_count=entry.get("playlist_count"),
            note=note,
        )
    )


def _attention(
    progress_callback: Callable[[Progress], None] | None,
    note: str,
    title: str | None = None,
) -> None:
    """Tell the UI something worth saying about a file that is being kept.

    Neither a failure nor a skip: nothing was refused and nothing is missing,
    so `pending` never hears about it. It is the only channel a kept track has
    to say "this one is on disk, but look at it".
    """
    if progress_callback is None:
        return
    progress_callback(
        Progress(
            stage="attention",
            percent=None,
            downloaded_bytes=0,
            total_bytes=None,
            speed=None,
            eta=None,
            title=title,
            note=note,
        )
    )


def _say_check(
    progress_callback: Callable[[Progress], None] | None,
    playing: _Playing,
    title: str | None,
) -> None:
    """Say what playing a file showed, when any of it is worth saying.

    The two things a length never says: that nothing could measure it - no
    decoder on this machine, or one that will not read the file, so the check
    was skipped rather than passed - and that silence sits inside the audio,
    where a player would go quiet while the file holds every second it ever
    had. Neither changes what happens to the track: it stays.
    """
    if playing.seconds is None:
        _attention(progress_callback, UNMEASURED, title)
    elif playing.gaps:
        _attention(progress_callback, _gap_note(playing.gaps), title)


def _gap_note(gaps: tuple[tuple[float, float], ...]) -> str:
    """How much of a track is quiet in the middle of it, as the UI shows it."""
    total = sum(end - start for start, end in gaps)
    what = "gap" if len(gaps) == 1 else "gaps"
    return f"{len(gaps)} silent {what} inside the audio ({total:.2f}s)"


def is_rate_limited(reason: str) -> bool:
    """True when `reason` is YouTube refusing the address rather than a miss."""
    if (reason or "") == REFUSED:
        return True
    return any(marker in (reason or "") for marker in RATE_LIMITED)


def retry_after(reason: str) -> int | None:
    """Seconds to wait before asking again, or None when it is not a refusal."""
    return RETRY_AFTER if is_rate_limited(reason) else None


def _lookup_reason(error: Exception) -> str:
    """Why a lookup failed, as the UI will show it.

    YouTube asking for a sign-in is the one worth naming: it is a rate limit on
    this machine rather than anything about the track, and the same playlist
    works again later.
    """
    message = _clean_message(error)
    if is_rate_limited(message):
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


class _Playing(NamedTuple):
    """What playing a file once told us.

    `seconds` is how long it plays and `gaps` the silences found inside the
    audio, measured in that one play. `seconds` is None when nothing here
    could say - a machine with no decoder, or one that would not answer.
    """

    seconds: float | None
    gaps: tuple[tuple[float, float], ...] = ()


def _playing(path: Path) -> _Playing:
    """Play `path` all the way through and report what that says.

    A container writes its length once, into the header it writes first, and
    keeps claiming it after the audio behind it is gone: a FLAC or an MP4 cut
    in half still says it is every second of a track (measured - which is why
    reading the tag would prove nothing). Playing the file is what a player
    does and what a truncated one cannot do for as long, so that is what
    counts here - and it is done once per file: whatever else that play shows
    (`_Playing.gaps`) comes with the length instead of costing a second play.
    """
    ffmpeg = find_ffmpeg()
    if ffmpeg is not None:
        return _decoded(ffmpeg, path)
    # PyAV has the length but not the quiet: it is a decoder, and measuring
    # where a track goes silent needs more than decoding it offers. What this
    # machine cannot check stays unchecked rather than guessed at - which is
    # what `_say_check` then says.
    return _Playing(_pyav_length(path))


def _play_length(path: Path) -> float | None:
    """The seconds `_playing` measured, for a caller that wants nothing else."""
    return _playing(path).seconds


def _decoded(ffmpeg: str, path: Path) -> _Playing:
    """What ffmpeg says about `path`: how long it plays and where it goes quiet.

    Both answers come out of one play - the length `astats` counts to the end
    and the silences `silencedetect` marks on the way - because a second play
    would cost as much as the whole check does.
    """
    try:
        process = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-nostats", "-v", "info", "-i", str(path),
                "-af", PLAY_FILTER, "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=DECODE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return _Playing(None)  # no decoder to ask, or it never finished
    said = (process.stdout or "") + (process.stderr or "")
    rates, counts = _AUDIO_RATE.findall(said), _AUDIO_SAMPLES.findall(said)
    if not (rates and counts):
        # ffmpeg ran and got no audio out of the file at all: nothing of it
        # plays. A run that ended cleanly without a count says nothing.
        return _Playing(0.0 if process.returncode else None)
    seconds = int(counts[-1]) / int(rates[0])
    return _Playing(seconds, _silences(said, seconds))


def _silences(said: str, seconds: float) -> tuple[tuple[float, float], ...]:
    """The gaps `silencedetect` marked inside the audio, edges left out.

    Read as they come: a silence with no end printed never had one - it runs
    to the end of the file, which is an outro - and one whose start was not
    printed began before the file did. Only what starts and ends between the
    two edges says anything about the middle of a track.
    """
    gaps: list[tuple[float, float]] = []
    began: float | None = None
    for what, value in _SILENCE.findall(said):
        if what == "start":
            began = float(value)
        elif began is not None:
            ended = float(value)
            if GAP_EDGE <= began and ended <= seconds - GAP_EDGE:
                gaps.append((began, ended))
            began = None
    return tuple(gaps)


def _pyav_length(path: Path) -> float | None:
    """What PyAV plays of `path` - the decoder a machine without ffmpeg has.

    Any refusal to read is answered with "nothing can be said": on the machine
    that reaches this branch a decoder that objects is more likely a build
    missing a codec than a damaged file, and a file wrongly thrown away is a
    worse answer than one left alone.
    """
    try:
        import av
    except ImportError:
        return None
    try:
        with av.open(str(path)) as incoming:
            stream = incoming.streams.audio[0]
            rate = stream.codec_context.rate
            samples = sum(frame.samples for frame in incoming.decode(stream))
    except Exception:  # noqa: BLE001 - whatever a decoder will not describe
        return None
    return samples / rate if rate else None


def _produce_tolerance(expected: float | None) -> float:
    """How much shorter than `expected` a produced file may be and still be whole.

    Two percent of the track, between the floor a length counted in whole
    seconds needs and the cap a long one is held to: the band a twenty-second
    intro is checked with is a second, a seven-minute track gets the full two -
    one number either way would be far too much of the short one and none of
    what the long one actually needs. Any length that cannot be read is checked
    at the cap, which is the looser of the two.
    """
    try:
        want = float(expected)
    except (TypeError, ValueError):
        return SHORT_TOLERANCE
    return min(SHORT_TOLERANCE, max(MIN_SHORT_TOLERANCE, want * SHORT_TOLERANCE_SHARE))


def _short_of(playing: _Playing, expected: float | None, tolerance: float) -> str | None:
    """Why what `playing` measured is not the whole of the track, when it is not.

    Only a file that comes out *short* counts as wrong. Everything a download
    can lose - a fragment skipped, an extraction cut off, a run killed inside
    the converter - takes seconds away from the track, while a file longer than
    its source only means the two disagree about where the recording ends, and
    fetching it again would not change that. `expected` is the length the source
    itself stated, so `tolerance` is what that statement is worth: the band
    `_produce_tolerance` draws around a file measured against the upload it came
    from, and `EXISTING_TOLERANCE` for one measured against Spotify, which also
    carries what the matcher accepted it for.

    None when the file plays as long as it should - or when nothing here can
    play it, because a doubt must not delete a track that may be fine. What
    `playing` says is taken rather than measured again: a caller that also
    wants `_Playing.gaps` must have played the file once already.
    """
    try:
        want = float(expected)
    except (TypeError, ValueError):
        return None
    if not want:
        return None
    actual = playing.seconds
    if actual is None or actual >= want - tolerance:
        return None
    return f"{INCOMPLETE} ({actual:.1f}s of {want:.1f}s)"


def _discard(out_dir: Path, index: int, title: str, numbered: bool) -> None:
    """Remove what a track that failed left behind: its thumbnail, its part file.

    yt-dlp writes the picture before the audio, so a download that ends in an
    error leaves a cover for a file that does not exist - which is litter in a
    folder the user is meant to read as their music. Tidying up is never worth
    the original failure being replaced by a complaint about a locked file
    (`_forget`), so a leftover that will not go is left where it is.
    """
    prefix = f"{_stem(index, title, numbered)}."
    for leftover in out_dir.iterdir():
        if leftover.name.startswith(prefix):
            _forget(leftover)


def _forget(path: Path) -> bool:
    """Delete `path`; False when it will not go.

    Windows refuses to remove a file another program has open - a player with
    the track in it, an indexer, an antivirus mid-scan - and that refusal must
    not end the run over it: what the caller does next still has to happen, and
    the caller that was about to fetch the track again needs to know the old
    file is still standing there, because writing over it will not work either.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


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
        # Nothing is written under the track's own name until the track is
        # finished: the conversion and the tags both land on a `.part` file that
        # is moved into place in one step at the end. A run cut off in the middle
        # - and Android cuts processes off for less than a full battery - leaves
        # something no folder would offer to play and no second run would take
        # for a finished track, instead of a file that looks done and is not.
        staging = destination.with_name(destination.name + ".part")
        try:
            transcode.convert(source, target_format, staging)
            thumbnail = _thumbnail_of(source)
            cover = transcode.cover_from(thumbnail) if thumbnail is not None else None
            written = metadata.write_tags(staging, _video_tags(entry, album), cover)
            staging.replace(destination)
        except BaseException:
            staging.unlink(missing_ok=True)  # never half a track under a name
            raise
        if destination != source:
            source.unlink(missing_ok=True)
        if written and cover and thumbnail is not None:
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


class _YtDlpLog:
    """Where yt-dlp's messages go, now that the app no longer silences them.

    `no_warnings` used to throw away everything YouTube had to say about a
    download, which is how a track that lost something could still come back
    looking like a success: the complaint existed and nobody ever saw it.
    Chatter stays out of the console (`debug` drops it, which is what `quiet`
    asked for), but warnings and errors are kept - the last few of them, because
    they are what explains a failure, and `_told` puts them next to it.
    """

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def debug(self, message: str) -> None:  # noqa: ARG002 - deliberately dropped
        pass

    def info(self, message: str) -> None:  # noqa: ARG002 - deliberately dropped
        pass

    def warning(self, message: str) -> None:
        self._keep(message)

    def error(self, message: str) -> None:
        self._keep(message)

    def _keep(self, message: str) -> None:
        self.warnings.append(str(message).strip())
        del self.warnings[:-KEPT_WARNINGS]


def _told(log: _YtDlpLog | None, message: str) -> str:
    """`message`, with what yt-dlp only warned about when that is all there is.

    A warning by definition does not stop a download, so without this the one
    that explains why a track came out wrong is shown to nobody - and the next
    run warns into the void again. One warning, the last, so the reason stays
    readable in the skip list and in `pending.json`.
    """
    if log is None or not log.warnings:
        return message
    return f"{message} [{log.warnings[-1]}]" if message else log.warnings[-1]


def _base_opts() -> dict:
    return {
        "quiet": True,
        # Warnings are kept rather than silenced - see `_YtDlpLog`, which is
        # also what `quiet` routes the rest of yt-dlp's chatter into.
        "logger": _YtDlpLog(),
        # yt-dlp's own default is to skip a fragment it cannot fetch and hand
        # back the rest as though nothing were missing (`skip_unavailable_fragments`).
        # A fragment skipped is a piece of the song gone - it plays as a cut
        # through the whole track - so a fragment that will not come is an
        # error here: the track is reported, not silently delivered short.
        "skip_unavailable_fragments": False,
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


def _preview_for(title: str, artist: str | None, duration: float | None) -> str | None:
    """30s sample for one resolved track, or None when no database knows it.

    Best effort and silent: a database that is down or does not know the track
    only means its row plays nothing, never a failed search.
    """
    try:
        match = metadata.lookup(title, artist, duration)
    except Exception:  # noqa: BLE001 - a preview must never break a search
        return None
    return match.preview if match is not None else None

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
            preview=None,
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
        preview=_preview_for(title, _clean_artist(info.get("artist") or info.get("uploader")), info.get("duration")),
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
