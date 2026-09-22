# Replaces the hook flet-cli ships (`flet_cli/__pyinstaller/hook-flet.py`).
#
# That hook adds the *unpacked* Flet client from `~/.flet/client/` as
# `flet_desktop/app/`, and `build.py` has already re-packed that same client
# into the archive `flet_desktop` looks for - so a build run where flet-cli is
# installed (a developer's machine, or the APK job's virtualenv) ships the
# client twice: 16 MB of files nothing ever reads. CI's desktop job installs no
# flet-cli and never sees them, which is exactly the kind of difference that
# should not exist between two builds of the same commit.
#
# `--collect-all flet` already collects flet's own data files, the client
# archive is added with `--add-data` by `build.py`, and hooks found in
# `--additional-hooks-dir` take precedence over the installed ones.
datas = []
