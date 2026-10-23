"""A pull follows the virtual bucket rules of spec/README.md#virtual-buckets."""

from datetime import datetime, timezone

import pytest

from ebrains_bucket_sync.engine import SyncRefused, sync_from_bucket, sync_to_bucket
from ebrains_bucket_sync.model import FileEntry, SyncOptions
from ebrains_bucket_sync.virtual_bucket import (
    BucketInfo,
    ManifestError,
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


def test_pull_deletes_placeholders_of_removed_objects_and_records_the_listing(folder, storage):
    make_virtual_bucket(
        folder,
        "b",
        "results/",
        manifest=[
            FileEntry("a.dat", 3, LISTED_AT),
            FileEntry("data.dat", 3, LISTED_AT),
            FileEntry("removed.dat", 3, LISTED_AT),
            FileEntry("was-empty.dat", 0, LISTED_AT),
        ],
    )
    write(folder, "a.dat", b"")
    write(folder, "data.dat", b"abc")
    write(folder, "removed.dat", b"")
    write(folder, "mine.dat", b"")
    write(folder, "was-empty.dat", b"")
    storage.put("b", "results/a.dat", b"xyz", last_modified=OBJECT_TIME)
    storage.put("b", "results/skipped.tmp", b"t", last_modified=OBJECT_TIME)
    storage.put("b", "results/.ebrains/manifest.csv", b"m")
    before = datetime.now(timezone.utc).replace(microsecond=0)

    results = sync_from_bucket(
        "b", folder, storage, SyncOptions(prefix="results", exclude=["*.tmp"]), **NO_WAIT
    )

    assert statuses(results) == {
        "a.dat": ("download", "size", "done"),
        "data.dat": ("none", "extraneous", ""),
        "mine.dat": ("none", "extraneous", ""),
        "removed.dat": ("delete", "placeholder", "done"),
        "was-empty.dat": ("none", "extraneous", ""),
    }
    assert (folder / "a.dat").read_bytes() == b"xyz"
    assert not (folder / "removed.dat").exists()
    assert (folder / "data.dat").read_bytes() == b"abc"
    assert (folder / "mine.dat").is_file() and (folder / "was-empty.dat").is_file()
    # The manifest is the listing before exclude patterns
    assert read_manifest(folder) == [
        FileEntry("a.dat", 3, utc(2024, 6, 1, 12), md5_of(b"xyz")),
        FileEntry("skipped.tmp", 1, utc(2024, 6, 1, 12), md5_of(b"t")),
    ]
    info = read_bucket_info(folder)
    assert (info.bucket, info.prefix) == ("b", "results/")
    assert before <= info.listed_at <= datetime.now(timezone.utc)
    assert metadata_files(folder) == ["bucket.json", "manifest.csv"]


def test_downloaded_placeholder_is_unchanged_to_the_next_sync_either_way(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.dat", 3, LISTED_AT)])
    write(folder, "a.dat", b"")
    storage.put("b", "a.dat", b"xyz", last_modified=OBJECT_TIME)

    sync_from_bucket("b", folder, storage, **NO_WAIT)

    assert statuses(sync_to_bucket(folder, "b", storage, **NO_WAIT)) == {
        "a.dat": ("none", "unchanged", "")
    }
    assert statuses(sync_from_bucket("b", folder, storage, **NO_WAIT)) == {
        "a.dat": ("none", "unchanged", "")
    }


def test_manifest_path_outside_the_folder_stops_the_pull_before_any_change(folder, storage):
    # A manifest entry that names a path leaving the folder makes the
    # manifest unusable, so the pull stops before it lists or changes anything.
    outside = folder.parent / "outside.txt"
    outside.write_bytes(b"")
    make_virtual_bucket(
        folder,
        "b",
        manifest=[FileEntry("../outside.txt", 3, LISTED_AT), FileEntry("gone.txt", 3, LISTED_AT)],
    )
    write(folder, "gone.txt", b"")
    storage.put("b", "../outside.txt", b"abc")
    outside_time = outside.stat().st_mtime_ns
    manifest_text = (folder / ".ebrains" / "manifest.csv").read_bytes()

    with pytest.raises(ManifestError, match="does not stay below the folder"):
        sync_from_bucket("b", folder, storage, SyncOptions(delete=True), **NO_WAIT)

    assert outside.is_file() and outside.read_bytes() == b""
    assert outside.stat().st_mtime_ns == outside_time
    assert (folder / "gone.txt").is_file()
    assert storage.downloaded == []
    assert (folder / ".ebrains" / "manifest.csv").read_bytes() == manifest_text


def test_placeholder_deletions_count_for_the_refusal_rules(folder, storage):
    manifest = [FileEntry("a.dat", 3, LISTED_AT), FileEntry("gone.dat", 3, LISTED_AT)]
    make_virtual_bucket(folder, "b", manifest=manifest)
    write(folder, "a.dat", b"")
    write(folder, "gone.dat", b"")
    storage.put("b", "a.dat", b"xyz")

    with pytest.raises(SyncRefused) as info:
        sync_from_bucket("b", folder, storage, SyncOptions(max_delete=0))

    assert info.value.code == "TooManyDeletions"
    assert (folder / "gone.dat").is_file() and (folder / "a.dat").read_bytes() == b""
    assert read_manifest(folder) == manifest
    assert read_bucket_info(folder) == BucketInfo("b", "", LISTED_AT)


def test_excluded_placeholder_of_a_removed_object_is_kept(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("gone.dat", 3, LISTED_AT)])
    write(folder, "gone.dat", b"")
    write(folder, "a.txt", b"a")
    storage.put("b", "a.txt", b"a", last_modified=FakeStorage.UPLOADED_BEFORE_LOCAL)

    results = sync_from_bucket("b", folder, storage, SyncOptions(exclude=["gone.dat"]), **NO_WAIT)

    assert [r.path for r in results] == ["a.txt"]
    assert (folder / "gone.dat").is_file()


def test_failed_download_holds_back_the_deletion_of_placeholders(folder, storage):
    manifest = [FileEntry("a.dat", 3, LISTED_AT), FileEntry("gone.dat", 3, LISTED_AT)]
    make_virtual_bucket(folder, "b", manifest=manifest)
    write(folder, "a.dat", b"")
    write(folder, "gone.dat", b"")
    storage.put("b", "a.dat", b"xyz")
    storage.errors["a.dat"] = ValueError("malformed")

    results = sync_from_bucket("b", folder, storage, **NO_WAIT)

    assert statuses(results) == {
        "a.dat": ("download", "size", "failed"),
        "gone.dat": ("delete", "placeholder", "skipped"),
    }
    assert (folder / "a.dat").read_bytes() == b""
    assert (folder / "gone.dat").is_file()
    # The placeholder keeps its manifest entry, so the next push still
    # knows it for one instead of uploading an empty object
    assert read_manifest(folder) == [
        FileEntry("a.dat", 3, utc(2999, 1, 1), md5_of(b"xyz")),
        FileEntry("gone.dat", 3, LISTED_AT),
    ]
    del storage.errors["a.dat"]
    pushed = sync_to_bucket(folder, "b", storage, **NO_WAIT)
    assert statuses(pushed)["gone.dat"] == ("none", "placeholder", "")
    assert storage.uploaded == []


def test_placeholder_that_cannot_be_deleted_keeps_its_manifest_entry(folder, storage, monkeypatch):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("gone.dat", 3, LISTED_AT)])
    write(folder, "gone.dat", b"")
    write(folder, "a.txt", b"a")
    storage.put("b", "a.txt", b"a", last_modified=FakeStorage.UPLOADED_BEFORE_LOCAL)

    def refuse_unlink(path, *args, **kwargs):
        raise PermissionError(f"Not permitted: {path}")

    monkeypatch.setattr("pathlib.Path.unlink", refuse_unlink)
    results = sync_from_bucket("b", folder, storage, **NO_WAIT)

    assert statuses(results)["gone.dat"] == ("delete", "placeholder", "failed")
    assert read_manifest(folder) == [
        FileEntry("a.txt", 1, utc(2000, 1, 1), md5_of(b"a")),
        FileEntry("gone.dat", 3, LISTED_AT),
    ]


