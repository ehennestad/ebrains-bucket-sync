"""Push to a real bucket. Needs EBRAINS_BUCKET_SYNC_TEST_BUCKET and a login.

The login comes from the stored device-flow login, or from EBRAINS_BUCKET_SYNC_TOKEN.
"""

import os
import uuid

import pytest

from ebrains_bucket_sync import (
    DeviceFlowAuthenticator,
    EbrainsDriveStorage,
    SyncOptions,
    sync_to_bucket,
)
from ebrains_bucket_sync.remote import list_remote_files

BUCKET = os.environ.get("EBRAINS_BUCKET_SYNC_TEST_BUCKET")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not BUCKET, reason="EBRAINS_BUCKET_SYNC_TEST_BUCKET is not set"),
]


@pytest.fixture
def storage():
    return EbrainsDriveStorage(DeviceFlowAuthenticator())


@pytest.fixture
def prefix(storage):
    prefix = f"ebrains-bucket-sync-test/{uuid.uuid4()}/"
    yield prefix
    for entry in list_remote_files(storage, BUCKET, prefix):
        storage.delete_object(BUCKET, prefix + entry.path)


def test_push_round_trip(tmp_path, storage, prefix):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_bytes(b"alpha")
    (tmp_path / "sub" / "name with space.txt").write_bytes(b"beta")
    options = SyncOptions(prefix=prefix, delete=True)

    first = sync_to_bucket(tmp_path, BUCKET, storage, options)
    assert {(r.path, r.status) for r in first} == {
        ("a.txt", "done"),
        ("sub/name with space.txt", "done"),
    }

    listed = list_remote_files(storage, BUCKET, prefix)
    assert [(f.path, f.bytes) for f in listed] == [("a.txt", 5), ("sub/name with space.txt", 4)]
    assert all(f.modified_time is not None for f in listed), "the listing reports no times"
    assert all(len(f.hash) == 32 for f in listed), "the listing reports no plain MD5"

    second = sync_to_bucket(tmp_path, BUCKET, storage, options)
    assert all(r.reason == "unchanged" for r in second)

    (tmp_path / "a.txt").write_bytes(b"alpha!")
    (tmp_path / "sub" / "name with space.txt").unlink()
    third = sync_to_bucket(tmp_path, BUCKET, storage, options)
    assert {(r.path, r.action, r.status) for r in third} == {
        ("a.txt", "upload", "done"),
        ("sub/name with space.txt", "delete", "done"),
    }
    assert [f.path for f in list_remote_files(storage, BUCKET, prefix)] == ["a.txt"]
