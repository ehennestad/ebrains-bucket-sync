"""A push follows the virtual bucket rules of spec/README.md#virtual-buckets."""

from datetime import datetime, timezone

import pytest

from ebrains_bucket_sync.engine import SyncRefused, sync_to_bucket
from ebrains_bucket_sync.model import FileEntry, SyncOptions
from ebrains_bucket_sync.paths import DOWNLOAD_PART_SUFFIX
from ebrains_bucket_sync.virtual_bucket import (
    BucketInfo,
    read_bucket_info,
    read_manifest,
    write_bucket_info,
    write_manifest,
)

from .conftest import FakeStorage, md5_of, utc, write

NO_WAIT = {"sleep": lambda seconds: None}
LISTED_AT = utc(2024, 1, 2, 3, 4, 5)
OBJECT_TIME = "2024-06-01T12:00:00"


def statuses(results):
    return {r.path: (r.action, r.reason, r.status) for r in results}


def make_virtual_bucket(folder, bucket="b", prefix="", manifest=None):
    """Lay out folder as createVirtualBucket does: a bucket.json, and a manifest once listed."""
    listed_at = None if manifest is None else LISTED_AT
    write_bucket_info(folder, BucketInfo(bucket, prefix, listed_at))
    if manifest is not None:
        write_manifest(folder, manifest)


def metadata_files(folder):
    return sorted(path.name for path in (folder / ".ebrains").iterdir())


def test_metadata_folder_is_left_out_of_both_sides_of_any_sync(folder, storage: FakeStorage):
    write(folder, "a.txt", b"a")
    write(folder, ".ebrains/notes.txt", b"n")
    storage.put("b", "results/.ebrains/manifest.csv", b"m")
    storage.put("b", "results/.ebrains", b"")

    results = sync_to_bucket(
        folder, "b", storage, SyncOptions(prefix="results", delete=True), **NO_WAIT
    )

    assert [r.path for r in results] == ["a.txt"]
    assert storage.uploaded == [("b", "results/a.txt")]
    assert storage.deleted == []
    assert metadata_files(folder) == ["notes.txt"]


def test_manifest_does_not_record_the_leftovers_of_transfers(folder, storage: FakeStorage):
    make_virtual_bucket(folder, "b")
    write(folder, "a.dat", b"a")
    write(folder, f"a.dat.k3x9{DOWNLOAD_PART_SUFFIX}", b"partial")
    storage.put("b", f"old.dat.z1{DOWNLOAD_PART_SUFFIX}", b"p")
    storage.put("b", "big.bin.multipart_manifest.json", b"{}")

    results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert statuses(results) == {"a.dat": ("upload", "new", "done")}
    assert [entry.path for entry in read_manifest(folder)] == ["a.dat"]


def test_sync_of_another_bucket_or_folder_is_refused_before_listing(folder, storage: FakeStorage):
    make_virtual_bucket(folder, "b", "results/")
    write(folder, "a.txt", b"a")
    events = []

    with pytest.raises(SyncRefused) as info:
        sync_to_bucket(
            folder, "other", storage, SyncOptions(prefix="results"), on_event=events.append
        )
    assert info.value.code == "OtherBucket"
    assert info.value.reason == (
        "The folder is a virtual bucket of bucket 'b', folder 'results/', "
        "not of bucket 'other', folder 'results/'."
    )

    with pytest.raises(SyncRefused) as info:
        sync_to_bucket(folder, "b", storage, on_event=events.append)
    assert info.value.code == "OtherBucket"

    assert storage.listings == [] and storage.uploaded == []
    assert events == []


def test_dry_run_of_another_bucket_shows_the_plan_with_the_refusal(folder, storage: FakeStorage):
    make_virtual_bucket(folder, "b")
    write(folder, "a.txt", b"a")
    storage.put("other", "extra.txt", b"e")
    events = []

    results = sync_to_bucket(
        folder, "other", storage, SyncOptions(dry_run=True, delete=True), on_event=events.append
    )

    assert statuses(results) == {
        "a.txt": ("upload", "new", "skipped"),
        "extra.txt": ("delete", "extraneous", "skipped"),
    }
    assert all(r.message.startswith("The folder is a virtual bucket of") for r in results)
    assert events[0].kind == "planned" and events[0].message == results[0].message
    assert storage.uploaded == [] and storage.deleted == []
    assert read_bucket_info(folder) == BucketInfo("b")
    assert metadata_files(folder) == ["bucket.json"]


