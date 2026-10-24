"""Live checks of push, pull, virtual buckets and multipart uploads.

The unit tests of the sync see only the in-memory bucket, so what the live
listing reports is checked here: the time of each upload, the MD5 checksum
of an object, and that a multipart object has no checksum of its content.
The rest of the tests run the paths of the sync contract (spec/README.md)
end to end against the Data Proxy: only new and changed files are
transferred, deletions are refused before anything changes, a download
gets the time of its object, and a virtual bucket keeps its placeholders
and its manifest. They mirror SyncLiveTest of the EBRAINS MATLAB toolbox
test for test, so both implementations are checked the same way.

See conftest.py of this folder for how to run the live tests.
"""

from __future__ import annotations

import json
import math
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import ebrains_drive.bucket
import pytest
import requests

from ebrains_bucket_sync import SyncOptions, SyncRefused, SyncWarning, sync_from_bucket
from ebrains_bucket_sync import sync_to_bucket as sync_to_bucket_
from ebrains_bucket_sync.browser import VirtualBucket
from ebrains_bucket_sync.local import compute_md5
from ebrains_bucket_sync.storage import MULTIPART_MANIFEST_SUFFIX
from ebrains_bucket_sync.virtual_bucket import read_bucket_info, read_manifest

from ..conftest import write
from .conftest import (
    BUCKET,
    CLOCK_TOLERANCE,
    FILE_TIME_TOLERANCE,
    LIVE_MARKS,
    delete_objects_below,
    download_bytes,
    file_time,
    list_files,
    list_into_virtual_bucket,
    make_prefix,
    set_file_time,
)

pytestmark = LIVE_MARKS


@pytest.fixture
def prefix(storage):
    """The folder of the bucket that the running test works in, emptied afterwards."""
    prefix = make_prefix("sync")
    yield prefix
    # A failed assertion must not leave the objects of the test in the bucket
    delete_objects_below(storage, prefix)


def rows(results):
    return [(r.path, r.action, r.reason, r.status) for r in results]


def push(folder, storage, prefix, **options):
    return sync_to_bucket_(folder, BUCKET, storage, SyncOptions(prefix=prefix, **options))


def pull(folder, storage, prefix, **options):
    return sync_from_bucket(BUCKET, folder, storage, SyncOptions(prefix=prefix, **options))


def sizes(storage, prefix):
    """(path, bytes) of each object below the prefix, sorted by path."""
    return [(entry.path, entry.bytes) for entry in list_files(storage, prefix)]


def file_states(folder):
    """The state of each file of a virtual bucket, as the browser judges it."""
    return [(row.path, row.state) for row in VirtualBucket.open(folder).files]


def test_listing_reports_upload_times_and_checksums(tmp_path, storage, prefix):
    """The sync compares by the time and the MD5 the listing reports; the unit tests only see
    them from the fake."""
    source = tmp_path / "source"
    write(source, "a.txt", b"abc")

    push(source, storage, prefix)
    files = list_files(storage, prefix)

    assert [entry.path for entry in files] == ["a.txt"]
    assert files[0].bytes == 3
    assert files[0].modified_time.tzinfo == timezone.utc
    assert abs(files[0].modified_time - datetime.now(timezone.utc)) < CLOCK_TOLERANCE, (
        "The listing time is not the time of the upload."
    )
    assert files[0].hash == compute_md5(source / "a.txt")


def test_push_uploads_only_new_and_changed_files(tmp_path, storage, prefix):
    source = tmp_path / "source"
    write(source, "a.txt", b"abc")
    write(source, "sub/b.txt", b"hello")
    write(source, "sub/deep/c.bin", b"x" * 1000)
    paths = ["a.txt", "sub/b.txt", "sub/deep/c.bin"]

    results = push(source, storage, prefix)
    assert rows(results) == [(path, "upload", "new", "done") for path in paths]
    assert sizes(storage, prefix) == [("a.txt", 3), ("sub/b.txt", 5), ("sub/deep/c.bin", 1000)]

    # The listing reports the time of each upload, so the second run finds
    # nothing to do and does not warn that times are missing
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyncWarning)
        results = push(source, storage, prefix)
    assert rows(results) == [(path, "none", "unchanged", "") for path in paths]

    # A file of another size is uploaded again
    write(source, "sub/b.txt", b"hello again")
    results = push(source, storage, prefix)
    assert [(r.reason, r.status) for r in results] == [
        ("unchanged", ""),
        ("size", "done"),
        ("unchanged", ""),
    ]
    assert dict(sizes(storage, prefix))["sub/b.txt"] == 11

    # A file of the same size changed after its upload is uploaded again
    write(source, "a.txt", b"xyz")
    object_time = next(f.modified_time for f in list_files(storage, prefix) if f.path == "a.txt")
    set_file_time(source / "a.txt", object_time + timedelta(minutes=1))
    results = push(source, storage, prefix)
    assert [(r.reason, r.status) for r in results] == [
        ("newer", "done"),
        ("unchanged", ""),
        ("unchanged", ""),
    ]
    assert download_bytes(storage, prefix + "a.txt", tmp_path) == b"xyz"


