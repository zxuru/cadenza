#!/usr/bin/env python3
"""Freeze this app into a single-file executable for the current platform.

Run it from the project directory with the project virtualenv's interpreter::

    python build.py

There are no options and no prompts: the script detects the host platform,
stages the payloads PyInstaller cannot discover by itself, invokes
PyInstaller, and then verifies that the produced executable really contains
them.

The payloads that need staging are:

* **The Flet desktop client** (a ~40 MB Flutter binary).  ``flet_desktop``
  looks for a client archive inside its own package directory
  (``flet_desktop/app/<artifact>``) and unpacks it into
  ``~/.flet/client/`` on first use; without the archive a frozen app would try
  to download it at runtime (and fail on a machine without network access).
  We re-pack the archive from the client already cached by ``flet_desktop``,
  which keeps it identical to the one Flet itself would download.
* **Deno**, the JavaScript runtime yt-dlp uses to solve YouTube's signature
  challenges.  Without one the stream URLs come back unsigned and the affected
  downloads fail with HTTP 403, so the release binary is fetched from GitHub
  once, verified against the SHA-256 published next to it, and embedded at
  ``jsrt/`` inside the executable.
* **ffmpeg**, shipped inside the ``imageio_ffmpeg`` wheel.  Its PyInstaller
  hook bundles the binary and its hidden import, so the build only needs to
  stay out of the way (see ``HIDDEN_IMPORTS``).

The ``assets/`` directory (UI icons plus the Windows executable icon) is
copied in as data, so the frozen app is fully standalone: it needs no network
access and no system-wide JavaScript runtime to run.

The version the build reports is written into ``_buildinfo.py`` before
PyInstaller runs.  CI passes the release in ``CADENZA_VERSION``; a build made
by hand gets ``<base>+local``, which the updater reads as "leave this one
alone" (see ``version.py`` and ``update.py``).

The executable is written to ``dist/``; PyInstaller's intermediates and the
generated spec file go to ``build/``.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import version

PROJECT_DIR = Path(__file__).resolve().parent
ENTRY_POINT = PROJECT_DIR / "main.py"
APP_NAME = "Cadenza"
ASSETS_DIR = PROJECT_DIR / "assets"
LOCALES_DIR = PROJECT_DIR / "locales"
DIST_DIR = PROJECT_DIR / "dist"
WORK_DIR = PROJECT_DIR / "build"

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# Deno release to embed (https://github.com/denoland/deno/releases).  Pinned
# instead of "latest" so that a build is reproducible; the archive is checked
# against the `sha256sum` GitHub publishes next to it before it is used.
DENO_VERSION = "v2.9.7"
DENO_RELEASE_URL = (
    f"https://github.com/denoland/deno/releases/download/{DENO_VERSION}"
)
# Release asset per (platform, architecture).  `platform.machine()` spells the
# architecture differently on every OS, so it is normalised first.
DENO_ASSETS = {
    ("linux", "x86_64"): "deno-x86_64-unknown-linux-gnu.zip",
    ("linux", "aarch64"): "deno-aarch64-unknown-linux-gnu.zip",
    ("win32", "x86_64"): "deno-x86_64-pc-windows-msvc.zip",
    ("win32", "aarch64"): "deno-aarch64-pc-windows-msvc.zip",
    ("darwin", "x86_64"): "deno-x86_64-apple-darwin.zip",
    ("darwin", "aarch64"): "deno-aarch64-apple-darwin.zip",
}
DENO_ARCHES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}
# Directory inside the bundle the app looks for the runtime in, and the name
# the release gives the binary on this platform.
JSRT_DIR = "jsrt"
DENO_BINARY = "deno.exe" if IS_WINDOWS else "deno"
# Downloads are kept here between builds: PyInstaller's `--clean` only empties
# its own `build/<name>/` subdirectory, so this survives.
DENO_CACHE_DIR = WORK_DIR / "deno"


class BuildError(RuntimeError):
    """A payload could not be prepared, so the build must not go ahead."""

# Packages that resolve modules at runtime (control classes looked up by name,
# the yt-dlp extractor registry) and therefore need their submodules collected
# wholesale instead of relying on static import analysis.
COLLECT_ALL = ("flet", "flet_desktop", "yt_dlp")

# `imageio_ffmpeg.binaries` holds the static ffmpeg executable and is reached
# through `importlib.resources`, so it is never imported directly.  `mutagen`
# is imported inside the tag writers (`metadata.py`) and behind a
# `try:`/`except ImportError:` shim in yt-dlp, so static analysis sees neither:
# a build that drops it still downloads, but every flac download then fails
# while writing the cover art, and no tag lookup can be written.
HIDDEN_IMPORTS = ("imageio_ffmpeg.binaries", "mutagen")

# What a frozen desktop build never needs, left out rather than shipped:
#
# * `transcode`'s in-process converter is for a platform whose wheels carry no
#   ffmpeg - Android - and a desktop executable always has one. PyAV and
#   Pillow come with it. (Pillow also arrives in the build environment as a
#   dependency of flet-cli, which is only there to run the build.)
# * `flet_web` is the browser view's runtime: the FastAPI/uvicorn server and
#   the Pyodide assets, with fastapi, uvicorn, uvloop and pydantic behind it.
#   A frozen build always runs the desktop client. (What lands in the
#   executable still depends a little on what the build environment has
#   installed - `flet` imports those packages for its own web-server path, so
#   a virtualenv carrying the web or cli extras builds a few MB larger than
#   the clean one CI uses.)
EXCLUDED_MODULES = ("av", "PIL", "flet_web")


def stamp_version() -> str:
    """Write the version the frozen app reports; returns it.

    `CADENZA_VERSION` is what CI passes for the release it is about to publish.
    A build made by hand has no version to claim, so it stamps the base one as
    `+local` - the updater replaces a released build, never someone's own.
    """
    requested = os.environ.get("CADENZA_VERSION") or None
    version.stamp(requested)
    return requested or f"{version.BASE_VERSION}{version.LOCAL_SUFFIX}"


def pyinstaller_args(
    client_archive: Path, client_sidecar: Path, deno_binary: Path
) -> list[str]:
    """Return the full PyInstaller command line for this platform."""
    # `--add-data` separates source and destination with the platform's path
    # separator (';' on Windows, ':' elsewhere).
    sep = os.pathsep
    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(ENTRY_POINT),
        "--name",
        APP_NAME,
        "--onefile",
        "--noconfirm",
        "--clean",
        "--noupx",
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(WORK_DIR),
        "--specpath",
        str(WORK_DIR),
    ]
    for package in COLLECT_ALL:
        args += ["--collect-all", package]
    for module in HIDDEN_IMPORTS:
        args += ["--hidden-import", module]
    for module in EXCLUDED_MODULES:
        args += ["--exclude-module", module]
    # The desktop client archive (plus its fingerprint sidecar, which saves
    # every launch from re-hashing 40 MB) lives where `flet_desktop` looks.
    args += ["--add-data", f"{client_archive}{sep}flet_desktop/app"]
    args += ["--add-data", f"{client_sidecar}{sep}flet_desktop/app"]
    # `--add-binary` (unlike `--add-data`) marks the member as an executable,
    # so the extracted `jsrt/deno` keeps its permission bits.
    args += ["--add-binary", f"{deno_binary}{sep}{JSRT_DIR}"]
    # Icons and any other static files the UI loads at runtime.
    args += ["--add-data", f"{ASSETS_DIR}{sep}assets"]
    # One JSON file per UI language; `i18n` reads them from the bundle.
    args += ["--add-data", f"{LOCALES_DIR}{sep}locales"]
    if IS_WINDOWS:
        # Only Windows takes the icon from the executable itself; the window
        # icon comes from the bundled PNG/SVG everywhere.
        args += ["--icon", str(ASSETS_DIR / "icon.ico")]
    if IS_WINDOWS or IS_MACOS:
        # No console window; on Linux the console is kept so that tracebacks
        # are visible when launching the executable from a terminal.
        args.append("--windowed")
    return args


def stage_flet_client(stage_dir: Path) -> tuple[Path, Path]:
    """Re-pack the Flet desktop client as the archive `flet_desktop` expects.

    Returns the archive and its `<archive>.sha256` sidecar.  The archive is
    written deterministically (fixed timestamps, sorted members), so that
    rebuilding does not change its fingerprint and therefore does not make
    every user re-extract the client into a new cache directory.
    """
    import flet_desktop

    client_dir = Path(flet_desktop.ensure_client_cached())
    artifact = flet_desktop.get_artifact_filename()
    archive = stage_dir / artifact

    if artifact.endswith(".zip"):
        write_zip(client_dir, archive)
    else:
        write_tar_gz(client_dir, archive)

    sidecar = Path(f"{archive}.sha256")
    sidecar.write_text(
        f"{sha256_file(archive)} {archive.stat().st_size}\n", encoding="ascii"
    )

    print(f"  client source: {client_dir}")
    print(f"  client archive: {archive.name} ({archive.stat().st_size / 1e6:.1f} MB)")
    return archive, sidecar


def sha256_file(path: Path) -> str:
    """Hex SHA-256 digest of `path`."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def deno_asset() -> str:
    """Name of the Deno release asset built for this platform."""
    arch = DENO_ARCHES.get(platform.machine().lower())
    asset = DENO_ASSETS.get((sys.platform, arch))
    if asset is None:
        raise BuildError(
            f"no pinned Deno build for {sys.platform}/{platform.machine()}"
        )
    return asset


