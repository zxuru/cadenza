# Building Cadenza

Cadenza ships as four artifacts, built by two tools:

| Artifact | Built with | Build host | Ends up as |
| --- | --- | --- | --- |
| Linux x86_64, arm64 | `python build.py` (PyInstaller) | Linux | `dist/Cadenza` |
| Windows x64, arm64 | `python build.py` (PyInstaller) | Windows | `dist\Cadenza.exe` |
| macOS arm64, x86_64 | `python build.py` (PyInstaller) | macOS | `dist/Cadenza.app` |
| Android, one APK per ABI | `flet build apk --split-per-abi` | any of the three | `build/apk/cadenza-<abi>.apk` |

Neither tool cross-compiles - a desktop executable has to be frozen on the
platform it runs on, and `flet build` refuses a target its host cannot produce -
so [Automatic builds and releases](#automatic-builds-and-releases) builds all of them at once and the
commands below are for doing one by hand. iOS and the web are not targets at
all: [What is not built, and why](#what-is-not-built-and-why).

`python build.py` freezes the desktop app - no options, no prompts. The
executable lands in `dist/`, PyInstaller's intermediates and the generated spec
file in `build/`. What ends up inside it is listed under
[What gets bundled](#what-gets-bundled-and-why).

## Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pyinstaller
.venv/bin/python build.py
```

Produces `dist/Cadenza` — a single ELF file (69 MB on Ubuntu 26.04, a minute to
build; CI's Python makes the released one 85 MB, see
[What it weighs](#what-it-weighs-and-why)). Nothing
else has to be installed on the target machine: Python, ffmpeg, the Flet client
and a JavaScript runtime are all inside.

## Windows

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
.venv\Scripts\python build.py
```

Produces `dist\Cadenza.exe` — a single file with no console window
(`--windowed`), carrying the application icon from `assets/icon.ico`. Python
3.12 or newer; PyInstaller builds with the interpreter it is installed in, so
always invoke it through the virtualenv.

## macOS

Same commands as Linux. The build applies `--windowed` as well, so expect
`dist/Cadenza.app` — a macOS *bundle directory*, not a single file. It is
built and self-tested by [Automatic builds and releases](#automatic-builds-and-releases) on a macOS
runner; the one thing still missing is the icon, which wants an `.icns` (the
repository carries `.ico` and `.png`), so the bundle shows the PyInstaller
default in the dock.

## Android

```bash
python3 -m venv .venv
.venv/bin/python -m pip install "flet[all]==1.0.0"
.venv/bin/flet build apk --yes
```

Produces `build/apk/cadenza-<abi>.apk`: a release APK per architecture
(`arm64-v8a`, `armeabi-v7a`, `x86_64`), signed with the debug key. `--split-per-abi`
is not optional here in practice: the three sets of native libraries — Flutter's,
Python's and ffmpeg's — are what a package carrying all of them is made of, and
two thirds of that never runs on any given phone. The three APKs are 91, 75 and
97 MB; the fat one was 230 MB. `--android-signing-key-store` (with the three key
flags, or `[tool.flet.android.signing]` in `pyproject.toml`) signs with a real
key instead of the debug one.

The first run installs what it needs by itself — Flutter 3.44.8 into
`~/flutter`, a JDK 17 into `~/java`, the Android SDK into `~/Android/sdk`, some
4 GB in all — and `--yes` answers those prompts. Later builds reuse all three
and take a few minutes.

### What the APK carries

| Payload | How |
| --- | --- |
| The app | `assets/app.zip`: every module compiled (`main.pyc`, `engine.pyc`, `transcode.pyc`, `metadata.pyc`, `bundle.pyc`, `i18n.pyc`, `settings.pyc`), plus `assets/` and `locales/` |
| Python 3.14.7 | `libpython3.14.so` per ABI, with `stdlib.zip` and `sitepackages.zip` |
| `flet`, `yt-dlp`, `mutagen` | `[project.dependencies]` in `pyproject.toml`, installed by `flet build` from PyPI |
| `av`, `pillow` | `[tool.flet.android.dependencies]`, from Flet's own wheel index (`pypi.flet.dev`), which is where the Android builds of binary packages live |
| ffmpeg's libraries | `libavcodec.so` and the rest, in `lib/<abi>/`, pulled in behind `av` |

### Why there is no ffmpeg executable, and what that costs

Since Android 10 an app whose `targetSdk` is 29 or higher may not `execve()` a
file it ships, and Flet's Android build ships ffmpeg's *libraries*, not its
CLI. So the APK converts audio in the same process instead of in a subprocess:
`transcode.py` drives those libraries through PyAV, and it is used on any
machine that has no ffmpeg binary (a desktop without one falls back to it too).
What that changes:

* The format dropdown offers what the bundled encoder can produce: **FLAC, M4A
  and WAV**. MP3 is absent, because `libmp3lame` is the only MP3 encoder ffmpeg
  has and Flet's Android build ships without it. `engine.available_formats()`
  is what the dropdown asks.
* Tags and cover art go in with mutagen and Pillow rather than through yt-dlp's
  postprocessors: the video's own tags and its cropped frame first, and the
  music database's metadata on top where there is a match — the same lookup the
  desktop build does.
* YouTube has no JavaScript runtime to call on a phone, so extraction runs in
  yt-dlp's degraded mode: the common tracks still download; some formats may be
  missing.

### Running it

Android gives an app one directory it may write to. On the first run Cadenza
takes that one — `Android/data/cl.zxuru.cadenza/files` on the primary storage —
and shows it in the header; "Change folder" can move it wherever the folder
picker can reach. Nothing else is asked of the user: every Flet APK already
declares `INTERNET`.

Sideload with `adb install -r build/apk/cadenza-arm64-v8a.apk` (that is the one
every phone from the last few years wants), or copy the file to the phone and
open it. The debug key means the system may warn about an unknown
developer; it installs all the same.

To try it without a phone — on a machine with `/dev/kvm` — install the emulator
and one system image, create an AVD, and launch the app:

```bash
export ANDROID_HOME=$HOME/Android/sdk JAVA_HOME=$HOME/java/17.0.13+11
$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager --install \
    emulator "system-images;android-35;default;x86_64"
$ANDROID_HOME/cmdline-tools/latest/bin/avdmanager create avd -n cadenza \
    -k "system-images;android-35;default;x86_64" -d pixel_6
$ANDROID_HOME/emulator/emulator -avd cadenza -no-window -no-audio &
$ANDROID_HOME/platform-tools/adb wait-for-device
$ANDROID_HOME/platform-tools/adb install -r build/apk/cadenza.apk
$ANDROID_HOME/platform-tools/adb shell am start -n cl.zxuru.cadenza/.MainActivity
```

## Verifying a build

From a directory other than the project (so nothing resolves from the sources),
run the app's headless self-test — it exercises the bundled ffmpeg, the
JavaScript runtime, the yt-dlp extractors, the network path and the tags and
cover art written into the file:

```bash
mkdir -p /tmp/frozen-selftest && cd /tmp/frozen-selftest
/absolute/path/to/dist/Cadenza --selftest     # exits 0 when everything works
```

A real run, ~25 s:

```
version      1.0.57
ffmpeg       /tmp/_MEI0003d59fP62kNf/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2
js runtimes  quickjs
extractors   1751
mutagen      1.48.1
search       2 result(s): Sneaky Snitch
download     Sneaky Snitch.flac (22.3 MiB)
tags         artist=Kevin MacLeod · album=Mystery · genre=Electronic · date=2014-12-27
cover art    mjpeg 1400x1400
```

The `version` line is what the build stamped into itself
([Automatic builds and releases](#automatic-builds-and-releases)); the workflow
compares it against the release it is publishing.

The `extractors` line is the check on the `--collect-submodules yt_dlp` payload: yt-dlp
builds that registry by scanning its own package, so a bundle that ships the
package without its extractors still starts and only fails at the first search.

The `tags` line is also the check that the tag lookup ran: that upload's own
tags are `album=Robotik Party`, `genre=Music` and `date=2023-11-28`, so
Mystery, Electronic and 2014-12-27 can only have come from the music database
(see [Tags and cover art](#tags-and-cover-art)) — a build whose lookup fails
reports the video's own values instead.

The `cover art` line is either the square cover the database served or the
video thumbnail cropped to 4:3, so a build that embeds YouTube's 16:9 frame
unmodified fails the self-test instead of shipping covers that look stretched.

YouTube will not serve a CI runner: every video answers "Sign in to confirm
you're not a bot", and often the search does too, which says nothing about the
build. Automatic builds therefore run the same check with `--no-download`,
which leaves out everything that needs YouTube to cooperate and checks what any
machine can: the bundled ffmpeg, the JavaScript runtime, the yt-dlp extractor
registry and mutagen.

```bash
/absolute/path/to/dist/Cadenza --selftest --no-download
```

To prove the executable really is standalone, run the full check with a
stripped environment: if `js runtimes` still says `quickjs`, that QuickJS came
out of the bundle, not from the machine, and the search and download that follow
were solved with it:

```bash
env -i PATH=/usr/bin:/bin HOME=/tmp/selftest-home \
  /absolute/path/to/dist/Cadenza --selftest
```

Finally, launch it without arguments to confirm the window opens (the Flet
client process appears ~1.5 s after start, the window ~2.5 s) and that the
first-run dialog asks where to put your music.

The same executable says what the **Check for updates** button in its footer
would find, without opening a window:

```bash
/absolute/path/to/dist/Cadenza --check-updates
```

```
version      1.0.57
asset        linux-x86_64.tar.gz
token        given
release      none newer
```

It downloads nothing; a release line other than `none newer` is what the
button would then install. Like the self-test it falls back to a file
(`updatecheck.txt`) when the build has no console, and it exits non-zero only
when the feed could not be read.

The self-test is the *desktop* build's check: it runs ffmpeg, which an Android
build has not got, and a phone has no console to run it from. An APK is
verified by running it — see [Running it](#running-it).

## Automatic builds and releases

`.github/workflows/build.yml` builds every target on the platform that can
build it, runs the self-test on each desktop artifact before uploading it, and
then **publishes every artifact as a GitHub release** — there is no separate
release step to run by hand:

| Job | Runner | Release asset |
| --- | --- | --- |
| desktop (linux-x86_64) | `ubuntu-latest` | `linux-x86_64.tar.gz` |
| desktop (linux-arm64) | `ubuntu-24.04-arm` | `linux-arm64.tar.gz` |
| desktop (windows-x86_64) | `windows-latest` | `windows-x86_64.exe` |
| desktop (macos-arm64) | `macos-latest` | `macos-arm64.zip` |
| desktop (macos-x86_64) | `macos-15-intel` | `macos-x86_64.zip` |
| android | `ubuntu-latest` | `cadenza-arm64-v8a.apk`, `cadenza-armeabi-v7a.apk`, `cadenza-x86_64.apk` |

The names are what `update.py` looks for, so the same table is written down
there; a file renamed on one side stops the app from finding it.

Windows on ARM is the one architecture left out: `imageio-ffmpeg`, which
supplies the bundled ffmpeg, publishes no `win_arm64` wheel, so the x64 build
is the one to hand out there — Windows runs it through its own emulation.

It runs on a push to `main`, on a tag, and on demand (**Actions → build → Run
workflow**). The `version` job decides the number:

| Trigger | Version | Example |
| --- | --- | --- |
| push to `main` | `<major>.<minor>.<run number>`, from `BASE_VERSION` in `version.py` | `v1.0.57` |
| push of a `v*` tag | the tag itself | `v1.2.3` |

The patch is the Actions run number, which only grows: an installed copy can
always tell a newer release from an older one, which is the whole basis of
[Updating](#updating). A build that only rewrites prose builds nothing —
`paths-ignore` leaves `**/*.md` and `packaging/` out — so nothing is released
for it either.

The version is stamped into `_buildinfo.py` before the build runs
(`CADENZA_VERSION` for `build.py`, `version.py --stamp` for the APK), and each
desktop artifact proves it: the self-test prints a `version` line and the
workflow fails if it does not match the release. An executable that lost its
stamp would report the base version, look older than it is, and offer to
update itself forever.

Bumping `BASE_VERSION` in `version.py` is how a release line moves on (a new
minor or major); everything else is automatic.

```bash
git tag v1.2.3 && git push origin v1.2.3   # only for a version worth naming
```

Every run is from scratch except the Android toolchain: Flutter, the JDK and
the Android SDK are cached under a key that includes `pyproject.toml`, so they
are downloaded again only when the Android side of the app changes. Nothing has
to be built locally for a release; the two commands above are for working on
one platform at a time.

The release carries a `SHA256SUMS` file next to the artifacts, and `update.py`
checks a download against it before installing anything.

## Updating

The app updates when asked, never on its own: the footer has the version it is
running and a **Check for updates** button, and nothing is fetched before that
button is pressed.

* **Desktop (frozen build).** The check reads GitHub's newest release; if it is
  newer, the artifact for this platform is downloaded, checked against
  `SHA256SUMS`, put in place of the running executable (the whole `.app` bundle
  on macOS) and the app restarts into it. It waits first if a download is in
  progress — `update.py` never restarts the app out from under one.
* **Android.** An app cannot install an APK from inside itself, so the footer
  offers the release page and the browser downloads it; the system then does
  what installing an APK always asks for.
* **From a checkout** (`python main.py`, or a build made by hand) nothing is
  replaced: the button says so instead.

### The repository, and the token it does not need

`zxuru/cadenza` is public, so the feed and the artifacts need no credentials:
the button works out of the box, on any machine.

`update.py` still sends a token when it finds one, because a private fork or a
mirror would need it — from `update_token` in the config file, or from
`CADENZA_UPDATE_TOKEN`:

```json
{
  "update_token": "github_pat_..."
}
```

A fine-grained token with **Contents: read** on that repository is enough, and
it stays on the machine it was pasted on — it is not part of any build. Without
one, a private repository answers 404 and the check reports *a private
repository needs `update_token`* instead of pretending the app is up to date.

When there is a token it is dropped as soon as GitHub redirects the download to
its asset storage: the redirect is signed, and that signature is the whole
credential there (`update._Redirect`).

## What is not built, and why

**iOS.** `flet build ipa` needs an Apple Developer account and a signing
certificate to produce an `.ipa` at all — without them it stops at an
`.xcarchive`. Signing aside, the app would not work there: iOS forbids starting
a bundled binary *and* loading your own dynamic libraries, so neither an ffmpeg
CLI nor PyAV's libffmpeg can run, and every conversion this app does would
fail.

**Web.** `flet build web` runs the app under Pyodide, which has no processes
(`subprocess` raises `OSError: [Errno 138]`) and an in-memory file system that
is gone on reload. yt-dlp could not download, and nothing could convert.

## Running the app

| | Linux | Windows | macOS | Android |
| --- | --- | --- | --- | --- |
| OS | Ubuntu 24.04 or newer (older distros need a rebuild on that distro) | 64-bit Windows 10/11 | macOS 12 or newer | Android 7 or newer |
| Session | X11 or Wayland desktop | any | any | any |
| Installed dependencies | none | none | none | none |
| Network | required for search, download and the tag lookup | required | required | required |
| Disk | ~85 MB for the executable, plus your music | ~98 MB, plus your music | ~96 MB, plus your music | ~91 MB for the APK, plus your music |
| Admin rights | not needed | not needed | not needed | not needed |

The executable carries Python, ffmpeg, the Flet desktop client, a QuickJS
JavaScript runtime and the UI assets. It prefers a JavaScript runtime already
installed (`deno`, `node`, `bun` or `quickjs`/`qjs` on `PATH`) and falls back to
the bundled QuickJS, so nothing has to be set up either way. What the APK
carries instead — and why it has no ffmpeg to run — is under
[Android](#android).

First launch takes a few seconds longer: the executable unpacks the Flet client
into `~/.flet/client/` and reuses it afterwards. Settings live in
`~/.config/Cadenza/config.json` (`%APPDATA%\Cadenza\config.json` on Windows,
`~/Library/Application Support/Cadenza/config.json` on macOS, and the app's own
data directory on Android); the music goes wherever the first-run dialog points
— or, on Android, where the system lets the app write.

The executables are unsigned, so Windows SmartScreen may warn once
("More info" → "Run anyway"); on Linux you may need `chmod +x` after copying
the file from somewhere that dropped the bit.

### Linux desktop integration (optional)

A bare executable has no icon in the dock: the shell matches one through a
desktop entry plus an icon installed in the theme. To get both:

```bash
install -Dm755 dist/Cadenza                          ~/.local/bin/Cadenza
install -Dm644 assets/icon.png                       ~/.local/share/icons/hicolor/512x512/apps/cadenza.png
install -Dm644 packaging/cadenza.desktop             ~/.local/share/applications/cadenza.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
```

The app sets `FLET_APP_ID=cadenza`, which is the id the window reports, so the
desktop entry (whose `StartupWMClass` is `cadenza`) matches and the icon is
used. On Windows the icon is embedded in the `.exe` and needs no such setup.

## Tags and cover art

YouTube hands over a video, so the tags yt-dlp can write on its own are the
channel name, the upload date and the category — `genre=People & Blogs` on a
plain re-upload. Search results are therefore ranked with the music uploads
first (YouTube's own order is kept inside each group): the same recording is
usually on YouTube both as the release and as a re-upload by an unrelated
channel, and only one of the two carries the release's metadata. Once the audio
is on disk, `metadata.py` looks the track up in a music database and rewrites
the tags and the picture with mutagen:

1. **iTunes Search** — one request already carries the album, the album artist,
   the track and disc number, the genre and a square cover.
2. **Deezer** — the fallback, for what iTunes does not know or stops answering
   once its rate limit bites. Its album endpoint adds the genres (asked for in
   English with `Accept-Language`, since it localizes them) and a 1000×1000
   cover.

A candidate is accepted only when its title matches the video's — exactly, or
one containing the other, which is how an uploader's decoration
(`Sneaky Snitch (Kevin MacLeod) - Background Music (HD)`) matches the plain
title — and its length is within 10 s of the video's. Among the accepted ones,
the exact title, the words the artist names share and the closest length
decide.

Both databases are public: no key, no account, nothing to configure. The lookup
is best effort — a database that is down, slow or simply does not know the
track leaves the tags yt-dlp wrote in place and the download still succeeds.
It runs for the containers that can hold a picture (flac, mp3, m4a); a WAV
keeps the tags yt-dlp wrote.

For an album download the release the user picked keeps its album name, its
album artist and its year, and every track keeps the position it has in it: a
match can land on a different pressing of the same recording. The genre and the
cover always come from the database. The picture yt-dlp embeds on its own is
the video thumbnail cropped to 4:3 (the shape the player frames covers in, see
`ART_CROP_FILTER`); a database cover replaces it with the square artwork,
uncropped.

## Languages

The UI follows the system language: it checks Windows' user UI language, then
`LC_ALL`/`LC_MESSAGES`/`LANG`/`LANGUAGE`, and falls back to English. English and
Spanish ship today.

Each language is one file, `locales/<code>.json`, and dropping a file in is the
whole registration step — no list to update, in the code or in this document:

```json
{
  "search": "Zoeken",
  "status_results": {
    "one": "1 resultaat gevonden.",
    "many": "{count} resultaten gevonden."
  }
}
```

Keys are the ones `main.py` renders: a plain string is read with `t("search")`,
the plural object with `t.plural("status_results", count)` (its `{count}` comes
from the argument, other `{name}` placeholders from keyword arguments). Anything
a file leaves out falls back to English, so a partial translation is usable
while it grows; neither call ever raises on an unknown key.

```bash
.venv/bin/python i18n.py            # every language against English
.venv/bin/python i18n.py pt-BR      # just one; lists missing and unknown keys
```

The name is ISO 639-1, with a region only when the translation differs from the
base language (`pt-BR.json`): a request for `pt` or `pt-PT` is served by it, and
a regional request with no file of its own (`es-AR`) falls back to `es.json`.
A file that is missing, unreadable or malformed is reported on stderr and
skipped, which leaves that language in English instead of breaking startup.

## What it weighs, and why

The released Linux build is 85 MB, and 69 MB when built on Ubuntu 26.04 - the
difference is the interpreter CI freezes it with. Before the JavaScript runtime
was swapped and the payload was pruned it was 160 MB (131 MB to download). What
is left is almost entirely three things, and every one of them is the feature it
looks like:

| Payload | In the executable | Unpacked | Why it is there |
| --- | --- | --- | --- |
| ffmpeg (from `imageio-ffmpeg`) | 29 MB | 80 MB | Converts to FLAC/MP3/M4A/WAV and crops the video frame that stands in for cover art. A machine that has its own `ffmpeg` on `PATH` still carries this one. |
| Flet desktop client | 16 MB | 16 MB | The Flutter window. `flet_desktop` unpacks it into `~/.flet/client/` on the first launch. |
| Python modules (the PYZ) | 12 MB | 12 MB | The app plus yt-dlp's extractor registry, which is why it is not 2 MB. |
| QuickJS | 1 MB | 2.6 MB | yt-dlp runs YouTube's player JS in it to solve the signature challenges. Deno, yt-dlp's default, is 96 MB for the same job. |
| CPython and its extension modules | 4 MB | 11 MB | The interpreter the app is frozen with. |
| OpenSSL, SQLite, zlib, assets, locales | 6 MB | 13 MB | HTTPS, and the UI's own files. |

The numbers move with the interpreter, and that is the whole of the difference
between the two sizes above: CI builds with the Python from
`actions/setup-python` (a python-build-standalone build, which carries a shared
`libpython` and every extension module), where Ubuntu's own `python3.14` is
leaner.

What is *not* in there is worth as much as what is. `flet.cli` and everything it
drags in (rich, pygments, cookiecutter, watchdog, markdown-it, httpx), the web
view's FastAPI stack, PyAV and Pillow are all excluded — see
`EXCLUDED_MODULES`. And the Flet client rides as the single archive
`flet_desktop` looks for: `flet-cli`'s PyInstaller hook would *also* add the
unpacked client from `~/.flet/client/`, so a build run on a machine that has
`flet-cli` installed shipped the client twice. `packaging/pyinstaller-hooks/`
overrides that hook, which is what took the executable from 107 MB to 69 MB.

## What gets bundled, and why

| Payload | How | Reason |
| --- | --- | --- |
| `flet` | `--collect-all flet` | Control classes and submodules are resolved by name at runtime. |
| `flet_desktop` | `--collect-all flet_desktop` + staged client archive | Flet launches a separate Flutter client process; the frozen app must ship it. |
| `yt_dlp` | `--collect-submodules yt_dlp` (its own PyInstaller hook collects the rest) | Extractors are imported lazily, so static analysis alone misses them and searches would fail. `--collect-all` would also copy the package's 10 MB of `.py` sources as data files, which the PYZ already holds compiled. |
| `imageio_ffmpeg` | PyInstaller hook (`pyinstaller-hooks-contrib`, a PyInstaller dependency) + `--hidden-import imageio_ffmpeg.binaries` | Ships the ~79 MB static ffmpeg used when no system `ffmpeg` is on `PATH`; the package is reached via `importlib.resources` and is never imported directly. |
| QuickJS | `--add-binary` after downloading the pinned release binary and verifying its pinned SHA-256 | yt-dlp needs a JavaScript runtime to decipher YouTube stream URLs; without one some tracks return HTTP 403. quickjs-ng solves the same challenges as yt-dlp's default Deno for 2.5 MB instead of 96 MB. |
| `mutagen` | `--hidden-import mutagen` | Writes the tags and the cover art: yt-dlp uses it for the picture inside flac files, `metadata.py` for the tags a music database gave us. Without it those flac downloads fail. |
| `assets/` | `--add-data` | Window icon (Windows) and the source SVG for the icon. |
| `locales/` | `--add-data` | One JSON file per UI language; `i18n` reads them from inside the bundle. |

### Licenses

Two of the payloads decide the license of the whole, so it is worth writing down
what each one is:

| Payload | License |
| --- | --- |
| Cadenza itself | GPL-3.0-or-later (`LICENSE`) |
| yt-dlp | Unlicense (public domain) |
| `flet`, `flet_desktop` | Apache-2.0 |
| ffmpeg, the binary `imageio_ffmpeg` ships | GPL-3.0 — the wrapper is BSD-2-Clause, the binary is a `--enable-gpl --enable-version3` static build |
| QuickJS (`quickjs-ng`) | MIT |
| mutagen | GPL-2.0-or-later |
| PyInstaller's bootloader | GPL-2.0-or-later, with the exception that covers the applications it freezes |
| PyAV, Pillow (Android only) | BSD-3-Clause, MIT-CMU |

`metadata.py` imports mutagen and the executable carries that ffmpeg, so an app
distributing both has to be GPL-compatible: Cadenza is GPL-3.0-or-later rather
than MIT for that reason, not by preference. Nothing here is copyleft in a way
that reaches a user's own music or downloads.

### The Flet desktop client

`flet_desktop` looks for a client archive inside its own package directory
(`flet_desktop/app/<artifact>`) and unpacks it to `~/.flet/client/` on first
use; if the archive is missing it downloads it from GitHub Releases. `build.py`
therefore re-packs the client already cached by `flet_desktop` into that
location inside the executable, together with a `<archive>.sha256` sidecar so
the runtime does not re-hash 40 MB on every launch. The archive is written
deterministically (fixed timestamps, sorted members, fixed gzip mtime), so
rebuilding does not change its content fingerprint — an installed copy keeps
reusing the same unpacked client instead of extracting a fresh one per build.

`flet_web` is deliberately **not** bundled: it is only used by the web/browser
view, which Flet selects when no `DISPLAY` is available. The desktop build
targets the desktop client, so a headless launch (no X11/Wayland display) is
not supported; run it from a desktop session.

### The bundled JavaScript runtime

`DENO_ASSETS` in `build.py` maps the build host (platform and machine type) to
the matching QuickJS release binary; it is downloaded once into
`build/quickjs/`, checked against the digest pinned in `QUICKJS_ASSETS` (the
project publishes no checksum file, so the digests live in the source), and
copied to `jsrt/qjs` inside the bundle. A cached copy whose digest no longer
matches is re-downloaded, and a mismatch aborts the build. To move to a newer
QuickJS, change `QUICKJS_VERSION` **and** the digests — a build that pins a
version and trusts whatever arrives is not pinned at all.

## The exact PyInstaller command

`build.py` runs, with `--windowed` and `--icon assets/icon.ico` added on
Windows (macOS gets `--windowed` too):

```bash
<venv>/python -m PyInstaller main.py \
  --name Cadenza \
  --onefile \
  --noconfirm \
  --clean \
  --noupx \
  --distpath dist \
  --workpath build \
  --specpath build \
  --additional-hooks-dir "<project>/packaging/pyinstaller-hooks" \
  --collect-all flet \
  --collect-all flet_desktop \
  --collect-submodules yt_dlp \
  --hidden-import imageio_ffmpeg.binaries \
  --hidden-import mutagen \
  --exclude-module av \
  --exclude-module PIL \
  --exclude-module flet_web \
  --exclude-module flet.cli \
  --exclude-module flet.fastapi \
  --exclude-module flet.testing \
  --exclude-module flet.pytest_plugin \
  --exclude-module pygments \
  --exclude-module rich \
  --exclude-module markdown_it \
  --exclude-module cookiecutter \
  --exclude-module watchdog \
  --exclude-module questionary \
  --exclude-module httpx \
  --exclude-module httpcore \
  --exclude-module oauthlib \
  --add-binary "<staged>/qjs:jsrt" \
  --add-data "<project>/assets:assets" \
  --add-data "<project>/locales:locales" \
  --add-data "<staged>/flet-linux-<distro>-light-<arch>.tar.gz:flet_desktop/app" \
  --add-data "<staged>/flet-linux-<distro>-light-<arch>.tar.gz.sha256:flet_desktop/app"
```

(`--add-data` uses `;` instead of `:` on Windows.) The staged payloads live in
a temporary directory, not in `build/`: `--clean` wipes the work path before
every build. `--additional-hooks-dir` points at this repository's own hooks,
which win over the ones an installed `flet-cli` ships. `--noupx` keeps the result identical whether or not UPX happens to
be installed. The three `--exclude-module` flags drop what a frozen desktop
build never uses: the in-process converter's PyAV and Pillow (`flet-cli` drags
Pillow into the build environment, and the executable has its own ffmpeg) and
`flet_web`, the browser view's server. When the build finishes, the script
inspects the executable's archive and fails if the client archive, ffmpeg, the
QuickJS binary, the UI assets, a locale file, the version stamp or the yt-dlp
extractors are missing - the last two in the PYZ, where Python modules actually
live, rather than among the data files.

## Notes

* **First launch** unpacks the Flet client into
  `~/.flet/client/flet-desktop-<flavor>-<version>-<fingerprint>/` (a few
  seconds) and the rest of the bundle into a temporary directory; later
  launches reuse the client and only re-extract the bundle.
* **Settings** are read from `~/.config/Cadenza/config.json`
  (`%APPDATA%\Cadenza\config.json` on Windows) — nothing is written next to the
  executable.
* **Single-file startup** costs an extra unpack of the whole bundle (~85 MB) on
  each run; that is inherent to `--onefile`. `--onedir` trades it for a folder
  of files if startup time matters more than tidiness.
* **Antivirus** engines occasionally flag PyInstaller one-file builds; the
  `--onedir` variant avoids that too.