def test_pull_downloads_and_gives_files_the_time_of_their_objects(tmp_path, storage, prefix):
    source = tmp_path / "source"
    write(source, "a.txt", b"abc")
    write(source, "sub/b.txt", b"hello")
    push(source, storage, prefix)

    # The target folder does not exist yet; the sync creates it
    target = tmp_path / "target"
    results = pull(target, storage, prefix)
    assert rows(results) == [
        ("a.txt", "download", "new", "done"),
        ("sub/b.txt", "download", "new", "done"),
    ]
    assert (target / "a.txt").read_bytes() == b"abc"
    assert (target / "sub" / "b.txt").read_bytes() == b"hello"

    for entry in list_files(storage, prefix):
        assert abs(file_time(target / entry.path) - entry.modified_time) <= FILE_TIME_TOLERANCE, (
            f"The download {entry.path} did not get the time of its object."
        )

    # The folder matches the bucket in both directions, so neither sync
    # transfers a file or warns that times are missing
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyncWarning)
        results = pull(target, storage, prefix)
        assert [r.reason for r in results] == ["unchanged", "unchanged"]
        results = push(target, storage, prefix)
        assert [r.reason for r in results] == ["unchanged", "unchanged"]


def test_checksum_comparison_finds_a_file_edited_in_place(tmp_path, storage, prefix):
    source = tmp_path / "source"
    write(source, "a.txt", b"abc")
    push(source, storage, prefix)
    target = tmp_path / "target"
    pull(target, storage, prefix)

    # An edit that keeps the size and the time of the file
    local_file = target / "a.txt"
    object_time = file_time(local_file)
    local_file.write_bytes(b"xyz")
    set_file_time(local_file, object_time)

    results = pull(target, storage, prefix)
    assert [r.reason for r in results] == ["unchanged"], (
        "The default comparison must keep a local edit of the same size."
    )
    assert local_file.read_bytes() == b"xyz"

    results = pull(target, storage, prefix, comparison="Checksum")
    assert [(r.reason, r.status) for r in results] == [("checksum", "done")]
    assert local_file.read_bytes() == b"abc"


def test_pull_deletes_local_extras_but_not_excluded_files(tmp_path, storage, prefix):
    source = tmp_path / "source"
    write(source, "a.txt", b"abc")
    push(source, storage, prefix)
    target = tmp_path / "target"
    pull(target, storage, prefix)
    write(target, "extra.txt", b"extra")
    write(target, "scratch.tmp", b"scratch")

    results = pull(target, storage, prefix, delete=True, exclude=("*.tmp",))

    # An excluded file is on neither side of the sync
    assert rows(results) == [
        ("a.txt", "none", "unchanged", ""),
        ("extra.txt", "delete", "extraneous", "done"),
    ]
    assert not (target / "extra.txt").exists()
    assert (target / "scratch.tmp").is_file()
    assert (target / "a.txt").is_file()


