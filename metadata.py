"""Real music metadata: match a downloaded track in a public music database.

YouTube hands over a video, so a plain re-upload tags a file with the channel
as the artist and the category ("People & Blogs") as the genre. Deezer and
iTunes know the release instead, and neither needs a key or an account: the
tags are looked up there once the download has finished and written into the
file with mutagen.

Both the lookup and the write are best effort. A database that is down, slow
or simply does not know the track leaves the file exactly as yt-dlp wrote it -
a finished download is never reported as failed because a tag could not be
improved.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from settings import APP_NAME

DEEZER_SEARCH = "https://api.deezer.com/search"
DEEZER_ALBUM = "https://api.deezer.com/album/{album_id}"
ITUNES_SEARCH = "https://itunes.apple.com/search"

# How far a candidate's length may be from the video's before it is a different
# recording (a live take, an extended edit, a cover) rather than the same one.
# A re-upload can add an intro or run a few seconds long, so this is a window
# and not an equality test.
DURATION_TOLERANCE = 10  # seconds
CANDIDATES = 5
TIMEOUT = 10  # seconds per request
# Deezer localizes its genre names from the caller's locale; ask for English so
# one library does not end up with genres in two languages.
LANGUAGE = "en"
USER_AGENT = APP_NAME
# iTunes serves the same artwork at any size, so ask for the largest and take
# whatever resolution the original has.
ITUNES_ARTWORK = "3000x3000bb"

_BRACKETS = re.compile(r"[([{].*?[)\]}]")
_FEATURING = re.compile(r"\b(feat|ft|featuring|with)\b.*$")
_NON_WORD = re.compile(r"[^a-z0-9]+")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True, slots=True)
class Match:
    """What a music database knows about one recording."""

    title: str
    artist: str
    album: str | None = None
    album_artist: str | None = None
    date: str | None = None  # ISO date, "2014-12-27"
    genre: str | None = None
    track: int | None = None
    disc: int | None = None
    cover: bytes | None = None


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One search hit, normalized to the fields both databases share."""

    title: str
    artist: str
    duration: float | None = None
    album: str | None = None
    album_artist: str | None = None
    date: str | None = None
    genre: str | None = None
    track: int | None = None
    disc: int | None = None
    cover_url: str | None = None
    album_id: str | None = None  # Deezer only: genres and cover live on the album


def lookup(
    title: str, artist: str | None = None, duration: float | None = None
) -> Match | None:
    """Find the recording behind `title` and `artist`, or None when in doubt.

    iTunes answers first: a single request already carries the album, the album
    artist, the track and disc number, the genre and a square cover. Deezer
    covers the tracks iTunes does not know - and the moment its rate limit
    starts refusing them.
    """
    for find in (_find_itunes, _find_deezer):
        try:
            match = find(title, artist, duration)
        except Exception:  # noqa: BLE001 - a lookup must never break a download
            continue
        if match is not None:
            return match
    return None


def apply(path: Path, match: Match, keep: Mapping[str, str] | None = None) -> bool:
    """Write `match` into the audio file at `path`; True when it was written.

    `keep` holds the tags the download itself owns - the album the user picked
    and the track's position in it - and wins over the database, which may have
    matched the recording on a different release. Containers that cannot hold a
    picture are left untouched.
    """
    return write_tags(path, _tags(match) | dict(keep or {}), match.cover)


def write_tags(path: Path, tags: Mapping[str, str], cover: bytes | None = None) -> bool:
    """Write plain tags into the audio file at `path`; True when they went in.

    The write `apply` ends in, and the one a build without ffmpeg needs for the
    tags a video carries: nothing else would put them in the file there. A file
    whose container cannot hold a picture, or that nothing here knows how to
    tag, is left alone.
    """
    writer = _WRITERS.get(path.suffix.lower())
    if writer is None:
        return False
    try:
        writer(path, dict(tags), cover)
    except ImportError:  # mutagen is optional, exactly as it is for yt-dlp
        return False
    except Exception:  # noqa: BLE001 - a tag write must not fail the download
        return False
    return True


# --------------------------------------------------------------------- lookup


def _find_deezer(title: str, artist: str | None, duration: float | None) -> Match | None:
    """Deezer: the track carries its position, the album the genres and cover."""
    candidate = _pick(_deezer_hits(title, artist), title, artist, duration)
    if candidate is None:
        return None
    album = _deezer_album(candidate.album_id or "")
    return Match(
        title=candidate.title,
        artist=candidate.artist,
        album=_text(album.get("title")) or candidate.album,
        album_artist=_text(_mapping(album.get("artist")).get("name")),
        date=candidate.date or _date(album.get("release_date")),
        genre=_genre(album) or candidate.genre,
        track=candidate.track,
        disc=candidate.disc,
        cover=_cover(album.get("cover_xl")),
    )