def test_placeholders_are_left_alone_by_the_listing_without_a_manifest(folder, storage):
    make_virtual_bucket(folder, "b")
    write(folder, "placeholder.dat", b"")
    write(folder, "edited.dat", b"abcd")
    write(folder, "empty.dat", b"")
    write(folder, "fresh.dat", b"")
    storage.put("b", "placeholder.dat", b"xyz")
    storage.put("b", "edited.dat", b"abc")
    storage.put("b", "empty.dat", b"")

    results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert statuses(results) == {
        "edited.dat": ("upload", "size", "done"),
        "empty.dat": ("none", "unchanged", ""),
        "fresh.dat": ("upload", "new", "done"),
        "placeholder.dat": ("none", "placeholder", ""),
    }
    assert storage.content("b", "placeholder.dat") == b"xyz"


def test_placeholder_of_an_object_removed_since_the_manifest_is_not_recreated(folder, storage):
    manifest = [
        FileEntry("placeholder.dat", 3, LISTED_AT, md5_of(b"xyz")),
        FileEntry("removed.dat", 3, LISTED_AT, md5_of(b"abc")),
    ]
    make_virtual_bucket(folder, "b", manifest=manifest)
    write(folder, "placeholder.dat", b"")
    write(folder, "removed.dat", b"")
    storage.put("b", "placeholder.dat", b"xyz")

    results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert statuses(results) == {
        "placeholder.dat": ("none", "placeholder", ""),
        "removed.dat": ("none", "placeholder", ""),
    }
    assert storage.uploaded == []


def test_placeholder_of_a_removed_object_stays_protected_in_the_next_push(folder, storage):
    removed = FileEntry("removed.dat", 3, LISTED_AT, md5_of(b"abc"))
    excluded = FileEntry("excluded.dat", 3, LISTED_AT, md5_of(b"def"))
    make_virtual_bucket(folder, "b", manifest=[excluded, removed])
    write(folder, "removed.dat", b"")
    write(folder, "excluded.dat", b"")

    sync_to_bucket(folder, "b", storage, SyncOptions(exclude=("excluded.dat",)), **NO_WAIT)
    assert read_manifest(folder) == [excluded, removed]

    results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert statuses(results) == {
        "excluded.dat": ("none", "placeholder", ""),
        "removed.dat": ("none", "placeholder", ""),
    }
    assert storage.uploaded == []


def test_plain_folder_uploads_its_empty_files_and_gets_no_metadata(folder, storage):
    write(folder, "a.dat", b"")
    storage.put("b", "a.dat", b"xyz")

    results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert statuses(results) == {"a.dat": ("upload", "size", "done")}
    assert storage.content("b", "a.dat") == b""
    assert not (folder / ".ebrains").exists()


def test_push_records_the_listing_as_the_manifest_after_a_failed_upload(folder, storage):
    make_virtual_bucket(folder, "b", "results/", manifest=[FileEntry("stale.dat", 9, LISTED_AT)])
    write(folder, "good.txt", b"g")
    write(folder, "bad.txt", b"b")
    write(folder, "placeholder.dat", b"")
    storage.put("b", "results/placeholder.dat", b"xyz", last_modified=OBJECT_TIME)
    storage.put("b", "results/extra.txt", b"e", last_modified=OBJECT_TIME)
    storage.put("b", "results/.ebrains/manifest.csv", b"m")
    storage.put("b", "results/skipped.tmp", b"t", last_modified=OBJECT_TIME)
    storage.errors["results/bad.txt"] = ValueError("malformed")
    before = datetime.now(timezone.utc).replace(microsecond=0)

    results = sync_to_bucket(
        folder,
        "b",
        storage,
        SyncOptions(prefix="results", delete=True, exclude=["*.tmp"]),
        attempts=1,
        **NO_WAIT,
    )

    assert statuses(results) == {
        "bad.txt": ("upload", "new", "failed"),
        "extra.txt": ("delete", "extraneous", "skipped"),
        "good.txt": ("upload", "new", "done"),
        "placeholder.dat": ("none", "placeholder", ""),
    }
    # The manifest is the listing before exclude patterns, plus the uploaded
    # file with an unknown time, without the failed one.
    assert read_manifest(folder) == [
        FileEntry("extra.txt", 1, utc(2024, 6, 1, 12), md5_of(b"e")),
        FileEntry("good.txt", 1, None, md5_of(b"g")),
        FileEntry("placeholder.dat", 3, utc(2024, 6, 1, 12), md5_of(b"xyz")),
        FileEntry("skipped.tmp", 1, utc(2024, 6, 1, 12), md5_of(b"t")),
    ]
    info = read_bucket_info(folder)
    assert (info.bucket, info.prefix) == ("b", "results/")
    assert before <= info.listed_at <= datetime.now(timezone.utc)
    assert metadata_files(folder) == ["bucket.json", "manifest.csv"]


