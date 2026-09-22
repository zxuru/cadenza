"""UI strings and system-language detection.

Add a language by adding one `TRANSLATIONS` entry; anything missing from it
falls back to English, so a partial translation is always usable.
"""

from __future__ import annotations

import os
import sys

DEFAULT_LANGUAGE = "en"
SUPPORTED_LANGUAGES = ("en", "es")

# Windows LANGID primary-language ids we can serve (see `system_language()`).
_WINDOWS_PRIMARY_LANGUAGES = {0x0A: "es"}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": {
        "badge_ready": "Ready",
        "badge_working": "Working",
        "badge_searching": "Searching",
        "badge_reading": "Reading album",
        "badge_confirm": "Confirm",
        "badge_downloading": "Downloading",
        "badge_done": "Done",
        "badge_failed": "Failed",
        "badge_input": "Waiting for input",
        "badge_results": "Results",
        "folder_none": "no folder chosen",
        "change_folder": "Change folder",
        "format": "Format",
        "format_flac": "FLAC (lossless)",
        "format_mp3": "MP3 (320 kbps)",
        "format_m4a": "M4A (AAC 320 kbps)",
        "format_wav": "WAV (PCM)",
        "query_label": "Song, album or link",
        "query_hint": "e.g. Pink Floyd - The Wall",
        "search": "Search",
        "status_start": "Search for a track or an album to get started.",
        "status_need_input": "Enter a song, an album or a link first.",
        "status_need_folder": "Choose a download folder first.",
        "status_searching": 'Searching for "{query}" ...',
        "status_results_one": "1 result found. Pick the track or album you want.",
        "status_results_many": "{count} results found. Pick the track or album you want.",
        "status_reading_album": 'Reading the track list of "{title}" ...',
        "status_confirm": "Check the track list, then confirm the download.",
        "status_downloading": "downloading {size} ...",
        "status_progress": "{percent}% - {done} of {total} at {speed}/s - ETA {eta}",
        "status_extracting": "extracting {format} with ffmpeg ...",
        "status_saved_track": 'Saved "{title}" as {format} in {path}',
        "status_saved_album": "Saved {count} of {total} tracks to {folder}",
        "status_error": "Error: {message}",
        "subtitle_album": "Album",
        "subtitle_tracks_one": "1 track",
        "subtitle_tracks_many": "{count} tracks",
        "subtitle_track": "Track",
        "tooltip_download_album": "Download album",
        "tooltip_download_track": "Download track",
        "first_run_title": "Where should your music go?",
        "first_run_body": "Downloads are saved in one folder per album. "
        "You can change this later.",
        "first_run_suggested": "Suggested: {path}",
        "first_run_use_suggested": "Use suggested folder",
        "first_run_choose": "Choose folder ...",
        "picker_title": "Choose where downloads are saved",
        "album_folder": "Folder: {path}",
        "album_more": "... and {count} more",
        "cancel": "Cancel",
        "download_album": "Download album",
    },
    "es": {
        "badge_ready": "Listo",
        "badge_working": "Trabajando",
        "badge_searching": "Buscando",
        "badge_reading": "Leyendo álbum",
        "badge_confirm": "Confirmar",
        "badge_downloading": "Descargando",
        "badge_done": "Completado",
        "badge_failed": "Falló",
        "badge_input": "Falta información",
        "badge_results": "Resultados",
        "folder_none": "sin carpeta elegida",
        "change_folder": "Cambiar carpeta",
        "format": "Formato",
        "format_flac": "FLAC (sin pérdida)",
        "format_mp3": "MP3 (320 kbps)",
        "format_m4a": "M4A (AAC 320 kbps)",
        "format_wav": "WAV (PCM)",
        "query_label": "Canción, álbum o enlace",
        "query_hint": "p. ej. Pink Floyd - The Wall",
        "search": "Buscar",
        "status_start": "Busca una canción o un álbum para empezar.",
        "status_need_input": "Escribe primero una canción, un álbum o un enlace.",
        "status_need_folder": "Elige primero una carpeta de descargas.",
        "status_searching": 'Buscando "{query}" ...',
        "status_results_one": "1 resultado. Elige la canción o el álbum que quieres.",
        "status_results_many": "{count} resultados. Elige la canción o el álbum que quieres.",
        "status_reading_album": 'Leyendo la lista de temas de "{title}" ...',
        "status_confirm": "Revisa la lista de temas y confirma la descarga.",
        "status_downloading": "descargando {size} ...",
        "status_progress": "{percent}% - {done} de {total} a {speed}/s - quedan {eta}",
        "status_extracting": "extrayendo {format} con ffmpeg ...",
        "status_saved_track": 'Guardado "{title}" como {format} en {path}',
        "status_saved_album": "Guardados {count} de {total} temas en {folder}",
        "status_error": "Error: {message}",
        "subtitle_album": "Álbum",
        "subtitle_tracks_one": "1 tema",
        "subtitle_tracks_many": "{count} temas",
        "subtitle_track": "Canción",
        "tooltip_download_album": "Descargar álbum",
        "tooltip_download_track": "Descargar canción",
        "first_run_title": "¿Dónde guardamos tu música?",
        "first_run_body": "Cada álbum se guarda en su propia carpeta. "
        "Puedes cambiarlo cuando quieras.",
        "first_run_suggested": "Sugerida: {path}",
        "first_run_use_suggested": "Usar carpeta sugerida",
        "first_run_choose": "Elegir carpeta ...",
        "picker_title": "Elige dónde se guardan las descargas",
        "album_folder": "Carpeta: {path}",
        "album_more": "... y {count} más",
        "cancel": "Cancelar",
        "download_album": "Descargar álbum",
    },
}


def system_language() -> str:
    """Language code the operating system is set to (`en` when unsure)."""
    if sys.platform == "win32":
        code = _windows_language()
        if code:
            return code
    for variable in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(variable)
        if not value or value in ("C", "POSIX"):
            continue
        # "es_ES.UTF-8" / "es-AR" / "es:en" all mean Spanish
        code = value.split(":")[0].split(".")[0].replace("-", "_").split("_")[0]
        if code:
            return code.lower()
    return DEFAULT_LANGUAGE


def _windows_language() -> str | None:
    try:
        import ctypes

        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except Exception:  # noqa: BLE001 - never let locale detection break startup
        return None
    return _WINDOWS_PRIMARY_LANGUAGES.get(langid & 0x3FF, "en")


class Translator:
    """Looks up UI strings, falling back to English for missing entries."""

    def __init__(self, language: str | None = None) -> None:
        requested = (language or system_language()).lower()
        self.language = requested if requested in TRANSLATIONS else DEFAULT_LANGUAGE

    def __call__(self, key: str, **values: object) -> str:
        return self._lookup(key).format(**values) if values else self._lookup(key)

    def plural(self, key: str, count: int, **values: object) -> str:
        """Singular for a count of one, plural otherwise (`<key>_one`/`_many`)."""
        form = f"{key}_{'one' if count == 1 else 'many'}"
        if self._lookup(form, fallback=False) is None:
            form = key
        return self._lookup(form).format(count=count, **values)

    def _lookup(self, key: str, fallback: bool = True) -> str | None:
        text = TRANSLATIONS[self.language].get(key)
        if text is None and fallback:
            text = TRANSLATIONS[DEFAULT_LANGUAGE].get(key)
        return text if text is not None else (key if fallback else None)
