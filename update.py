"""Self-update: fetch the newest release and put it in place of this build.

A release is published for every push to `main` (see
`.github/workflows/build.yml`), so the newest release is always the code that
was pushed last. The feed is GitHub's own `releases/latest`, and the artifact is
the one built for this platform, named as the workflow names it
(`linux-x86_64.tar.gz`, `windows-x86_64.exe`, `macos-arm64.zip`).

Nothing here runs on its own: `main.py` calls it when the user asks for it, and
installs only what that check found.

The repository is public, so the feed and the artifacts are readable without
credentials. A token is still sent when one is configured - `update_token` in
the settings file or `CADENZA_UPDATE_TOKEN` - which is what a private fork or a
mirror would need, and it is dropped as soon as GitHub redirects the download
to its asset storage, which refuses a request carrying two credentials.

Replacing a running build is platform work. Linux and macOS let the file - or
the whole `.app` bundle - be moved over while it runs; Windows refuses to
delete a mapped executable but allows it to be renamed, so the old image steps
aside and `cleanup()` sweeps it up on the next launch. A phone cannot install
an APK from inside the app: `Release.page` is what a browser is sent to
instead, and `installable()` says which of the two this build gets.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import settings
import version

REPO = "zxuru/cadenza"
API = "https://api.github.com"
# Home of the digest list a release carries; verified when it is there.
SUMS_ASSET = "SHA256SUMS"
TOKEN_ENV = "CADENZA_UPDATE_TOKEN"
TIMEOUT = 15
CHUNK = 256 * 1024

# Release asset per (platform, architecture), as the workflow names them.
ASSETS = {
    ("linux", "x86_64"): "linux-x86_64.tar.gz",
    ("linux", "arm64"): "linux-arm64.tar.gz",
    ("win32", "x86_64"): "windows-x86_64.exe",
    ("darwin", "arm64"): "macos-arm64.zip",
    ("darwin", "x86_64"): "macos-x86_64.zip",
}
# Android ships one APK per ABI (`flet build apk --split-per-abi`), named after
# the ABI; the fat package carrying all three is what `--split-per-abi` exists
# to avoid, since two thirds of it never runs on any given phone.
ANDROID_ASSETS = {
    "aarch64": "cadenza-arm64-v8a.apk",
    "arm64": "cadenza-arm64-v8a.apk",
    "armv7l": "cadenza-armeabi-v7a.apk",
    "armv8l": "cadenza-armeabi-v7a.apk",
    "x86_64": "cadenza-x86_64.apk",
}
# `platform.machine()` spells the architecture differently on every OS.
ARCHITECTURES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "arm64": "arm64",
    "aarch64": "arm64",
}

Progress = Callable[[int, int], None]


class UpdateError(RuntimeError):
    """The update could not be fetched, verified or put in place."""


@dataclass(frozen=True)
class Asset:
    """One file attached to a release."""

    name: str
    # API url, not `browser_download_url`: it is the one that accepts a token,
    # and it redirects to the same signed url the browser would get.
    url: str
    size: int
    # What to hand a browser instead: a phone installs an APK from outside the
    # app, and a plain link starts the download in one tap.
    browser: str = ""


@dataclass(frozen=True)
class Release:
    """A published release, as much of it as the updater uses."""

    version: str
    tag: str
    # Page a browser can open: the way to update where nothing is installed.
    page: str
    assets: dict[str, Asset] = field(default_factory=dict)

    def asset(self, name: str) -> Asset | None:
        return self.assets.get(name)


@dataclass(frozen=True)
class Result:
    """What a check found: a newer release, or why there is none."""

    release: Release | None = None
    error: str | None = None
    # The feed answered 404 and no token was configured: a private repository
    # looks exactly like this.
    needs_token: bool = False


# ------------------------------------------------------------------------- feed


class _Redirect(urllib.request.HTTPRedirectHandler):
    """Follow GitHub's redirect to its asset storage without the token.

    Asset bytes are served from a signed URL, and the signature is the whole
    credential there: a request carrying the token as well is refused. The
    token therefore stops at github.com.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        followed = super().redirect_request(req, fp, code, msg, headers, newurl)
        if followed is not None:
            if urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(
                req.full_url
            ).netloc:
                followed.remove_header("Authorization")
        return followed


_opener = urllib.request.build_opener(_Redirect)


