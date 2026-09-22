"""UI strings, system-language detection, and the locale files they come from.

Every language is one file, `locales/<code>.json` (ISO 639-1, plus a region
only when it differs from the base language: `en.json`, `es.json`,
`pt-BR.json`).  Dropping a file in is the whole registration step — nothing in
this module or in the UI lists the languages by hand.

A locale file is a flat JSON object.  A key holds either the finished string:

    "search": "Search",

or, when the text depends on a count, its plural forms:

    "status_results": {
        "one": "1 result found.",
        "many": "{count} results found."
    }

`Translator` renders the first with `t("search")` and the second with
`t.plural("status_results", count)`; `{name}` placeholders are filled from the
keyword arguments of either call.  Only the forms a language actually needs
have to be defined, and anything missing falls back to English, so a partially
translated file is always usable.

`python i18n.py` checks every language against English and exits non-zero when
one is incomplete, unknown keys included; `python i18n.py pt-BR` checks one.
"""

from __future__ import annotations

import json
import os
import sys
from functools import lru_cache
from pathlib import Path

import bundle

DEFAULT_LANGUAGE = "en"

# A key holds either the finished string or, when the text depends on a count,
# the plural forms it can take (`one` for a count of one, `many` otherwise).
Entry = str | dict[str, str]
_PLURAL_FORMS = ("one", "many")


def locales_dir() -> Path:
    """Directory holding the locale files; inside the bundle once frozen."""
    return bundle.locales_dir()


@lru_cache(maxsize=1)
def available_languages() -> tuple[str, ...]:
    """Codes with a locale file on disk, the default language first."""
    if not locales_dir().is_dir():
        return ()
    codes = {
        path.stem
        for path in locales_dir().glob("*.json")
        if not path.name.startswith(("_", "."))
    }
    return tuple(sorted(codes, key=lambda code: (code != DEFAULT_LANGUAGE, code)))


@lru_cache(maxsize=None)
def catalog(language: str) -> dict[str, Entry]:
    """Strings of one language; empty (and reported) when its file is unusable."""
    path = locales_dir() / f"{language}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        _warn(f"{path} is missing; using {DEFAULT_LANGUAGE} instead")
        return {}
    except ValueError as error:
        _warn(f"{path} is not valid JSON ({error}); using {DEFAULT_LANGUAGE} instead")
        return {}
    if not isinstance(raw, dict):
        _warn(f"{path} must hold a JSON object mapping keys to strings")
        return {}
    return {key: entry for key, entry in raw.items() if _valid(key, entry, path)}


def resolve(language: str | None) -> str:
    """Code of the locale file serving `language`; English when none does."""
    if not language:
        return DEFAULT_LANGUAGE
    wanted = _normalize(language)
    codes = available_languages()
    for code in codes:
        if _normalize(code) == wanted:
            return code
    # A regional variant falls back to its base language: es-AR is served by
    # es.json, and pt is served by pt-BR.json.
    for code in codes:
        if _primary(code) == _primary(wanted):
            return code
    return DEFAULT_LANGUAGE


def system_language() -> str:
    """Language code the operating system is set to (`en` when unsure)."""
    if sys.platform == "win32":
        code = _windows_language()
        if code:
            return _primary(code)
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(variable)
        if not value or value in ("C", "POSIX"):
            continue
        # "es_ES.UTF-8" / "es-AR" / "es:en" all mean Spanish
        code = _primary(value)
        if code:
            return code
    return DEFAULT_LANGUAGE


def _windows_language() -> str | None:
    """Windows' user UI language (`es_ES`), or None when it cannot be read."""
    try:
        import ctypes
        import locale

        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except Exception:  # noqa: BLE001 - never let locale detection break startup
        return None
    # Neutral ids (0x0A for Spanish, whose regions are 0x0C0A, 0x080A, ...) are
    # absent from the table; any region of the same language answers the
    # question just as well, since `resolve()` matches on the primary subtag.
    return locale.windows_locale.get(langid) or next(
        (
            name
            for identifier, name in locale.windows_locale.items()
            if (identifier & 0x3FF) == (langid & 0x3FF)
        ),
        None,
    )


