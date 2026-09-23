"""Tracks a download did not get, kept until they are got.

A playlist is the one download that cannot simply be asked for again: forty
tracks, thirty-nine of them already in the folder, and the last one missing
because YouTube was refusing requests that minute. What is missing is written
down here - the link it came from, the folder it belongs in, the format and
which track - so the app can finish the job by itself the next time it starts,
and keep a count until it does.

The list holds what is missing, never what is done: an entry leaves the moment
the track is on disk. It lives beside the settings (see `settings.config_dir`),
so it survives a restart, and if it cannot be read or written the app simply
behaves as it did before there was one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from settings import config_dir

FILE_NAME = "pending.json"
# How often the app retries by itself before leaving it to the button. A track
# YouTube genuinely does not have would otherwise be searched on every start.
MAX_ATTEMPTS = 5
# A lid on the file: someone with a hundred half-downloaded playlists keeps the
# newest entries rather than a list that grows without end.
MAX_ITEMS = 500


@dataclass(frozen=True, slots=True)
class Item:
    """One track that is not on disk yet, and why it is not."""

    url: str  # the link that was asked for
    root: str  # download folder the rest of that playlist sits in
    format: str
    index: int  # position in the playlist
    title: str
    reason: str  # what the last attempt ended in
    attempts: int = 1

    @property
    def key(self) -> tuple[str, str, str, int]:
        """What makes two entries the same track."""
        return (self.url, self.root, self.format, self.index)

    @property
    def place(self) -> tuple[str, str, str]:
        """What makes two entries the same download: one link, one folder, one format."""
        return (self.url, self.root, self.format)


def path() -> Path:
    """Where the list lives: next to the settings, per user."""
    return config_dir() / FILE_NAME


def load() -> list[Item]:
    """Every track still missing; a file that cannot be read is an empty list."""
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    items = [item for item in (_item(entry) for entry in raw) if item is not None]
    return items[-MAX_ITEMS:]


def save(items: Sequence[Item]) -> None:
    """Write the list, keeping the newest entries when there are too many."""
    payload = [_entry(item) for item in list(items)[-MAX_ITEMS:]]
    try:
        path().parent.mkdir(parents=True, exist_ok=True)
        path().write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # a list that cannot be written is a convenience lost, not a failure


def merged(items: Sequence[Item], failures: Sequence[Item]) -> list[Item]:
    """`items` with `failures` folded in: one entry per track, counted up.

    The count is what keeps a track that will never be there from being looked
    up on every start for the rest of time (`due`), and the reason is replaced
    by the newest one, which is the one worth showing.
    """
    merged = {item.key: item for item in items}
    for failure in failures:
        previous = merged.get(failure.key)
        merged[failure.key] = replace(
            failure, attempts=previous.attempts + 1 if previous else 1
        )
    return list(merged.values())


def without(items: Sequence[Item], place: tuple[str, str, str]) -> list[Item]:
    """`items` minus one download's entries: what a run of it has accounted for."""
    return [item for item in items if item.place != place]


def due(items: Sequence[Item]) -> list[Item]:
    """The ones still worth trying by themselves."""
    return [item for item in items if item.attempts < MAX_ATTEMPTS]


def _entry(item: Item) -> dict:
    return {
        "url": item.url,
        "root": item.root,
        "format": item.format,
        "index": item.index,
        "title": item.title,
        "reason": item.reason,
        "attempts": item.attempts,
    }


def _item(entry: object) -> Item | None:
    """One entry as an `Item`; anything malformed is dropped, never raised."""
    if not isinstance(entry, dict):
        return None
    url, root, target_format, title = (
        entry.get(key) for key in ("url", "root", "format", "title")
    )
    if not all(isinstance(value, str) and value for value in (url, root, target_format, title)):
        return None
    index, attempts = entry.get("index"), entry.get("attempts")
    if not isinstance(index, int) or isinstance(index, bool) or index < 1:
        return None
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        attempts = 1
    return Item(
        url=url,
        root=root,
        format=target_format,
        index=index,
        title=title,
        reason=str(entry.get("reason") or ""),
        attempts=attempts,
    )
