"""Live checks of the host of the virtual bucket browser.

The host is opened on a folder of the test bucket and sent the requests
its page sends, so that the listing, a download, making a file online
only again and the update after an object was removed run against the
real Data Proxy. The host needs no window: the page is a test double that
records the events, so the tests run wherever the sync tests run. They
mirror VirtualBucketBrowserLiveTest of the EBRAINS MATLAB toolbox test
for test.

See conftest.py of this folder for how to run the live tests.
"""

from __future__ import annotations

import pytest

from ebrains_bucket_sync import SyncOptions, sync_to_bucket
from ebrains_bucket_sync.virtual_bucket import read_bucket_info, read_manifest

from ..conftest import write
from .conftest import (
    BUCKET,
    FILE_TIME_TOLERANCE,
    LIVE_MARKS,
    delete_objects_below,
    file_time,
    make_prefix,
    open_host,
    request,
)

pytestmark = LIVE_MARKS


@pytest.fixture
def prefix(storage):
    """The folder of the bucket that the running test works in, emptied afterwards."""
    prefix = make_prefix("browser")
    yield prefix
    # A failed assertion must not leave the objects of the test in the bucket
    delete_objects_below(storage, prefix)


@pytest.fixture
def source(tmp_path, storage, prefix):
    """Two small files, uploaded below the prefix of the test."""
    source = tmp_path / "source"
    write(source, "a.txt", b"abc")
    write(source, "sub/b.txt", b"hello")
    sync_to_bucket(source, BUCKET, storage, SyncOptions(prefix=prefix))
    return source


def state_of(host, path):
    """The state the host gives a file."""
    return host.bucket.row(path).state


def test_new_folder_is_listed_and_filled_with_placeholders(tmp_path, storage, prefix, source):
    """The host has no counterpart of the IsListedInSession flag of the MATLAB browser, so only
    the listing itself is checked."""
    virtual_bucket = tmp_path / "virtual"

    host, page = open_host(virtual_bucket, prefix, storage, update_on_ready=True)

    assert page.alerts == []
    files = host.bucket.files
    assert [row.path for row in files] == ["a.txt", "sub/b.txt"]
    assert [row.state for row in files] == ["online_only", "online_only"]
    assert [row.remote_bytes for row in files] == [
        (source / "a.txt").stat().st_size,
        (source / "sub" / "b.txt").stat().st_size,
    ]
    assert host.bucket.is_listed
    assert (virtual_bucket / "sub" / "b.txt").stat().st_size == 0

    info = read_bucket_info(virtual_bucket)
    assert info is not None, "The host wrote no bucket.json."
    assert (info.bucket, info.prefix) == (BUCKET, prefix)
    manifest = read_manifest(virtual_bucket)
    assert manifest is not None, "The host wrote no manifest."
    assert [entry.path for entry in manifest] == ["a.txt", "sub/b.txt"]


def test_download_and_online_only_round_trip(tmp_path, storage, prefix, source):
    virtual_bucket = tmp_path / "virtual"
    host, page = open_host(virtual_bucket, prefix, storage, update_on_ready=True)
    local_file = virtual_bucket / "a.txt"

    request(host, "download", paths=["a.txt"])

    assert page.alerts == []
    assert local_file.read_bytes() == (source / "a.txt").read_bytes()
    assert state_of(host, "a.txt") == "downloaded"
    remote_time = host.bucket.manifest_entry("a.txt").modified_time
    assert abs(file_time(local_file) - remote_time) <= FILE_TIME_TOLERANCE, (
        "The download did not get the time of its object."
    )

    # A file that matches its object in a listing is emptied without a
    # confirmation dialog
    request(host, "onlineOnly", paths=["a.txt"])

    assert page.named("confirm") == []
    assert local_file.stat().st_size == 0
    assert state_of(host, "a.txt") == "online_only"


def test_update_removes_the_placeholder_of_a_removed_object(tmp_path, storage, prefix, source):
    virtual_bucket = tmp_path / "virtual"
    host, page = open_host(virtual_bucket, prefix, storage, update_on_ready=True)

    storage.delete_object(BUCKET, prefix + "sub/b.txt")
    request(host, "update")

    assert page.alerts == []
    assert [row.path for row in host.bucket.files] == ["a.txt"]
    assert not (virtual_bucket / "sub" / "b.txt").exists()
    assert (virtual_bucket / "a.txt").is_file()
    assert [entry.path for entry in read_manifest(virtual_bucket)] == ["a.txt"]


def test_open_online_shows_the_page_of_the_bucket(tmp_path, storage, prefix, source):
    opened = []
    host, page = open_host(
        tmp_path / "virtual", prefix, storage, update_on_ready=True, open_url=opened.append
    )

    request(host, "openOnline")

    assert page.alerts == []
    assert len(opened) == 1
    assert opened[0].startswith("https://")
    assert BUCKET in opened[0]
