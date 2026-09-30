"""Which file a release is asked for, under both namings it has had.

The workflow writes `Cadenza-<version>-<platform>.<ext>` now and wrote plain
`<platform>.<ext>` before; an updater from either era has to find its file in
either release, which is what the resolver in `Release.asset` promises.
"""

from __future__ import annotations

import pytest

import update


def release(*names: str) -> update.Release:
    return update.Release(
        version="1.0.16",
        tag="v1.0.16",
        page="https://github.com/zxuru/cadenza/releases/tag/v1.0.16",
        assets={
            name: update.Asset(name, f"https://example.invalid/{name}", 1)
            for name in names
        },
    )


def test_a_release_named_the_new_way_is_found():
    published = release(
        "Cadenza-1.0.16-windows-x86_64.exe",
        "Cadenza-1.0.16-linux-x86_64.tar.gz",
    )

    found = published.asset(update.ASSETS[("win32", "x86_64")])

    assert found is not None
    assert found.name == "Cadenza-1.0.16-windows-x86_64.exe"


def test_a_release_named_the_old_way_is_still_found():
    published = release("windows-x86_64.exe")

    found = published.asset("windows-x86_64.exe")

    assert found is not None
    assert found.name == "windows-x86_64.exe"


def test_the_version_of_the_release_is_never_this_build_s():
    # An updater stamped 1.0.15 asking a 1.0.99 release - and the other way
    # round: the version is the file's own, found behind the platform part.
    assert release("Cadenza-1.0.99-macos-arm64.zip").asset("macos-arm64.zip")
    assert release("macos-arm64.zip").asset("macos-arm64.zip")


@pytest.mark.parametrize(
    ("machine", "base"),
    [
        ("aarch64", "arm64-v8a.apk"),
        ("arm64", "arm64-v8a.apk"),
        ("armv7l", "armeabi-v7a.apk"),
        ("x86_64", "x86_64.apk"),
    ],
)
def test_every_abi_is_found_under_both_namings(machine, base):
    assert update.ANDROID_ASSETS[machine] == base

    old = release(f"cadenza-{base}")
    new = release(f"cadenza-1.0.16-{base}")

    assert old.asset(base).name == f"cadenza-{base}"
    assert new.asset(base).name == f"cadenza-1.0.16-{base}"


def test_nothing_else_in_the_release_is_taken_for_it():
    published = release(
        "Cadenza-1.0.16-macos-arm64.zip",
        "Cadenza-1.0.16-linux-x86_64.tar.gz",
        "SHA256SUMS",
    )

    assert published.asset("windows-x86_64.exe") is None
    assert published.asset("x86_64.apk") is None  # an ABI is not a tarball
    # The digest list is fetched by its own, fixed name.
    assert published.asset(update.SUMS_ASSET).name == "SHA256SUMS"
