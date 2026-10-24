import os
import sys
from datetime import timezone

import pytest

from ebrains_bucket_sync.local import (
    compute_md5,
    is_case_insensitive,
    list_local_files,
    scan_local_folder,
)

from .conftest import write


def test_lists_relative_paths_with_slashes_sorted(folder):
    write(folder, "b.txt", b"bb")
    write(folder, "sub/deeper/c.txt", b"c")
    write(folder, "sub/a.txt", b"")
    (folder / "empty").mkdir()

    files = list_local_files(folder)

    assert [(f.path, f.bytes) for f in files] == [
        ("b.txt", 2),
        ("sub/a.txt", 0),
        ("sub/deeper/c.txt", 1),
    ]
    assert all(f.modified_time.tzinfo == timezone.utc for f in files)
    assert all(f.hash == "" for f in files)


def test_missing_folder_is_an_error(tmp_path):
    with pytest.raises(NotADirectoryError):
        list_local_files(tmp_path / "nope")


def test_md5_of_known_content(folder):
    path = write(folder, "hello.txt", b"hello world")

    assert compute_md5(path) == "5eb63bbbe01eeed093cb22bb8f5acdc3"


@pytest.mark.skipif(sys.platform == "win32", reason="Symbolic links need privileges on Windows.")
def test_scan_reports_linked_folders_without_listing_their_files(folder, tmp_path):
    outside = tmp_path / "outside"
    write(outside, "elsewhere.txt", b"e")
    write(folder, "a.txt", b"a")
    (folder / "sub").mkdir()
    (folder / "sub" / "link").symlink_to(outside, target_is_directory=True)

    listing = scan_local_folder(folder)

    assert [f.path for f in listing.files] == ["a.txt"]
    assert listing.linked_folders == ["sub/link"]
    assert listing.unreadable_folders == []
    assert [f.path for f in list_local_files(folder)] == ["a.txt"]


@pytest.mark.skipif(
    sys.platform == "win32" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="Needs POSIX permissions that apply to the user.",
)
def test_scan_reports_unreadable_folders(folder):
    write(folder, "a.txt", b"a")
    write(folder, "locked/b.txt", b"b")
    (folder / "locked").chmod(0o000)
    try:
        listing = scan_local_folder(folder)
    finally:
        (folder / "locked").chmod(0o755)

    assert [f.path for f in listing.files] == ["a.txt"]
    assert listing.unreadable_folders == ["locked"]


def test_case_probe_looks_up_a_listed_file_with_its_case_changed(folder):
    write(folder, "Data.txt", b"d")
    ignores_case = (folder / "DATA.TXT").is_file()

    assert is_case_insensitive(folder, ["Data.txt"]) == ignores_case


def test_case_probe_without_a_name_to_look_up_goes_by_platform(folder):
    write(folder, "123", b"d")

    expected = sys.platform in ("darwin", "win32")
    assert is_case_insensitive(folder, ["123"]) == expected
    assert is_case_insensitive(folder / "missing", []) == expected
