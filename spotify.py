"""Read a public Spotify playlist without an API key or a login.

Spotify's Web API needs OAuth, and the anonymous token its web player used to
hand out is refused outright now (HTTP 400, "Usage of this endpoint is not
permitted under the Spotify Developer Terms"), so there is nothing to call.
What is left is the player Spotify embeds in third-party pages, and it asks for
no credentials at all: ``https://open.spotify.com/embed/playlist/<id>`` answers
with the playlist's first tracks as JSON in its ``__NEXT_DATA__`` script -
title, artist and length, which is what a search on YouTube Music needs (see
`engine`) and all this module reads.

Two things the embed cannot do, both Spotify's doing and both reported instead
of papered over:

* **Private playlists.** The embed carries published ones only: a private,
  deleted or region-locked playlist answers with the player's "content is not
  available" page instead of a track list, and reading it would take the
  owner's login.
* **More than `TRACK_LIMIT` tracks.** The embed carries the first hundred and
  there is no way to ask for the next page - it ignores ``?offset=`` and
  ``?limit=`` - so `Playlist.truncated` says when a longer playlist was cut.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

EMBED_URL = "https://open.spotify.com/embed/{kind}/{id}"
HOST = "open.spotify.com"
# The entities the embed player carries, and the ones a link is accepted for.
KINDS = ("playlist", "album", "track")
# What the embed carries of one collection, and no way to page past it.
TRACK_LIMIT = 100
TIMEOUT = 15  # seconds
# The embed player is a browser page; it answers a browser.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# The player ships its data in the same Next.js payload its pages carry, and
# builds the rest of a long list as the user scrolls - which is exactly the
# part that never reaches this module.
_NEXT_DATA = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)
# `/intl-es/playlist/<id>`: the locale prefix a shared link can carry.
_LOCALE = re.compile(r"intl-[a-z]{2}(?:-[a-z]{2})?", re.I)
# A Spotify id: 22 characters of base62, and no shorter in practice.
_ID = re.compile(r"[A-Za-z0-9]{16,}")


class SpotifyError(RuntimeError):
    """A link that is not a Spotify link, or one Spotify will not show."""


@dataclass(frozen=True, slots=True)
class Track:
    """One entry of a playlist: what identifying the song on YouTube takes."""

    title: str
    artist: str | None
    duration: float | None  # seconds


@dataclass(frozen=True, slots=True)
class Playlist:
    """A linked playlist, album or track, as the embed describes it."""

    name: str
    owner: str | None
    tracks: tuple[Track, ...]
    truncated: bool = False


def handles(url: str) -> bool:
    """True when `url` is a link to a Spotify playlist, album or track."""
    return _identify(url) is not None


def _identify(url: str) -> tuple[str, str] | None:
    """`(kind, id)` of a Spotify link, or None when the link is not one."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or parts.hostname != HOST:
        return None
    segments = [segment for segment in parts.path.split("/") if segment]
    if segments and _LOCALE.fullmatch(segments[0]):
        segments.pop(0)
    if segments and segments[0] == "embed":  # the player's own URL works too
        segments.pop(0)
    if len(segments) != 2 or segments[0] not in KINDS or not _ID.fullmatch(segments[1]):
        return None
    return segments[0], segments[1]


@lru_cache(maxsize=8)
def playlist(url: str) -> Playlist:
    """Read the collection at `url`.

    Cached: one link is read three times - when it is pasted, when the dialog
    is filled in and when the download starts - and the embed carries up to
    100 tracks each time it is asked.
    """
    identity = _identify(url)
    if identity is None:
        raise SpotifyError(f"Not a Spotify playlist link: {url}")
    kind, identifier = identity
    entity = _entity(_get(EMBED_URL.format(kind=kind, id=identifier)), identifier)
    tracks = _tracks(entity)
    if not tracks:
        raise SpotifyError(f"Spotify has no track list for {identifier}")
    return Playlist(
        name=_text(entity.get("title")) or _text(entity.get("name")) or identifier,
        owner=_owner(entity),
        tracks=tuple(tracks),
        truncated=len(tracks) >= TRACK_LIMIT,
    )


def _entity(page: str, identifier: str) -> dict:
    """The player's own JSON, or a complaint about what came back instead."""
    match = _NEXT_DATA.search(page)
    if match is None:
        raise SpotifyError(f"Spotify served no playlist data for {identifier}")
    try:
        entity = json.loads(match.group(1))["props"]["pageProps"]["state"]["data"][
            "entity"
        ]
    except (ValueError, KeyError, TypeError):
        entity = None
    if not isinstance(entity, dict):
        # What a private, deleted or region-locked playlist answers with: the
        # player's notice, and no `state` in the payload at all.
        raise SpotifyError(
            f"Spotify will not show {identifier}: private, deleted or not published"
        )
    return entity


def _tracks(entity: dict) -> list[Track]:
    """Every track the embed carries, in the order the playlist has them."""
    if entity.get("type") == "track":  # a single-track link carries no list
        entity = entity | {"subtitle": _first_artist(entity)}
        return [track for track in (_track(entity),) if track is not None]
    return [track for item in entity.get("trackList") or [] if (track := _track(item))]


def _track(item: dict) -> Track | None:
    """One entry as a track; an entry with no title at all is not one."""
    title = _text(item.get("title"))
    if title is None:
        return None
    return Track(
        title=title,
        # The list carries the artist as the entry's subtitle; a playlist of
        # podcast episodes carries the show there instead, which is the same
        # thing to a YouTube search.
        artist=_text(item.get("subtitle")),
        duration=_seconds(item.get("duration")),
    )


def _owner(entity: dict) -> str | None:
    """Who the embed credits the collection to: the artist, or the playlist's owner."""
    if entity.get("type") == "track":
        return _first_artist(entity)
    authors = entity.get("authors") or []
    names = [_text(item.get("name")) for item in authors if isinstance(item, dict)]
    return next((name for name in names if name), None) or _text(entity.get("subtitle"))


def _first_artist(entity: dict) -> str | None:
    artists = entity.get("artists") or []
    names = [_text(item.get("name")) for item in artists if isinstance(item, dict)]
    return next((name for name in names if name), None)


def _seconds(milliseconds: object) -> float | None:
    """A length as the embed states it (milliseconds) in yt-dlp's unit."""
    if isinstance(milliseconds, bool) or not isinstance(milliseconds, (int, float)):
        return None
    return milliseconds / 1000 if milliseconds else None


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _get(url: str) -> str:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as err:
        raise SpotifyError(f"Spotify's embed player did not answer: {err}") from err
