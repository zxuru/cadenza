"""A log on disk: what failed, and what the app was doing when it did.

Nothing used to leave a trace. A Flet task that raises dies inside the event
loop, a thread that raises is answered by `threading.excepthook` printing to a
console nobody is looking at, and the window keeps whatever it last said - so
"it did not download" arrived with nothing to go on. The file lives in the
config directory beside `config.json`, so a user can be asked for one file, and
it is bounded in size, so a machine that fails in a loop keeps the newest
failures instead of filling its disk.
"""

from __future__ import annotations

import functools
import inspect
import logging
import logging.handlers
import sys
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import settings

LOG_FILE_NAME = "cadenza.log"
LOGGER_NAME = "cadenza"
# One rotated file behind the current one: a megabyte of history is months of
# ordinary use and still holds the run that broke, however loudly it broke.
MAX_BYTES = 512 * 1024
BACKUPS = 1

_logger = logging.getLogger(LOGGER_NAME)
_previous_excepthook = sys.excepthook
_previous_thread_excepthook = threading.excepthook


def path(directory: Path | None = None) -> Path:
    """The file this run logs to: the config directory, unless told otherwise."""
    return (directory or settings.config_dir()) / LOG_FILE_NAME


def log() -> logging.Logger:
    """The app's logger: what it writes lands in that file, once `setup` ran."""
    return _logger


def setup(directory: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    """Send everything the app logs to its own file; never fail because of it.

    Called once at startup. A second call for the same directory changes
    nothing; one for a different directory moves the log there, so the file a
    run leaves behind is the one it was told to write.

    A config directory that cannot be written makes an app that cannot log, not
    a second failure on top of the one being diagnosed, so the handler is
    created late - the file appears with the first record - and a file that
    cannot be written is answered by `logging` itself, on stderr, with the app
    carrying on.

    Also, the two places an exception can end up with no `except` above it: the
    main thread and any thread started without one. Both are recorded and then
    handed to the hook that was there before, so nothing that used to print
    stops printing.
    """
    wanted = path(directory)
    try:
        # A first run has no config directory yet: the log makes one, and an
        # app whose settings live there would make the same one anyway.
        wanted.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # not a reason to fail a startup: `logging` reports it if it writes
    for handler in list(_logger.handlers):
        if Path(getattr(handler, "baseFilename", "")) != wanted:
            _logger.removeHandler(handler)
            handler.close()
    if not _logger.handlers:  # a second call must not double every line
        handler = logging.handlers.RotatingFileHandler(
            wanted,
            maxBytes=MAX_BYTES,
            backupCount=BACKUPS,
            encoding="utf-8",
            delay=True,
            errors="replace",
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        _logger.addHandler(handler)
        _logger.setLevel(level)
    _hook_exceptions()
    return _logger


def failure(what: str, err: BaseException) -> None:
    """Record a failure that was answered on screen, traceback included.

    The window says what the user needs; the file says what a report needs -
    which part of the app failed, and where.
    """
    _logger.error("%s failed: %s", what, err, exc_info=err)


def watching(handler: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a `page.run_task` handler so a failure in it leaves a traceback.

    A task handler may be a coroutine function, a plain function, or something
    that returns an awaitable - `page.run_task` takes all three - so all three
    are wrapped the same way. The error is let go afterwards: recording is not
    swallowing, and whatever answered it before still does.
    """
    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def watched(*args: Any, **kwargs: Any) -> Any:
            try:
                return await handler(*args, **kwargs)
            except Exception:
                _failed(handler)
                raise

    else:

        @functools.wraps(handler)
        def watched(*args: Any, **kwargs: Any) -> Any:
            try:
                result = handler(*args, **kwargs)
            except Exception:
                _failed(handler)
                raise
            if inspect.isawaitable(result):
                return _awaited(result, handler)
            return result

    return watched


async def _awaited(awaitable: Awaitable[Any], handler: Callable[..., Any]) -> Any:
    """The awaitable a plain handler handed back, watched like the rest."""
    try:
        return await awaitable
    except Exception:
        _failed(handler)
        raise


def _failed(handler: Callable[..., Any]) -> None:
    """Name the task that failed; the traceback comes from where it was raised."""
    name = getattr(handler, "__qualname__", None) or getattr(handler, "__name__", "")
    _logger.exception("task %s failed", name or repr(handler))


def _hook_exceptions() -> None:
    """Record unhandled exceptions, then pass them to the hook that was there."""
    global _previous_excepthook, _previous_thread_excepthook
    if sys.excepthook is not _logged_excepthook:
        _previous_excepthook = sys.excepthook
        sys.excepthook = _logged_excepthook
    if threading.excepthook is not _logged_thread_excepthook:
        _previous_thread_excepthook = threading.excepthook
        threading.excepthook = _logged_thread_excepthook


def _logged_excepthook(
    exc_type: type[BaseException], value: BaseException, tb: Any
) -> None:
    failure("unhandled", value)
    _previous_excepthook(exc_type, value, tb)


def _logged_thread_excepthook(args: threading.ExceptHookArgs) -> None:
    if args.exc_value is not None:
        which = args.thread.name if args.thread is not None else "thread"
        failure(which, args.exc_value)
    _previous_thread_excepthook(args)