def download(url: str, dest: Path) -> None:
    """Download `url` to `dest`, leaving no half-written file behind."""
    print(f"  downloading {url}")
    partial = dest.with_name(f"{dest.name}.part")
    try:
        with (
            urllib.request.urlopen(url, timeout=120) as response,
            partial.open("wb") as handle,
        ):
            shutil.copyfileobj(response, handle)
        os.replace(partial, dest)
    except OSError:
        partial.unlink(missing_ok=True)
        raise


def deno_digest(asset: str) -> str:
    """SHA-256 GitHub publishes next to the Deno release asset.

    The `.sha256sum` file is written by different tools on different runners:
    a POSIX `sha256sum` line on Linux and macOS, PowerShell's `Get-FileHash`
    output on Windows (`Algorithm : SHA256`, `Hash : <digest>`, `Path : ...`).
    The digest is the only 64-character hex token in either.
    """
    with urllib.request.urlopen(f"{DENO_RELEASE_URL}/{asset}.sha256sum") as response:
        text = response.read().decode("ascii", "replace")
    digest = next(
        (token.lower() for token in re.findall(r"[0-9a-fA-F]{64}", text)), ""
    )
    if not digest:
        raise BuildError(f"no SHA-256 digest in {asset}.sha256sum: {text.strip()!r}")
    return digest