def test_push_takes_deleted_objects_out_of_the_manifest(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("extra.txt", 1, LISTED_AT)])
    write(folder, "new.txt", b"nn")
    storage.put("b", "extra.txt", b"e")

    results = sync_to_bucket(folder, "b", storage, SyncOptions(delete=True), **NO_WAIT)

    assert statuses(results) == {
        "extra.txt": ("delete", "extraneous", "done"),
        "new.txt": ("upload", "new", "done"),
    }
    assert read_manifest(folder) == [FileEntry("new.txt", 2, None, md5_of(b"nn"))]


def test_uploaded_entries_carry_the_checksum_the_comparison_computed(folder, storage):
    make_virtual_bucket(folder, "b")
    write(folder, "edited.txt", b"abd")
    storage.put("b", "edited.txt", b"abc")

    sync_to_bucket(folder, "b", storage, SyncOptions(comparison="Checksum"), **NO_WAIT)

    assert read_manifest(folder) == [FileEntry("edited.txt", 3, None, md5_of(b"abd"))]


def test_upload_checksum_is_read_before_the_upload_and_a_vanished_file_fails(folder, storage):
    make_virtual_bucket(folder, "b")
    write(folder, "good.txt", b"g")
    write(folder, "vanished.txt", b"v")
    read = []

    def md5(path):
        read.append(path.name)
        if path.name == "vanished.txt":
            raise FileNotFoundError(f"No such file: {path}")
        # Read before the upload, so the checksum is of the content that is sent
        assert ("b", path.name) not in storage.uploaded
        return md5_of(path.read_bytes())

    results = sync_to_bucket(folder, "b", storage, md5=md5, **NO_WAIT)

    assert statuses(results) == {
        "good.txt": ("upload", "new", "done"),
        "vanished.txt": ("upload", "new", "failed"),
    }
    assert "No such file" in results[1].message
    assert sorted(read) == ["good.txt", "vanished.txt"]
    assert storage.uploaded == [("b", "good.txt")]
    assert read_manifest(folder) == [FileEntry("good.txt", 1, None, md5_of(b"g"))]


def test_dry_run_of_a_virtual_bucket_writes_nothing(folder, storage):
    make_virtual_bucket(folder, "b")
    write(folder, "placeholder.dat", b"")
    write(folder, "a.txt", b"a")
    storage.put("b", "placeholder.dat", b"xyz")

    results = sync_to_bucket(folder, "b", storage, SyncOptions(dry_run=True))

    assert statuses(results) == {
        "a.txt": ("upload", "new", "planned"),
        "placeholder.dat": ("none", "placeholder", ""),
    }
    assert read_bucket_info(folder) == BucketInfo("b")
    assert metadata_files(folder) == ["bucket.json"]


def test_refused_sync_of_a_virtual_bucket_writes_nothing(folder, storage):
    make_virtual_bucket(folder, "b")
    write(folder, "a.txt", b"a")
    storage.put("b", "x.txt", b"x")
    storage.put("b", "y.txt", b"y")

    with pytest.raises(SyncRefused) as info:
        sync_to_bucket(folder, "b", storage, SyncOptions(delete=True, max_delete=1))

    assert info.value.code == "TooManyDeletions"
    assert read_bucket_info(folder) == BucketInfo("b")
    assert metadata_files(folder) == ["bucket.json"]