def _normalize(code: str) -> str:
    """Comparable form of a language code, from `LANG`-style values included."""
    return code.strip().split(":")[0].split(".")[0].replace("_", "-").lower()


def _primary(code: str) -> str:
    """`es_ES` -> `es`: the language a regional variant also answers to."""
    return _normalize(code).split("-")[0]


def _valid(key: str, entry: object, path: Path) -> bool:
    """Keep a well-formed entry; a broken one is dropped and reported."""
    if isinstance(entry, str):
        return True
    if (
        isinstance(entry, dict)
        and entry
        and all(form in _PLURAL_FORMS for form in entry)
        and all(isinstance(text, str) for text in entry.values())
    ):
        return True
    _warn(
        f"{path}: {key!r} must be a string, or an object with "
        f"{' and '.join(_PLURAL_FORMS)} strings"
    )
    return False


def _warn(message: str) -> None:
    """Complain on stderr, which a windowed build may not have."""
    if sys.stderr is not None:
        print(f"i18n: {message}", file=sys.stderr)


class Translator:
    """Renders UI strings for one language, falling back to English."""

    def __init__(self, language: str | None = None) -> None:
        self.language = resolve(language or system_language())

    def __call__(self, key: str, **values: object) -> str:
        text = self._text(key)
        return text.format(**values) if values else text

    def plural(self, key: str, count: int, **values: object) -> str:
        """The phrase for `count`, falling back to the `many` form."""
        entry = self._entry(key)
        if isinstance(entry, dict):
            text = (
                entry.get("one" if count == 1 else "many")
                or entry.get("many")
                or entry.get("one")
            )
        else:
            # A translation may skip the forms and spell `{count}` out itself.
            text = key if entry is None else entry
        return text.format(count=count, **values)

    def _entry(self, key: str) -> Entry | None:
        """`key` in this language, else in English, else None."""
        entry = catalog(self.language).get(key)
        return catalog(DEFAULT_LANGUAGE).get(key) if entry is None else entry

    def _text(self, key: str) -> str:
        entry = self._entry(key)
        if isinstance(entry, dict):
            # Addressed without a count, a plural key reads as the many form.
            entry = entry.get("many") or entry.get("one")
        return key if entry is None else entry


def _report(code: str, reference: dict[str, Entry]) -> list[str]:
    """Everything wrong with one locale file, as printable lines."""
    if code not in available_languages():
        return [f"locales/{code}.json not found"]
    entries = catalog(code)
    missing = sorted(key for key in reference if key not in entries)
    unknown = sorted(key for key in entries if key not in reference)
    problems = []
    if missing:
        problems.append(f"missing {len(missing)}: {', '.join(missing)}")
    if unknown:
        problems.append(f"unknown {len(unknown)}: {', '.join(unknown)}")
    return problems


def main(argv: list[str]) -> int:
    """Check locale files against English; non-zero when one is incomplete."""
    reference = catalog(DEFAULT_LANGUAGE)
    if not reference:
        print(f"locales/{DEFAULT_LANGUAGE}.json is missing", file=sys.stderr)
        return 1
    codes = [_normalize(code) for code in argv[1:]] or [
        code for code in available_languages() if code != DEFAULT_LANGUAGE
    ]
    if not codes:
        print(f"no locale files besides {DEFAULT_LANGUAGE}.json; nothing to check")
        return 0
    incomplete = False
    for code in codes:
        problems = _report(code, reference)
        incomplete = incomplete or bool(problems)
        detail = "; ".join(problems) or f"all {len(reference)} keys present"
        print(f"{code}: {len(catalog(code))} keys - {detail}")
    return 1 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