def stage_deno(stage_dir: Path) -> Path:
    """Download, verify and extract the Deno binary into `stage_dir`.

    yt-dlp shells out to a JavaScript runtime to solve YouTube's signature
    challenges; without one the stream URLs come back unsigned and the
    affected downloads fail with HTTP 403.  The release archive holds a single
    self-contained binary, which the app looks for at `jsrt/deno` inside the
    bundle.

    The ~42 MB archive is kept in `build/deno/` between builds so that a
    rebuild does not download it again; a cached copy is re-verified against
    the published digest, and a mismatch aborts the build.
    """
    asset = deno_asset()
    archive = DENO_CACHE_DIR / asset
    try:
        digest = deno_digest(asset)
    except OSError as exc:
        raise BuildError(f"could not read {asset}.sha256sum: {exc}") from exc

    if archive.is_file() and sha256_file(archive) == digest:
        print(f"  deno source: {archive.relative_to(PROJECT_DIR)} (cached)")
    else:
        DENO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        archive.unlink(missing_ok=True)
        try:
            download(f"{DENO_RELEASE_URL}/{asset}", archive)
        except OSError as exc:
            raise BuildError(f"could not download {asset}: {exc}") from exc
        actual = sha256_file(archive)
        if actual != digest:
            archive.unlink(missing_ok=True)
            raise BuildError(
                f"{asset} is not the published file: "
                f"expected sha256 {digest}, got {actual}"
            )

    print(
        f"  deno archive: {archive.name} "
        f"({archive.stat().st_size / 1e6:.1f} MB, {DENO_VERSION})"
    )
    print(f"  deno sha256: {digest} matches the published digest")
    return extract_deno(archive, stage_dir)


def extract_deno(archive: Path, stage_dir: Path) -> Path:
    """Extract the `deno` binary from `archive` into `stage_dir`."""
    with zipfile.ZipFile(archive) as zf:
        member = next(
            (name for name in zf.namelist() if Path(name).name == DENO_BINARY), None
        )
        if member is None:
            raise BuildError(f"{archive.name} contains no {DENO_BINARY}")
        binary = stage_dir / DENO_BINARY
        with zf.open(member) as src, binary.open("wb") as dest:
            shutil.copyfileobj(src, dest)
    if not IS_WINDOWS:
        binary.chmod(0o755)
    print(f"  deno binary: {binary.name} -> {JSRT_DIR}/{DENO_BINARY} in the bundle")
    return binary


def _archive_members(src: Path) -> list[str]:
    """Every path below `src`, relative and sorted, for reproducible archives."""
    return sorted(p.relative_to(src).as_posix() for p in src.rglob("*"))


