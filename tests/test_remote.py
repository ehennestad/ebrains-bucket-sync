import pytest

from ebrains_bucket_sync.model import SyncWarning
from ebrains_bucket_sync.remote import (
    MAX_SINGLE_OBJECT_BYTES,
    list_remote_files,
    parse_listing_time,
)

from .conftest import FakeStorage, utc


def test_strips_prefix_and_leaves_out_folders(storage: FakeStorage):
    storage.put("b", "results/", content_type="application/directory")
    storage.put("b", "results/sub", content_type="application/octet-stream")  # implied parent
    storage.put("b", "results/sub/a.txt", b"aaa")
    storage.put("b", "results/marker", content_type="application/directory")
    storage.put("b", "results/b.txt", b"b", hash="ABC")
    storage.put("b", "resultsx.txt", b"x")  # shares the prefix text but not the folder

    files = list_remote_files(storage, "b", "results/")

    assert [(f.path, f.bytes, f.hash) for f in files] == [
        ("b.txt", 1, "abc"),
        ("sub/a.txt", 3, files[1].hash),
    ]
    assert files[0].modified_time == utc(2999, 1, 1)


def test_whole_bucket_with_empty_prefix(storage: FakeStorage):
    storage.put("b", "a.txt", b"a")
    storage.put("b", "sub/b.txt", b"b")

    assert [f.path for f in list_remote_files(storage, "b", "")] == ["a.txt", "sub/b.txt"]


def test_leaves_out_names_that_escape_the_folder(storage: FakeStorage):
    storage.put("b", "ok.txt", b"a")
    storage.put("b", "../escape.txt", b"a")
    storage.put("b", "sub//double.txt", b"a")
    storage.put("b", "back\\slash.txt", b"a")

    with pytest.warns(SyncWarning, match="3 object"):
        files = list_remote_files(storage, "b", "")

    assert [f.path for f in files] == ["ok.txt"]


def test_drops_checksum_of_multipart_and_large_objects(storage: FakeStorage):
    storage.put("b", "multipart.bin", b"x", hash="abc-3")
    storage.put("b", "small.bin", b"x", hash="abc")
    files = list_remote_files(storage, "b", "")
    assert [f.hash for f in files] == ["", "abc"]

    large = storage.list_objects("b", "")[1]
    storage.buckets["b"]["small.bin"].content = b""
    storage.list_objects = lambda bucket, prefix: [  # type: ignore[method-assign]
        large.__class__(large.name, MAX_SINGLE_OBJECT_BYTES + 1, large.last_modified, "abc", None)
    ]
    assert list_remote_files(storage, "b", "")[0].hash == ""


def test_empty_bucket(storage: FakeStorage):
    assert list_remote_files(storage, "b", "") == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2024-05-03T10:22:33.123456", utc(2024, 5, 3, 10, 22, 33)),
        ("2024-05-03T10:22:33", utc(2024, 5, 3, 10, 22, 33)),
        ("2024-05-03T10:22:33Z", utc(2024, 5, 3, 10, 22, 33)),
        ("2024-05-03T12:22:33+02:00", utc(2024, 5, 3, 10, 22, 33)),
        ("2024-05-03T12:22:33.5+0200", utc(2024, 5, 3, 10, 22, 33)),
        ("yesterday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_listing_time(text, expected):
    assert parse_listing_time(text) == expected
