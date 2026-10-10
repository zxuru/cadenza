"""Every string the UI asks for is one a locale file actually holds.

A key deleted from a locale file does not fail loudly: `Translator` hands back
the key itself, so the window shows `status_preview_playing` where a sentence
belongs. `python i18n.py` only compares the files with each other, so a key
removed from all of them at once passes it - which is exactly how one went
missing here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import i18n

PROJECT = Path(__file__).resolve().parents[1]
# `t("key")`, `t("key", ...)` and `t.plural("key", ...)`. Dynamic keys - the
# `t(f"format_{key}")` family - are not literal, so they are not matched here.
ASKED = re.compile(r"""\bt\(\s*"([A-Za-z0-9_]+)"|\bt\.plural\(\s*"([A-Za-z0-9_]+)\"""")

# Every module that builds UI: a string moved from `main.py` into the styled
# components must stay under this scan, or it leaves it without a sound.
UI_MODULES = ["main.py", "components.py"]


def keys_asked_for(path: Path) -> set[str]:
    found: set[str] = set()
    for match in ASKED.finditer(path.read_text(encoding="utf-8")):
        found.add(match.group(1) or match.group(2))
    return found


@pytest.mark.parametrize("module", UI_MODULES)
def test_every_key_the_ui_asks_for_exists(module):
    asked = keys_asked_for(PROJECT / module)
    if module == "main.py":
        assert asked, "no keys found in main.py: the scan is broken, not the locales"
    catalog = i18n.catalog(i18n.DEFAULT_LANGUAGE)
    missing = sorted(key for key in asked if key not in catalog)
    assert not missing, f"{module} asks for keys no locale file holds: {missing}"


@pytest.mark.parametrize("language", ["es", "en"])
def test_the_languages_agree_on_every_key(language):
    reference = i18n.catalog(i18n.DEFAULT_LANGUAGE)
    entries = i18n.catalog(language)
    assert sorted(entries) == sorted(reference)