def write_tar_gz(src: Path, dest: Path) -> None:
    """Write `src` as a gzipped tarball whose members are rooted at `src`."""
    with (
        dest.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for name in _archive_members(src):
            path = src / name
            info = tar.gettarinfo(str(path), arcname=name)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if info.isreg():
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
            else:
                tar.addfile(info)


def write_zip(src: Path, dest: Path) -> None:
    """Write `src` as a zip archive whose members are rooted at `src`."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in _archive_members(src):
            zf.write(src / name, arcname=name)


def artifact_path() -> Path:
    """Location of the built executable (the .app bundle on macOS)."""
    exe = DIST_DIR / f"{APP_NAME}.exe" if IS_WINDOWS else DIST_DIR / APP_NAME
    if IS_MACOS and not exe.exists():
        bundle = DIST_DIR / f"{APP_NAME}.app"
        if bundle.exists():
            return bundle
    return exe


def archive_entry_count(artifact: Path) -> int:
    """How many members the executable's archive holds (a diagnostic)."""
    from PyInstaller.archive.readers import CArchiveReader

    return len(CArchiveReader(str(artifact)).toc)


def packaged_modules(reader: CArchiveReader) -> set[str]:
    """Names of the Python modules the executable carries.

    The executable's own table of contents holds data files, the entry script
    and the PYZ; everything else is compiled into that PYZ, one level down.
    """
    archive = next(str(entry) for entry in reader.toc if str(entry).endswith(".pyz"))
    entries = reader.open_embedded_archive(archive).toc
    return {
        ".".join(entry) if isinstance(entry, tuple) else str(entry) for entry in entries
    }


def missing_payloads(artifact: Path, client_artifact: str) -> list[str]:
    """Names of bundled payloads absent from the executable's archive."""
    from PyInstaller.archive.readers import CArchiveReader

    # The archive keeps whatever separator the build was given, and on Windows
    # that is a backslash: compare on one form or every name looks missing.
    reader = CArchiveReader(str(artifact))
    names = {str(name).replace("\\", "/") for name in reader.toc}
    missing = []
    if f"flet_desktop/app/{client_artifact}" not in names:
        missing.append(f"flet_desktop/app/{client_artifact}")
    if not any(n.startswith("imageio_ffmpeg/binaries/ffmpeg-") for n in names):
        missing.append("imageio_ffmpeg/binaries/ffmpeg-*")
    if not any(n.startswith("yt_dlp/extractor/youtube") for n in names):
        missing.append("yt_dlp/extractor/youtube*")
    if f"{JSRT_DIR}/{DENO_BINARY}" not in names:
        missing.append(f"{JSRT_DIR}/{DENO_BINARY}")
    for icon in ("icon.png", "icon.svg"):
        if f"assets/{icon}" not in names:
            missing.append(f"assets/{icon}")
    for locale in sorted(LOCALES_DIR.glob("*.json")):
        if f"locales/{locale.name}" not in names:
            missing.append(f"locales/{locale.name}")
    # The build's own version, which `version.py` reads back at runtime: a
    # build without it reports the base version, looks older than it is, and
    # offers to update itself forever.
    if version.STAMP_MODULE not in packaged_modules(reader):
        missing.append(version.STAMP_MODULE)
    return missing


def main() -> int:
    if not ENTRY_POINT.is_file():
        print(f"error: {ENTRY_POINT} not found", file=sys.stderr)
        return 1
    try:
        import flet_desktop  # noqa: F401
        import PyInstaller  # noqa: F401
    except ImportError as exc:
        print(
            f"error: {exc.name} is missing from {sys.executable}.\n"
            "Create the virtualenv and install the build requirements first:\n"
            "  python -m venv .venv\n"
            "  .venv/bin/python -m pip install -r requirements.txt pyinstaller",
            file=sys.stderr,
        )
        return 1

    stage_dir = Path(tempfile.mkdtemp(prefix="trpy-build-"))
    try:
        print("Staging payloads...")
        try:
            client_archive, client_sidecar = stage_flet_client(stage_dir)
            deno_binary = stage_deno(stage_dir)
        except BuildError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        args = pyinstaller_args(client_archive, client_sidecar, deno_binary)
        stamped = stamp_version()
        print(f"Version {stamped} written to {version.STAMP_FILE.name}")
        print("Running:", " ".join(shlex.quote(a) for a in args))
        started = time.monotonic()
        subprocess.run(args, cwd=PROJECT_DIR, check=True)
        elapsed = time.monotonic() - started

        artifact = artifact_path()
        if not artifact.exists():
            print(f"error: PyInstaller produced no {artifact}", file=sys.stderr)
            return 1

        missing = missing_payloads(artifact, client_archive.name)
        if missing:
            print(
                f"error: executable is missing {', '.join(missing)} "
                f"(its archive holds {archive_entry_count(artifact)} entries)",
                file=sys.stderr,
            )
            return 1

        print(
            f"\nBuilt {artifact} in {elapsed:.0f}s "
            f"({artifact.stat().st_size / 1e6:.1f} MB)\n"
            f"Version: {stamped}\n"
            f"Bundled: the Flet desktop client, ffmpeg, Deno {DENO_VERSION} "
            f"(the JavaScript runtime yt-dlp needs), assets/ and the locales in "
            f"locales/.\n"
            "First launch unpacks the Flet client into ~/.flet/client/, "
            "which takes a few seconds."
        )
        return 0
    finally:
        shutil.rmtree(stage_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
