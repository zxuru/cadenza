"""The failures the user reads: one code, and one sentence in their language.

yt-dlp explains itself to whoever wrote it - "ERROR: unable to download video
data: HTTP Error 403: Forbidden", wrapped in terminal colour, and written a
second time when the same run also kept a warning beside it. The window showed
that verbatim: English, doubled, escape sequences and all, which is neither a
reason nor something to try next.

`describe` turns such a report into a `Failure`: a short code a user can quote
in a bug report, and the key of the sentence that says what happened and what
to do about it. `clean` is the readable half on its own - escapes gone, the
doubled sentence collapsed - and anything about to be shown goes through it.

The codes are a closed set on purpose. A report that says "E-403" names the
failure without a screenshot, and every one of them is something the user can
act on: wait, sign in, choose another link.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

#: Longest tail of an unclassified message that is shown; the rest is cut.
DETAIL_LIMIT = 160

# Terminal colour, as yt-dlp writes it: the escape and its parameters.
_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# The same code with the escape byte already lost (a copy-paste, a logger that
# ate it): on its own it is noise the user must not be shown either.
_ORPHAN_SGR = re.compile(r"\[[0-9]{1,2}(?:;[0-9]{1,2})*m")
# `_told` appends the last warning yt-dlp kept, in brackets. When that warning
# is the same sentence the failure already carried, the copy is dropped.
_BRACKETED = re.compile(r"^(?P<said>.+?)\s*\[(?P<again>.*)\]$", re.DOTALL)
# What yt-dlp prefixes its own lines with, in the shapes it writes them.
_PREFIXES = ("ERROR: ", "error: ", "WARNING: ", "warning: ")


@dataclass(frozen=True)
class Failure:
    """One failure, reduced to a code and the sentence that explains it."""

    code: str
    """Short handle for the failure, as the user sees it: `E-403`."""

    key: str
    """Message key in the locale files holding what to say about it."""

    detail: str = ""
    """Cleaned text of a failure that matched nothing, and only then."""

    def line(self, say: Callable[..., str]) -> str:
        """What the window shows: the code, then the sentence beside it.

        `say` is the translator the caller already holds, so this module names
        the failure and the UI decides which language it is read in.
        """
        return f"{self.code} · {say(self.key, detail=self.detail)}"


# The failures worth naming, in the order they are looked for: an HTTP status
# is the reason even when the sentence around it says the download failed
# ("unable to download video data: HTTP Error 403: Forbidden"), so every
# status is tested before the transport that carried it.
_TABLE: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "E-401",
        "error_401",
        (
            "http error 401",
            "sign in to confirm",
            "sign in to verify",
            "please sign in",
            "login required",
            "requires authentication",
            "authentication required",
        ),
    ),
    (
        "E-403",
        "error_403",
        ("http error 403", "403 forbidden", "forbidden", "refused the request"),
    ),
    (
        "E-404",
        "error_404",
        (
            "http error 404",
            "http error 410",
            "video unavailable",
            "this video is not available",
            "removed by the uploader",
            "private video",
            "no longer available",
            "has been removed",
        ),
    ),
    ("E-429", "error_429", ("http error 429", "too many requests")),
    (
        "E-500",
        "error_500",
        (
            "http error 500",
            "http error 502",
            "http error 503",
            "http error 504",
            "internal server error",
            "bad gateway",
            "service unavailable",
        ),
    ),
    (
        "E-LINK",
        "error_link",
        (
            "unsupported url",
            "unsupported format",
            "not a valid url",
            "unable to extract",
            "not a spotify playlist link",
        ),
    ),
    ("E-SPOTIFY", "error_spotify", ("spotify",)),
    (
        "E-EMPTY",
        "error_empty",
        (
            "no tracks found",
            "no audio file was produced",
            "no audio stream",
            "nothing to download",
            "no preview could be fetched",
            "no track of",
        ),
    ),
    ("E-NOMATCH", "error_nomatch", ("no match on youtube", "nothing matched")),
    ("E-SHORT", "error_short", ("incomplete file",)),
    ("E-INPUT", "error_input", ("enter a song", "enter an album", "enter a url")),
    (
        "E-NET",
        "error_net",
        (
            "unable to download",
            "timed out",
            "timeout",
            "connection",
            "network",
            "getaddrinfo",
            "name resolution",
            "certificate",
        ),
    ),
    (
        "E-DISK",
        "error_disk",
        (
            "no space left",
            "permission denied",
            "read-only file system",
            "input/output error",
            "errno 13",
            "errno 28",
            "errno 30",
        ),
    ),
    ("E-CONV", "error_conv", ("ffmpeg", "postprocessing")),
)
#: Nothing above matched: the failure is named, and its cleaned text kept.
OTHER = "E-FAIL"


def codes() -> tuple[str, ...]:
    """Every code `describe` can answer with, `E-FAIL` included."""
    return tuple(code for code, _, _ in _TABLE) + (OTHER,)


def clean(message: object) -> str:
    """`message` as a person can read it: no escapes, nothing said twice.

    One line, too: a failure is shown where a sentence fits, and a traceback
    pasted there is not a sentence.
    """
    text = _ORPHAN_SGR.sub("", _ESCAPE.sub("", str(message or "")))
    text = " ".join(text.split())
    for prefix in _PREFIXES:
        while text.startswith(prefix):
            text = text[len(prefix) :].strip()
    while match := _BRACKETED.match(text):
        said, again = match["said"].strip(), match["again"].strip()
        if said != again:
            break
        text = said
    return text


def describe(message: object) -> Failure:
    """`message` as a code and a sentence, whatever shape it arrived in."""
    text = clean(message)
    lowered = text.lower()
    for code, key, markers in _TABLE:
        if any(marker in lowered for marker in markers):
            return Failure(code, key)
    if not text:
        return Failure(OTHER, "error_other")
    return Failure(OTHER, "error_other_detail", _short(text))


def _short(text: str) -> str:
    """The tail of a message nothing matched, short enough for the status line."""
    if len(text) <= DETAIL_LIMIT:
        return text
    return text[: DETAIL_LIMIT - 1].rstrip() + "…"