def _request(url: str, token: str | None, accept: str) -> urllib.request.Request:
    headers = {"User-Agent": f"Cadenza/{version.current()}", "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def env_token() -> str | None:
    """Token from the environment, for a machine that has no config file yet."""
    return os.environ.get(TOKEN_ENV) or None


def latest(token: str | None = None) -> Release:
    """The newest published release; raises when the feed cannot be read."""
    url = f"{API}/repos/{REPO}/releases/latest"
    with _opener.open(
        _request(url, token, "application/vnd.github+json"), timeout=TIMEOUT
    ) as response:
        return _release(json.load(response))


def _release(payload: dict) -> Release:
    """One release out of GitHub's JSON, with the fields the updater uses."""
    assets: dict[str, Asset] = {}
    for raw in payload.get("assets") or []:
        name, url = str(raw.get("name") or ""), str(raw.get("url") or "")
        if name and url:
            assets[name] = Asset(
                name,
                url,
                int(raw.get("size") or 0),
                str(raw.get("browser_download_url") or ""),
            )
    tag = str(payload.get("tag_name") or "")
    return Release(
        version=version.release(tag),
        tag=tag,
        page=str(payload.get("html_url") or ""),
        assets=assets,
    )


def check(token: str | None = None) -> Result:
    """The newest release when it is newer than this build, else why it is not."""
    try:
        newest = latest(token)
    except urllib.error.HTTPError as err:
        return Result(
            error=f"HTTP {err.code}", needs_token=err.code == 404 and not token
        )
    except (urllib.error.URLError, OSError, ValueError) as err:
        return Result(error=str(err) or type(err).__name__)
    if not version.is_newer(newest.version, version.current()):
        return Result()
    return Result(release=newest)


# ----------------------------------------------------------------------- local


def installable() -> bool:
    """True when this build can replace itself: a frozen desktop app.

    A source checkout has no installed build to replace, and a phone cannot
    install an APK from inside the app - `Release.page` is the way there.
    """
    return bool(getattr(sys, "frozen", False)) and not settings.is_mobile()


def executable_path() -> Path:
    """The file to replace: where the running build's executable lives.

    Resolved, so a build reached through a symlink in `~/.local/bin` is
    replaced where it really lives and the link keeps pointing at it.
    """
    return Path(sys.executable).resolve()


def app_bundle(exe: Path | None = None) -> Path | None:
    """The `.app` directory holding this executable, on macOS."""
    for parent in (exe or executable_path()).parents:
        if parent.suffix == ".app":
            return parent
    return None


def architecture() -> str:
    """`platform.machine()` under the name the release assets use."""
    return ARCHITECTURES.get(platform.machine().lower(), "")


def asset_name() -> str | None:
    """The release asset built for this machine, or None when there is none."""
    if settings.is_mobile():
        # Only Android has a build; an iPhone could not run one anyway.
        return ANDROID_ASSETS.get(platform.machine().lower()) if is_android() else None
    if sys.platform == "win32":
        # Windows on ARM is served by the x64 build, which it runs emulated:
        # the bundled ffmpeg publishes no win_arm64 wheel.
        return ASSETS[("win32", "x86_64")]
    return ASSETS.get((sys.platform, architecture()))


def download_url(release: Release) -> str:
    """What to hand a browser: this machine's artifact, else the release page.

    A phone cannot install an APK from inside the app, so this is the whole
    update path there - and with one APK per ABI, the link is the one for the
    ABI the device actually runs.
    """
    name = asset_name()
    asset = release.asset(name) if name is not None else None
    if asset is not None and asset.browser:
        return asset.browser
    return release.page


def is_android() -> bool:
    """True on Android, which the Flet runtime names in the environment."""
    return os.environ.get("FLET_PLATFORM") == "android" or str(sys.platform).startswith(
        "android"
    )


# -------------------------------------------------------------------- download


def fetch(
    asset: Asset, token: str | None, into: Path, progress: Progress | None = None
) -> Path:
    """Download `asset` into `into`; no half-written file is left behind."""
    into.mkdir(parents=True, exist_ok=True)
    target = into / asset.name
    partial = target.with_name(f"{target.name}.part")
    try:
        with _opener.open(
            _request(asset.url, token, "application/octet-stream"), timeout=TIMEOUT
        ) as response:
            expected = int(response.headers.get("Content-Length") or 0) or asset.size
            written = 0
            with partial.open("wb") as keep:
                while block := response.read(CHUNK):
                    keep.write(block)
                    written += len(block)
                    if progress is not None:
                        progress(written, expected)
        if expected and written != expected:
            raise UpdateError(
                f"{asset.name} is {written} bytes, {expected} were expected"
            )
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


def digest(path: Path) -> str:
    """Hex SHA-256 of a file."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def published_digest(path: Path, name: str) -> str | None:
    """Digest `name` in a `sha256sum`-style list, or None when it is not listed."""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            return parts[0].lower()
    return None


def verify(archive: Path, release: Release, token: str | None, into: Path) -> None:
    """Check `archive` against the digest list the release carries.

    A download interrupted in a way the connection does not notice would
    otherwise be installed as a broken build.
    """
    listing = release.asset(SUMS_ASSET)
    if listing is None:
        return  # a release published before the list was: nothing to check with
    published = published_digest(fetch(listing, token, into), archive.name)
    if published is None:
        raise UpdateError(f"{SUMS_ASSET} has no entry for {archive.name}")
    if published != digest(archive):
        raise UpdateError(f"{archive.name} does not match {SUMS_ASSET}")


def apply(
    release: Release, token: str | None = None, progress: Progress | None = None
) -> Path:
    """Install the release's artifact for this machine; returns what to relaunch."""
    name = asset_name()
    if name is None:
        machine = f"{sys.platform}/{platform.machine()}"
        raise UpdateError(f"no build is published for {machine}")
    asset = release.asset(name)
    if asset is None:
        raise UpdateError(f"release {release.tag} has no {name}")
    with tempfile.TemporaryDirectory(prefix="cadenza-update-") as staging:
        archive = fetch(asset, token, Path(staging), progress)
        verify(archive, release, token, Path(staging))
        return install(archive)


# --------------------------------------------------------------------- install


def install(archive: Path, exe: Path | None = None) -> Path:
    """Put `archive` in place of this build; returns the executable to relaunch.

    Linux and Windows ship one file, macOS a directory around one (the `.app`
    bundle): either way what was built is replaced whole.
    """
    exe = exe or executable_path()
    if (root := app_bundle(exe)) is not None:
        return _install_bundle(archive, root, exe.name)
    return _install_file(archive, exe)


def _install_file(archive: Path, exe: Path) -> Path:
    """Replace the executable, keeping the running image usable until it exits."""
    staged = exe.with_name(f"{exe.name}.new")
    try:
        _write_executable(archive, staged)
        if os.name == "nt":
            # Windows will not delete a mapped executable, but it will rename
            # it: the old image steps aside for the new one, and `cleanup()`
            # removes it on the next launch, once nothing maps it any more.
            os.replace(exe, exe.with_name(f"{exe.name}.{os.getpid()}.old"))
        # On Linux and macOS this replaces the directory entry while the
        # running image keeps the file it was started from.
        os.replace(staged, exe)
    finally:
        staged.unlink(missing_ok=True)
    return exe


def _install_bundle(archive: Path, app: Path, exe_name: str) -> Path:
    """Replace the whole `.app` directory, keeping the running one aside."""
    with tempfile.TemporaryDirectory(prefix="cadenza-update-") as unpacked:
        with zipfile.ZipFile(archive) as zipped:
            zipped.extractall(unpacked)
        # `ditto --keepParent` puts the bundle at the root of the archive.
        fresh = next(
            (
                path
                for path in sorted(Path(unpacked).iterdir())
                if path.suffix == ".app"
            ),
            None,
        )
        if fresh is None:
            raise UpdateError(f"{archive.name} holds no .app bundle")
        displaced = app.with_name(f"{app.name}.{os.getpid()}.old")
        os.replace(app, displaced)
        try:
            shutil.move(str(fresh), str(app))
        except OSError:
            os.replace(displaced, app)  # the working copy goes back
            raise
        shutil.rmtree(displaced, ignore_errors=True)
    return app / "Contents" / "MacOS" / exe_name


def _write_executable(archive: Path, dest: Path) -> None:
    """Write the executable inside `archive` to `dest`, executable bit and all."""
    if archive.suffix == ".exe":
        shutil.copyfile(archive, dest)
        os.chmod(dest, 0o755)
        return
    with tarfile.open(archive, "r:*") as tar:
        member = _payload(tar)
        source = tar.extractfile(member)
        if source is None:
            raise UpdateError(f"{archive.name} holds no file at {member.name}")
        with source, dest.open("wb") as keep:
            shutil.copyfileobj(source, keep)
        # The mode the workflow packed is what a fresh download would have;
        # the extraction above does not carry it.
        os.chmod(dest, 0o755)


def _payload(tar: tarfile.TarFile) -> tarfile.TarInfo:
    """The executable in a packaged build: the one file it holds."""
    members = [member for member in tar.getmembers() if member.isfile()]
    if not members:
        raise UpdateError("the archive holds no file")
    return max(members, key=lambda member: member.size)


# --------------------------------------------------------------------- restart


def relaunch(exe: Path, version_note: str) -> None:
    """Start the freshly installed build; the caller closes the window after.

    Detached, because it has to outlive this process: Flet's window belongs to
    a client process of our own, and it is this process that owns it.
    """
    command = [str(exe), "--updated", version_note]
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(command, close_fds=True, creationflags=flags)
    else:
        subprocess.Popen(command, close_fds=True, start_new_session=True)


def cleanup(exe: Path | None = None) -> None:
    """Remove what an update left behind, as far as the system lets us.

    A displaced executable is still mapped by the process that ran it, so on
    Windows it can only go on the next launch - which is this call.
    """
    exe = exe or executable_path()
    root = app_bundle(exe)
    parent = root.parent if root is not None else exe.parent
    pattern = f"{root.name if root is not None else exe.name}.*.old"
    for leftover in parent.glob(pattern):
        try:
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink()
        except OSError:
            pass  # still in use: the launch after that will get it
