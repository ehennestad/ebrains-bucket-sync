from datetime import timezone

import pytest

from ebrains_bucket_sync.local import compute_md5, list_local_files

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