def test_placeholder_that_holds_data_by_the_time_of_its_deletion_is_kept(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("gone.dat", 3, LISTED_AT)])
    write(folder, "gone.dat", b"")
    write(folder, "a.txt", b"a")
    storage.put("b", "a.txt", b"a", last_modified=FakeStorage.UPLOADED_BEFORE_LOCAL)

    def write_into_placeholder(event):
        if event.kind == "planned":
            (folder / "gone.dat").write_bytes(b"new data")

    results = sync_from_bucket("b", folder, storage, on_event=write_into_placeholder, **NO_WAIT)

    assert statuses(results)["gone.dat"] == ("delete", "placeholder", "failed")
    assert "holds data now" in results[1].message
    assert (folder / "gone.dat").read_bytes() == b"new data"
    # The file is no placeholder any more, so the manifest does not keep its entry
    assert [entry.path for entry in read_manifest(folder)] == ["a.txt"]


def test_pull_of_another_bucket_or_folder_is_refused_before_listing(folder, storage):
    make_virtual_bucket(folder, "b", "results/")
    write(folder, "a.txt", b"")
    events = []

    with pytest.raises(SyncRefused) as info:
        sync_from_bucket(
            "other", folder, storage, SyncOptions(prefix="results"), on_event=events.append
        )
    assert info.value.code == "OtherBucket"
    assert info.value.reason == (
        "The folder is a virtual bucket of bucket 'b', folder 'results/', "
        "not of bucket 'other', folder 'results/'."
    )

    with pytest.raises(SyncRefused) as info:
        sync_from_bucket("b", folder, storage, on_event=events.append)
    assert info.value.code == "OtherBucket"

    assert storage.listings == [] and storage.downloaded == []
    assert events == []
    assert metadata_files(folder) == ["bucket.json"]


def test_dry_run_of_another_bucket_shows_the_plan_with_the_refusal(folder, storage):
    make_virtual_bucket(folder, "b")
    write(folder, "extra.txt", b"e")
    storage.put("other", "a.txt", b"a")

    results = sync_from_bucket("other", folder, storage, SyncOptions(dry_run=True, delete=True))

    assert statuses(results) == {
        "a.txt": ("download", "new", "skipped"),
        "extra.txt": ("delete", "extraneous", "skipped"),
    }
    assert all(r.message.startswith("The folder is a virtual bucket of") for r in results)
    assert storage.downloaded == []
    assert (folder / "extra.txt").is_file()
    assert read_bucket_info(folder) == BucketInfo("b")


def test_dry_run_of_a_virtual_bucket_writes_nothing(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("gone.dat", 3, LISTED_AT)])
    write(folder, "gone.dat", b"")
    write(folder, "a.dat", b"")
    storage.put("b", "a.dat", b"xyz")

    results = sync_from_bucket("b", folder, storage, SyncOptions(dry_run=True))

    assert statuses(results) == {
        "a.dat": ("download", "size", "planned"),
        "gone.dat": ("delete", "placeholder", "planned"),
    }
    assert (folder / "gone.dat").is_file() and (folder / "a.dat").read_bytes() == b""
    assert read_manifest(folder) == [FileEntry("gone.dat", 3, LISTED_AT)]
    assert read_bucket_info(folder) == BucketInfo("b", "", LISTED_AT)