def test_push_refuses_deletions_before_anything_changes(tmp_path, storage, prefix):
    source = tmp_path / "source"
    write(source, "a.txt", b"abc")
    write(source, "sub/b.txt", b"hello")
    write(source, "sub/deep/c.bin", b"x" * 1000)
    push(source, storage, prefix)
    all_paths = ["a.txt", "sub/b.txt", "sub/deep/c.bin"]
    (source / "a.txt").unlink()
    (source / "sub" / "b.txt").unlink()

    # A dry run shows the refusal with the plan and changes nothing
    results = push(source, storage, prefix, delete=True, max_delete=1, dry_run=True)
    deletions = [r for r in results if r.action == "delete"]
    assert [(r.path, r.status) for r in deletions] == [
        ("a.txt", "skipped"),
        ("sub/b.txt", "skipped"),
    ]
    assert all("max_delete" in r.message for r in deletions)
    assert [path for path, _ in sizes(storage, prefix)] == all_paths

    # A real run stops before it deletes anything
    with pytest.raises(SyncRefused) as info:
        push(source, storage, prefix, delete=True, max_delete=1)
    assert info.value.code == "TooManyDeletions"
    assert [path for path, _ in sizes(storage, prefix)] == all_paths

    empty_folder = tmp_path / "empty"
    empty_folder.mkdir()
    with pytest.raises(SyncRefused) as info:
        push(empty_folder, storage, prefix, delete=True)
    assert info.value.code == "EmptySource"
    assert [path for path, _ in sizes(storage, prefix)] == all_paths

    # Deletions within the limit go ahead, except for excluded objects
    results = push(source, storage, prefix, delete=True, exclude=("a.txt",))
    assert rows(results) == [
        ("sub/b.txt", "delete", "extraneous", "done"),
        ("sub/deep/c.bin", "none", "unchanged", ""),
    ]
    assert [path for path, _ in sizes(storage, prefix)] == ["a.txt", "sub/deep/c.bin"]


def test_virtual_bucket_follows_the_contract(tmp_path, storage, prefix):
    """The virtual bucket is made and listed by the browser host, which is what the package
    has in place of createVirtualBucket of the MATLAB toolbox."""
    source = tmp_path / "source"
    write(source, "a.txt", b"abc")
    write(source, "sub/b.txt", b"hello")
    push(source, storage, prefix)
    virtual_bucket = tmp_path / "virtual"

    # The prefix without its trailing "/" is read as the folder, and the
    # files land below the root, not below a prefix folder
    list_into_virtual_bucket(virtual_bucket, prefix.rstrip("/"), storage)
    assert (virtual_bucket / "a.txt").stat().st_size == 0
    assert (virtual_bucket / "sub" / "b.txt").stat().st_size == 0
    assert not (virtual_bucket / "live-test").exists(), (
        "The prefix must not become a folder of the virtual bucket."
    )

    info = read_bucket_info(virtual_bucket)
    assert info is not None, "The listing wrote no bucket.json."
    assert (info.bucket, info.prefix) == (BUCKET, prefix)
    assert abs(info.listed_at - datetime.now(timezone.utc)) < CLOCK_TOLERANCE
    first_listed_at = info.listed_at

    manifest = read_manifest(virtual_bucket)
    assert manifest is not None, "The listing wrote no manifest."
    assert [(e.path, e.bytes) for e in manifest] == [("a.txt", 3), ("sub/b.txt", 5)]
    assert all(e.modified_time is not None for e in manifest)
    assert [e.hash for e in manifest] == [
        compute_md5(source / "a.txt"),
        compute_md5(source / "sub" / "b.txt"),
    ]

    # A sync to the bucket leaves the placeholders alone
    results = push(virtual_bucket, storage, prefix)
    assert [(r.action, r.reason) for r in results] == [("none", "placeholder")] * 2
    assert sizes(storage, prefix) == [("a.txt", 3), ("sub/b.txt", 5)], (
        "A placeholder replaced its object."
    )

    # A sync of another folder of the bucket is refused
    with pytest.raises(SyncRefused) as refusal:
        push(virtual_bucket, storage, prefix + "other/")
    assert refusal.value.code == "OtherBucket"

    # The placeholder of an object removed since the listing is deleted by
    # the next listing
    storage.delete_object(BUCKET, prefix + "sub/b.txt")
    list_into_virtual_bucket(virtual_bucket, prefix, storage)
    assert not (virtual_bucket / "sub" / "b.txt").exists()
    assert (virtual_bucket / "a.txt").stat().st_size == 0
    assert [e.path for e in read_manifest(virtual_bucket)] == ["a.txt"]

    # A sync from the bucket fills the placeholder and records its listing
    results = pull(virtual_bucket, storage, prefix)
    assert rows(results) == [("a.txt", "download", "size", "done")]
    assert (virtual_bucket / "a.txt").read_bytes() == b"abc"
    assert read_bucket_info(virtual_bucket).listed_at >= first_listed_at

    # A file changed locally is uploaded, and the manifest records its
    # checksum with no time, since its object has the time of the upload
    # only from the next listing on
    local_file = virtual_bucket / "a.txt"
    local_file.write_bytes(b"abcd")
    results = push(virtual_bucket, storage, prefix)
    assert rows(results) == [("a.txt", "upload", "size", "done")]
    assert sizes(storage, prefix) == [("a.txt", 4)]
    manifest = read_manifest(virtual_bucket)
    assert [(e.path, e.bytes, e.modified_time, e.hash) for e in manifest] == [
        ("a.txt", 4, None, compute_md5(local_file))
    ]

    # The checksum tells the pushed file from one changed since, which the
    # browser shows as downloaded and differs
    assert file_states(virtual_bucket) == [("a.txt", "downloaded")]
    local_file.write_bytes(b"abce")
    assert file_states(virtual_bucket) == [("a.txt", "differs")]