def _find_itunes(title: str, artist: str | None, duration: float | None) -> Match | None:
    candidate = _pick(_itunes_hits(title, artist), title, artist, duration)
    if candidate is None:
        return None
    return Match(
        title=candidate.title,
        artist=candidate.artist,
        album=candidate.album,
        album_artist=candidate.album_artist,
        date=candidate.date,
        genre=candidate.genre,
        track=candidate.track,
        disc=candidate.disc,
        cover=_cover(candidate.cover_url),
    )


def _deezer_hits(title: str, artist: str | None) -> list[_Candidate]:
    """Search Deezer; field queries first, then the words as plain text.

    `track:"..."` is precise but rejects titles carrying punctuation the
    database stored differently, so a strict query that finds nothing is
    retried as a loose one.
    """
    strict = f'track:"{title}"' + (f' artist:"{artist}"' if artist else "")
    for query in dict.fromkeys((strict, " ".join(part for part in (title, artist) if part))):
        payload = _get_json(f"{DEEZER_SEARCH}?q={urllib.parse.quote(query)}&limit={CANDIDATES}")
        hits = [_deezer_candidate(item) for item in (payload or {}).get("data") or []]
        if hits:
            return hits
    return []


def _itunes_hits(title: str, artist: str | None) -> list[_Candidate]:
    term = " ".join(part for part in (title, artist) if part)
    payload = _get_json(
        f"{ITUNES_SEARCH}?term={urllib.parse.quote(term)}&entity=song&limit={CANDIDATES}"
    )
    return [_itunes_candidate(item) for item in (payload or {}).get("results") or []]


def _deezer_candidate(item: dict) -> _Candidate:
    album = _mapping(item.get("album"))
    return _Candidate(
        title=_text(item.get("title")) or "",
        artist=_text(_mapping(item.get("artist")).get("name")) or "",
        duration=_number(item.get("duration")),
        album=_text(album.get("title")),
        date=_date(item.get("release_date")),
        track=_position(item.get("track_position")),
        disc=_position(item.get("disk_number")),
        cover_url=_text(album.get("cover_xl")),
        album_id=_text(album.get("id")),
    )


def _itunes_candidate(item: dict) -> _Candidate:
    artwork = _text(item.get("artworkUrl100"))
    return _Candidate(
        title=_text(item.get("trackName")) or "",
        artist=_text(item.get("artistName")) or "",
        duration=_milliseconds(item.get("trackTimeMillis")),
        album=_text(item.get("collectionName")),
        album_artist=_text(item.get("collectionArtistName")) or _text(item.get("artistName")),
        date=_date(item.get("releaseDate")),
        genre=_text(item.get("primaryGenreName")),
        track=_position(item.get("trackNumber")),
        disc=_position(item.get("discNumber")),
        cover_url=artwork.replace("100x100bb", ITUNES_ARTWORK) if artwork else None,
    )


def _pick(
    candidates: list[_Candidate], title: str, artist: str | None, duration: float | None
) -> _Candidate | None:
    """Best candidate for the video, or None when none is the same recording."""
    best: _Candidate | None = None
    best_key: tuple[int, int, float] | None = None
    for candidate in candidates:
        key = _match_key(candidate, title, artist, duration)
        if key is not None and (best_key is None or key > best_key):
            best, best_key = candidate, key
    return best


def _match_key(
    candidate: _Candidate, title: str, artist: str | None, duration: float | None
) -> tuple[int, int, float] | None:
    """Rank a candidate: exact title beats a partial one, then artist, then length.

    None means it is not the same recording: a different title, or a length too
    far from the video's to be the same take. The video's own metadata is weak
    (an uploader often puts the artist in the title and the title in the
    channel), so a candidate is not rejected for a mismatched artist - it only
    ranks lower.
    """
    rank = title_score(normalize(title), normalize(candidate.title))
    if not rank:
        return None
    delta = 0.0
    if duration and candidate.duration:
        delta = abs(duration - candidate.duration)
        if delta > DURATION_TOLERANCE:
            return None
    return (rank, artist_score(artist, candidate.artist), -delta)


def title_score(source: str, candidate: str) -> int:
    """3 when the titles are the same, 1 when one contains the other, else 0.

    Containment is what matches an uploader's decoration ("Sneaky Snitch
    (Kevin MacLeod) - Background Music (HD)") against the plain title.
    """
    if not source or not candidate:
        return 0
    if source == candidate:
        return 3
    return 1 if source in candidate or candidate in source else 0


def artist_score(source: str | None, candidate: str) -> int:
    """Shared words between the two names: "Kevin MacLeod Archive" is Kevin MacLeod."""
    if not source or not candidate:
        return 0
    return len(set(normalize(source).split()) & set(normalize(candidate).split()))


