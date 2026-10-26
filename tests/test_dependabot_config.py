"""Comprobaciones de la configuración de Dependabot.

Antes Cadenza no tenía bot de dependencias y las versiones se movían a mano en
tres archivos. Esta prueba cuida que el que entró cubra los tres y que no se
quede apuntando a nada: una entrada con un directorio equivocado no actualiza
nada y no avisa, y un grupo cuyos patrones no matchean ninguna dependencia deja
el bot abriendo un PR por paquete sin que nadie lo note.

Es estática a propósito: no descarga el CLI de nadie ni necesita red ni token.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="pyyaml viene en requirements-dev.txt")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".github" / "dependabot.yml"

# Lo que este repositorio tiene y el bot debe vigilar.
EXPECTED = [("pip", "/"), ("github-actions", "/")]

# Los tres archivos que declaran dependencias de Python.
PIP_FILES = ["requirements.txt", "requirements-dev.txt", "pyproject.toml"]


def _load() -> dict:
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def _declared_requirements() -> set[str]:
    """Nombres de paquete declarados en los tres archivos, en minúsculas."""
    names: set[str] = set()
    pattern = re.compile(r"^\s*[\"']?([A-Za-z0-9][A-Za-z0-9._-]*)")
    for name in PIP_FILES:
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or stripped.startswith("["):
                continue
            match = pattern.match(stripped)
            if match:
                names.add(match.group(1).lower())
    return names


def _as_regex(pattern: str) -> re.Pattern[str]:
    # Los patrones de Dependabot no son expresiones regulares: `*` es comodín.
    escaped = re.escape(pattern).replace(r"\*", ".*")
    return re.compile(f"^{escaped}$")


def test_config_parses_and_declares_version_two():
    config = _load()
    assert config["version"] == 2
    assert config["updates"], "dependabot.yml sin entradas no actualiza nada"


@pytest.mark.parametrize(("ecosystem", "directory"), EXPECTED)
def test_each_ecosystem_is_covered_once_in_its_directory(ecosystem: str, directory: str):
    matches = [
        update
        for update in _load()["updates"]
        if update["package-ecosystem"] == ecosystem and update["directory"] == directory
    ]
    assert len(matches) == 1, f"{ecosystem} en {directory}: {len(matches)} entradas"


def test_every_entry_has_schedule_limit_and_commit_prefix():
    for update in _load()["updates"]:
        label = f"{update['package-ecosystem']} en {update['directory']}"
        assert update["schedule"]["interval"] in {"daily", "weekly"}, label
        assert 0 < update["open-pull-requests-limit"] <= 10, label
        assert re.match(r"^[a-z]+(\([a-z-]+\))?!?:? ?$", update["commit-message"]["prefix"]), label


def test_directories_exist_and_hold_files_the_bot_can_read():
    for update in _load()["updates"]:
        label = f"{update['package-ecosystem']} en {update['directory']}"
        directory = ROOT / update["directory"].lstrip("/")
        assert directory.is_dir(), label

        if update["package-ecosystem"] == "pip":
            for name in PIP_FILES:
                assert (directory / name).is_file(), f"{label}: falta {name}"
        if update["package-ecosystem"] == "github-actions":
            assert (directory / ".github" / "workflows").is_dir(), label


def test_group_patterns_point_at_dependencies_that_exist():
    declared = _declared_requirements()
    for update in _load()["updates"]:
        if update["package-ecosystem"] != "pip":
            continue
        for name, group in (update.get("groups") or {}).items():
            patterns = group.get("patterns") or []
            assert patterns, f"{name}: sin patrones"
            excluded = [_as_regex(p) for p in group.get("exclude-patterns") or []]
            for pattern in patterns:
                if pattern == "*":
                    continue
                matcher = _as_regex(pattern)
                hits = {dep for dep in declared if matcher.match(dep) and not any(x.match(dep) for x in excluded)}
                assert hits, f"{name}: ningún paquete declarado coincide con {pattern}"
