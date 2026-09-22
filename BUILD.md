# Building Cadenza

`build.py` freezes the app into one self-contained executable for the platform
it runs on (PyInstaller cannot cross-compile: build on Linux to get a Linux
binary, on Windows to get a `.exe`).

```bash
python build.py
```

No options, no prompts. Artifacts land in `dist/`, PyInstaller's intermediates
and the generated spec file in `build/`. What ends up inside the executable is
listed under [What gets bundled](#what-gets-bundled-and-why).

## Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt pyinstaller
.venv/bin/python build.py
```

Produces `dist/Cadenza` — a single ELF file (~125 MB, ~80 s to build). Nothing
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
`dist/Cadenza.app` — a macOS *bundle directory*, not a single file. Untested
here: there is no macOS host to build on, and the bundle would need an `.icns`
icon instead of the `.ico` used on Windows.

## Verifying a build

From a directory other than the project (so nothing resolves from the sources),
run the app's headless self-test — it exercises the bundled ffmpeg, the
JavaScript runtime, the yt-dlp extractors, the network path and the tags
written into the file:

```bash
mkdir -p /tmp/frozen-selftest && cd /tmp/frozen-selftest
/absolute/path/to/dist/Cadenza --selftest     # exits 0 when everything works
```

A real run, ~9 s:

```
ffmpeg       /tmp/_MEI0003b74cPivChy/imageio_ffmpeg/binaries/ffmpeg-linux-x86_64-v7.0.2
js runtimes  deno
search       2 result(s): Sneaky Snitch (Kevin MacLeod) - Background Music (HD)
download     Sneaky Snitch (Kevin MacLeod) - Background Music (HD).mp3 (5.2 MiB)
tags         artist=Gaming Sound FX · genre=People & Blogs · date=20150625
```

To prove the executable really is standalone, run it with a stripped
environment: if `js runtimes` still says `deno`, that Deno came out of the
bundle, not from the machine, and the search/download that follow were solved
with it:

```bash
env -i PATH=/usr/bin:/bin HOME=/tmp/selftest-home \
  /absolute/path/to/dist/Cadenza --selftest
```

Finally, launch it without arguments to confirm the window opens (the Flet
client process appears ~1.5 s after start, the window ~2.5 s) and that the
first-run dialog asks where to put your music.

## Running the app

| | Linux | Windows |
| --- | --- | --- |
| OS | Ubuntu 24.04 or newer (older distros need a rebuild on that distro) | 64-bit Windows 10/11 |
| Session | X11 or Wayland desktop | any |
| Installed dependencies | none | none |
| Network | required for search and download | required |
| Disk | ~125 MB for the executable, plus your music | ~125 MB, plus your music |
| Admin rights | not needed | not needed |

The executable carries Python, ffmpeg, the Flet desktop client, a Deno
JavaScript runtime and the UI assets. It prefers a JavaScript runtime already
installed (`deno`, `node`, `bun` or `quickjs` on `PATH`) and falls back to the
bundled Deno, so nothing has to be set up either way.

First launch takes a few seconds longer: the executable unpacks the Flet client
into `~/.flet/client/` and reuses it afterwards. Settings live in
`~/.config/Cadenza/config.json` (`%APPDATA%\Cadenza\config.json` on Windows);
the music goes wherever the first-run dialog points.

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

## Languages

The UI follows the system language: it checks Windows' user UI language, then
`LC_ALL`/`LC_MESSAGES`/`LANG`/`LANGUAGE`, and falls back to English. English and
Spanish ship today; `i18n.py` holds one dictionary per language, and any key a
translation is missing falls back to English, so a new language can be added
partially and refined later.

## What gets bundled, and why

| Payload | How | Reason |
| --- | --- | --- |
| `flet` | `--collect-all flet` | Control classes and submodules are resolved by name at runtime. |
| `flet_desktop` | `--collect-all flet_desktop` + staged client archive | Flet launches a separate Flutter client process; the frozen app must ship it. |
| `yt_dlp` | `--collect-all yt_dlp` | Extractors are imported lazily, so static analysis alone misses them and searches would fail. |
| `imageio_ffmpeg` | PyInstaller hook (`pyinstaller-hooks-contrib`, a PyInstaller dependency) + `--hidden-import imageio_ffmpeg.binaries` | Ships the ~79 MB static ffmpeg used when no system `ffmpeg` is on `PATH`; the package is reached via `importlib.resources` and is never imported directly. |
| Deno | `--add-binary` after downloading the pinned release archive and verifying its published SHA-256 | yt-dlp needs a JavaScript runtime to decipher YouTube stream URLs; without one some tracks return HTTP 403. |
| `assets/` | `--add-data` | Window icon (Windows) and the source SVG for the icon. |

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
the matching Deno release archive; the archive is downloaded once into
`build/deno/`, checked against the SHA-256 Deno publishes next to it, and the
single `deno`/`deno.exe` member is embedded at `jsrt/`. A cached archive whose
digest no longer matches is re-downloaded, and a digest mismatch aborts the
build. To move to a newer Deno, change `DENO_VERSION` — the checksum is fetched
per build, so nothing else needs updating.

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
  --collect-all flet \
  --collect-all flet_desktop \
  --collect-all yt_dlp \
  --hidden-import imageio_ffmpeg.binaries \
  --add-binary "<staged>/deno:jsrt" \
  --add-data "<project>/assets:assets" \
  --add-data "<staged>/flet-linux-<distro>-light-<arch>.tar.gz:flet_desktop/app" \
  --add-data "<staged>/flet-linux-<distro>-light-<arch>.tar.gz.sha256:flet_desktop/app"
```

(`--add-data` uses `;` instead of `:` on Windows.) The staged payloads live in
a temporary directory, not in `build/`: `--clean` wipes the work path before
every build. `--noupx` keeps the result identical whether or not UPX happens to
be installed. When the build finishes, the script inspects the executable's
archive and fails if the client archive, ffmpeg, the Deno binary, the UI assets
or the yt-dlp extractors are missing.

## Notes

* **First launch** unpacks the Flet client into
  `~/.flet/client/flet-desktop-<flavor>-<version>-<fingerprint>/` (a few
  seconds) and the rest of the bundle into a temporary directory; later
  launches reuse the client and only re-extract the bundle.
* **Settings** are read from `~/.config/Cadenza/config.json`
  (`%APPDATA%\Cadenza\config.json` on Windows) — nothing is written next to the
  executable.
* **Single-file startup** costs an extra unpack of the whole bundle (~125 MB) on
  each run; that is inherent to `--onefile`. `--onedir` trades it for a folder
  of files if startup time matters more than tidiness.
* **Antivirus** engines occasionally flag PyInstaller one-file builds; the
  `--onedir` variant avoids that too.