def normalize(text: str) -> str:
    """Case-, accent- and punctuation-insensitive form, for comparing names."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    plain = _BRACKETS.sub(" ", plain.casefold())
    plain = _FEATURING.sub(" ", plain)
    return _NON_WORD.sub(" ", plain).strip()


# ------------------------------------------------------------------ the caches

_CACHE_LOCK = threading.Lock()
_ALBUMS: dict[str, dict] = {}
_COVERS: dict[str, bytes] = {}


def _deezer_album(album_id: str) -> dict:
    """Album payload, fetched once: a download of one album shares its cover."""
    if not album_id:
        return {}
    with _CACHE_LOCK:
        cached = _ALBUMS.get(album_id)
    if cached is not None:
        return cached
    payload = _get_json(DEEZER_ALBUM.format(album_id=urllib.parse.quote(album_id)))
    album = payload if isinstance(payload, dict) and "error" not in payload else {}
    with _CACHE_LOCK:
        _ALBUMS[album_id] = album
    return album


def _cover(url: str | None) -> bytes | None:
    """Cover art, fetched once per URL: every track of an album points at it."""
    if not url:
        return None
    with _CACHE_LOCK:
        cached = _COVERS.get(url)
    if cached is not None:
        return cached
    try:
        data = _request(url)
    except OSError:
        return None
    with _CACHE_LOCK:
        _COVERS[url] = data
    return data


# --------------------------------------------------------------------- network


def _get_json(url: str) -> dict | None:
    try:
        payload = json.loads(_request(url))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _request(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": LANGUAGE}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


# -------------------------------------------------------------------- the file


def _tags(match: Match) -> dict[str, str]:
    """Generic tag names; each writer translates them for its container."""
    values = {
        "title": match.title,
        "artist": match.artist,
        "album": match.album,
        "album_artist": match.album_artist,
        "date": match.date,
        "genre": match.genre,
        "track": _text(match.track),
        "disc": _text(match.disc),
    }
    return {name: value for name, value in values.items() if value}


def _write_flac(path: Path, tags: dict[str, str], cover: bytes | None) -> None:
    from mutagen.flac import FLAC, Picture

    keys = {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "album_artist": "albumartist",
        "date": "date",
        "genre": "genre",
        "track": "tracknumber",
        "disc": "discnumber",
    }
    audio = FLAC(path)
    for name, value in tags.items():
        if key := keys.get(name):
            audio[key] = [value]
    if cover:
        picture = Picture()
        picture.type = 3  # front cover
        picture.mime = _image_mime(cover)
        picture.data = cover
        audio.clear_pictures()
        audio.add_picture(picture)
    audio.save()


def _write_mp3(path: Path, tags: dict[str, str], cover: bytes | None) -> None:
    from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
    from mutagen.mp3 import MP3

    frames = {
        "title": TIT2,
        "artist": TPE1,
        "album": TALB,
        "album_artist": TPE2,
        "date": TDRC,
        "genre": TCON,
        "track": TRCK,
        "disc": TPOS,
    }
    # A file without tags yet gets an empty tag object to write into.
    id3 = MP3(path, ID3=ID3).tags or ID3()
    for name, value in tags.items():
        if frame := frames.get(name):
            id3.add(frame(encoding=3, text=[value]))  # replaces the same frame
    if cover:
        id3.delall("APIC")
        id3.add(APIC(encoding=3, mime=_image_mime(cover), type=3, desc="Cover", data=cover))
    id3.save(path)


def _write_mp4(path: Path, tags: dict[str, str], cover: bytes | None) -> None:
    from mutagen.mp4 import MP4, MP4Cover

    keys = {
        "title": "\xa9nam",
        "artist": "\xa9ART",
        "album": "\xa9alb",
        "album_artist": "aART",
        "date": "\xa9day",
        "genre": "\xa9gen",
    }
    audio = MP4(path)
    for name, value in tags.items():
        if key := keys.get(name):
            audio[key] = [value]
    # The track and disc number are a pair in MP4: number, total.
    if (track := tags.get("track", "")).isdigit():
        audio["trkn"] = [(int(track), 0)]
    if (disc := tags.get("disc", "")).isdigit():
        audio["disk"] = [(int(disc), 0)]
    if cover:
        audio["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


_WRITERS = {".flac": _write_flac, ".mp3": _write_mp3, ".m4a": _write_mp4}


def _image_mime(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg"


# ------------------------------------------------------------------ conversion


def _text(value: object) -> str | None:
    """Database field as a stripped string; numbers are accepted as they come."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    return text or None


def _mapping(value: object) -> dict:
    """A nested JSON object, or an empty one: the APIs never promise a shape."""
    return value if isinstance(value, dict) else {}


def _genre(album: dict) -> str | None:
    """Deezer keeps genres on the album, in the language Accept-Language asks for."""
    for item in album.get("genres") or []:
        if isinstance(item, dict) and (name := _text(item.get("name"))):
            return name
    return None


def _number(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _milliseconds(value: object) -> float | None:
    millis = _number(value)
    return millis / 1000 if millis else None


def _position(value: object) -> int | None:
    """Track or disc number, when the database has a sane one."""
    number = _number(value)
    return int(number) if number and number >= 1 else None


def _date(value: object) -> str | None:
    """The date part of a release date: Deezer sends a date, iTunes an instant."""
    text = _text(value)
    if not text:
        return None
    found = _DATE.search(text)
    return found.group(0) if found else None