class InterruptedUpload(Exception):
    """The test stopped the upload after the parts it allowed."""


class PartSender:
    """Makes the HTTP sessions that send the parts of a multipart upload, and counts the parts.

    ebrains_drive sends each part with a PUT of a session it creates for
    the upload. A part beyond max_parts raises InterruptedUpload before it
    is sent, which interrupts the upload the way a lost connection does.
    """

    def __init__(self) -> None:
        self.parts_sent = 0
        self.max_parts = math.inf

    def session(self) -> requests.Session:
        sender = self

        class Session(requests.Session):
            def put(self, url, **kwargs):
                if sender.parts_sent >= sender.max_parts:
                    raise InterruptedUpload(
                        f"The upload was interrupted after {sender.parts_sent} part(s)."
                    )
                sender.parts_sent += 1
                return super().put(url, **kwargs)

        return Session()


def test_multipart_upload_resumes_from_its_manifest(tmp_path, storage, prefix, monkeypatch):
    """Three parts, the last one smaller than the others. The thresholds are lowered so that a
    file of a few MiB takes the multipart path, which ebrains_drive resumes from the manifest it
    keeps next to the file."""
    part_size = 5 * 2**20
    file_size = 2 * part_size + 2**20
    source_file = tmp_path / "large.bin"
    pattern = bytes(range(251))
    source_file.write_bytes((pattern * (file_size // len(pattern) + 1))[:file_size])
    manifest_file = Path(f"{source_file}{MULTIPART_MANIFEST_SUFFIX}")
    object_name = prefix + "large.bin"

    monkeypatch.setattr("ebrains_bucket_sync.storage.EBRAINS_DRIVE_MULTIPART_THRESHOLD", 2**20)
    monkeypatch.setattr(ebrains_drive.bucket, "EBRAINS_DRIVE_MULTIPART_THRESHOLD", 2**20)
    monkeypatch.setattr(ebrains_drive.bucket, "EBRAINS_DRIVE_MULTIPART_CHUNK_SIZE", part_size)
    sender = PartSender()
    monkeypatch.setattr(
        ebrains_drive.bucket,
        "requests",
        SimpleNamespace(Session=sender.session, request=requests.request),
    )

    # The upload is interrupted after its first part
    sender.max_parts = 1
    with pytest.raises(InterruptedUpload):
        storage.upload(BUCKET, object_name, source_file)
    assert manifest_file.is_file(), "The interrupted upload left no manifest."
    manifest = json.loads(manifest_file.read_text())
    assert manifest["upload_id"]
    assert len(manifest["etag_maps"]) == 1

    # The next call sends the two parts that are missing and assembles the object
    sender.parts_sent = 0
    sender.max_parts = math.inf
    storage.upload(BUCKET, object_name, source_file)
    assert sender.parts_sent == 2, "The resumed upload did not continue from the manifest."
    assert not manifest_file.exists(), "The manifest must be removed once the object is assembled."
    assert sizes(storage, prefix) == [("large.bin", file_size)]

    assert download_bytes(storage, object_name, tmp_path) == source_file.read_bytes(), (
        "The downloaded object differs from the source file."
    )

    # The checksum the listing reports for a multipart object is not one of
    # its content, so the sync leaves it unknown
    files = list_files(storage, prefix)
    assert [entry.path for entry in files] == ["large.bin"]
    assert files[0].hash == ""
