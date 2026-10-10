"""The log on disk: written where it is asked for, and never in the way.

A task that raised, a thread that raised, a startup that failed: none of it
used to reach a file, so when a download did not happen there was nothing to
ask the user for. These tests pin where the file goes, that the reason lands in
it, and that a directory which cannot be written costs the log and nothing
else.
"""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest

import logs


@pytest.fixture(autouse=True)
def clean_logger():
    """The app's logger is process-wide: one test must not inherit another's
    handler, and a hook this test installs must not outlive it."""
    logger = logs.log()
    handlers = list(logger.handlers)
    hooks = (sys.excepthook, threading.excepthook)
    logger.handlers.clear()
    yield
    for handler in logger.handlers:
        if handler not in handlers:
            handler.close()
    logger.handlers[:] = handlers
    sys.excepthook, threading.excepthook = hooks


def read(directory) -> str:
    """What the log says, if a log was written at all."""
    path = logs.path(directory)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_a_failure_is_written_where_it_can_be_asked_for(tmp_path):
    logs.setup(tmp_path)

    assert not logs.path(tmp_path).exists(), "the file waits for a first line"

    try:
        raise ValueError("no space left")
    except ValueError as err:
        logs.failure("download", err)

    logged = read(tmp_path)
    assert "download failed: no space left" in logged
    assert "Traceback" in logged, "the traceback is the part a report needs"
    assert "ValueError" in logged


def test_a_failing_task_is_recorded_and_still_raises(tmp_path):
    logs.setup(tmp_path)

    async def loads_the_album() -> None:
        raise PermissionError("the folder is read-only")

    with pytest.raises(PermissionError):
        asyncio.run(logs.watching(loads_the_album)())

    logged = read(tmp_path)
    assert "loads_the_album failed" in logged, "the task that failed is named"
    assert "PermissionError" in logged


def test_a_handler_that_returns_an_awaitable_is_watched_too(tmp_path):
    logs.setup(tmp_path)

    async def work() -> None:
        raise RuntimeError("the sample never arrived")

    def start() -> object:
        return work()

    with pytest.raises(RuntimeError):
        asyncio.run(logs.watching(start)())

    assert "start failed" in read(tmp_path)


# The hook this app installs passes the exception on, and pytest's own hook is
# what it passes it to: the warning is about that handover, not about the app.
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_an_unhandled_exception_in_a_thread_is_recorded(tmp_path):
    logs.setup(tmp_path)

    def download() -> None:
        raise OSError("the disk is full")

    thread = threading.Thread(target=download, name="worker")
    thread.start()
    thread.join()

    logged = read(tmp_path)
    assert "worker failed: the disk is full" in logged
    assert "OSError" in logged


def test_setup_twice_does_not_double_every_line(tmp_path):
    logs.setup(tmp_path)
    logs.setup(tmp_path)

    assert len(logs.log().handlers) == 1


def test_the_log_follows_the_directory_it_is_given(tmp_path):
    # What keeps a test run out of the user's config directory: the second call
    # is the one that counts, and the first file is not written to at all.
    first, second = tmp_path / "one", tmp_path / "two"
    logs.setup(first)
    logs.setup(second)

    logs.failure("startup", RuntimeError("only the second run has this"))

    assert not logs.path(first).exists()
    assert "only the second run has this" in read(second)


def test_a_directory_that_cannot_be_written_costs_only_the_log(tmp_path):
    # A file where the config directory should be: something a user can end up
    # with, and the app has to survive it - it is a broken settings location,
    # not a broken app.
    in_the_way = tmp_path / "config.json"
    in_the_way.write_text("{}")

    logs.setup(in_the_way)  # must not raise
    logs.failure("startup", RuntimeError("no log for this run"))

    assert not logs.path(in_the_way).exists()
