import math

import pytest

from ebrains_sync.engine import SyncEvent, SyncRefused, sync_to_bucket
from ebrains_sync.model import SyncOptions, SyncWarning

from .conftest import FakeStorage, write

NO_WAIT = {"sleep": lambda seconds: None}


def statuses(results):
    return {r.path: (r.action, r.reason, r.status) for r in results}


def test_uploads_new_and_changed_files_and_keeps_extraneous(folder, storage: FakeStorage):
    write(folder, "new.txt", b"n")
    write(folder, "same.txt", b"ss")
    write(folder, "grown.txt", b"ggg")
    storage.put("b", "same.txt", b"ss")
    storage.put("b", "grown.txt", b"g")
    storage.put("b", "extra.txt", b"e")

    results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert statuses(results) == {
        "extra.txt": ("none", "extraneous", ""),
        "grown.txt": ("upload", "size", "done"),
        "new.txt": ("upload", "new", "done"),
        "same.txt": ("none", "unchanged", ""),
    }
    assert storage.content("b", "grown.txt") == b"ggg"
    assert storage.content("b", "new.txt") == b"n"
    assert "extra.txt" in storage.buckets["b"]


def test_uploads_file_changed_after_the_object_was_uploaded(folder, storage: FakeStorage):
    write(folder, "a.txt", b"a")
    storage.put("b", "a.txt", b"x", last_modified=FakeStorage.UPLOADED_BEFORE_LOCAL)

    results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert statuses(results)["a.txt"] == ("upload", "newer", "done")


def test_object_names_carry_the_prefix_and_paths_do_not(folder, storage: FakeStorage):
    write(folder, "sub/a.txt", b"a")
    storage.put("b", "results/old.txt", b"o")
    storage.put("b", "elsewhere/x.txt", b"x")

    results = sync_to_bucket(
        folder, "b", storage, SyncOptions(prefix="/results", delete=True), **NO_WAIT
    )

    assert [r.path for r in results] == ["old.txt", "sub/a.txt"]
    assert storage.uploaded == [("b", "results/sub/a.txt")]
    assert storage.deleted == [("b", "results/old.txt")]
    assert "elsewhere/x.txt" in storage.buckets["b"]


def test_dry_run_changes_nothing(folder, storage: FakeStorage):
    write(folder, "a.txt", b"a")
    storage.put("b", "extra.txt", b"e")

    results = sync_to_bucket(folder, "b", storage, SyncOptions(dry_run=True, delete=True))

    assert statuses(results) == {
        "a.txt": ("upload", "new", "planned"),
        "extra.txt": ("delete", "extraneous", "planned"),
    }
    assert storage.uploaded == [] and storage.deleted == []


def test_dry_run_shows_a_refusal_instead_of_stopping(folder, storage: FakeStorage):
    storage.put("b", "extra.txt", b"e")
    events = []

    results = sync_to_bucket(
        folder, "b", storage, SyncOptions(dry_run=True, delete=True), on_event=events.append
    )

    assert results[0].status == "skipped"
    assert "would delete all 1 file(s)" in results[0].message
    assert events[0].kind == "planned" and "would delete all" in events[0].message
    assert storage.deleted == []


def test_deletes_extraneous_objects_when_asked(folder, storage: FakeStorage):
    write(folder, "a.txt", b"a")
    storage.put("b", "extra.txt", b"e")

    results = sync_to_bucket(folder, "b", storage, SyncOptions(delete=True), **NO_WAIT)

    assert statuses(results)["extra.txt"] == ("delete", "extraneous", "done")
    assert storage.deleted == [("b", "extra.txt")]


def test_leaves_excluded_files_alone_on_both_sides(folder, storage: FakeStorage):
    write(folder, "keep.txt", b"k")
    write(folder, "scratch.tmp", b"t")
    storage.put("b", "old.tmp", b"o")
    options = SyncOptions(delete=True, exclude=["*.tmp"])

    results = sync_to_bucket(folder, "b", storage, options, **NO_WAIT)

    assert [r.path for r in results] == ["keep.txt"]
    assert "old.tmp" in storage.buckets["b"]


def test_never_uploads_a_multipart_manifest(folder, storage: FakeStorage):
    write(folder, "big.dat", b"d")
    write(folder, "big.dat.multipart_manifest.json", b"{}")

    results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert [r.path for r in results] == ["big.dat"]


def test_refuses_to_empty_bucket_from_empty_folder(folder, storage: FakeStorage):
    storage.put("b", "a.txt", b"a")

    with pytest.raises(SyncRefused) as info:
        sync_to_bucket(folder, "b", storage, SyncOptions(delete=True))

    assert info.value.code == "EmptySource"
    assert storage.deleted == []


def test_stops_before_more_deletions_than_allowed(folder, storage: FakeStorage):
    write(folder, "a.txt", b"a")
    storage.put("b", "x.txt", b"x")
    storage.put("b", "y.txt", b"y")

    with pytest.raises(SyncRefused) as info:
        sync_to_bucket(folder, "b", storage, SyncOptions(delete=True, max_delete=1))

    assert info.value.code == "TooManyDeletions"
    assert storage.deleted == []


def test_failed_upload_is_retried_then_holds_back_deletions(folder, storage: FakeStorage):
    write(folder, "bad.txt", b"b")
    write(folder, "good.txt", b"g")
    storage.put("b", "extra.txt", b"e")
    storage.errors["bad.txt"] = ConnectionError("connection lost")
    waits = []

    results = sync_to_bucket(
        folder, "b", storage, SyncOptions(delete=True), attempts=3, sleep=waits.append
    )

    assert statuses(results) == {
        "bad.txt": ("upload", "new", "failed"),
        "extra.txt": ("delete", "extraneous", "skipped"),
        "good.txt": ("upload", "new", "done"),
    }
    assert results[0].message == "connection lost"
    assert results[1].message == "Not deleted, since a file failed to upload."
    assert storage.upload_attempts["bad.txt"] == 3
    assert waits == [1.0, 2.0]
    assert storage.deleted == []


def test_error_that_is_not_transient_is_not_retried(folder, storage: FakeStorage):
    write(folder, "bad.txt", b"b")
    storage.errors["bad.txt"] = ValueError("malformed")

    results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert results[0].status == "failed"
    assert storage.upload_attempts["bad.txt"] == 1


def test_by_checksum_uploads_changed_content_only(folder, storage: FakeStorage):
    write(folder, "edited.txt", b"abd")
    write(folder, "same.txt", b"xyz")
    write(folder, "no-remote-hash.txt", b"qqq")
    storage.put("b", "edited.txt", b"abc")
    storage.put("b", "same.txt", b"xyz")
    storage.put("b", "no-remote-hash.txt", b"ppp", hash=None)
    hashed = []

    def md5(path):
        hashed.append(path.name)
        from ebrains_sync.local import compute_md5

        return compute_md5(path)

    results = sync_to_bucket(
        folder, "b", storage, SyncOptions(comparison="Checksum"), md5=md5, **NO_WAIT
    )

    assert statuses(results) == {
        "edited.txt": ("upload", "checksum", "done"),
        "no-remote-hash.txt": ("none", "unchanged", ""),
        "same.txt": ("none", "unchanged", ""),
    }
    assert sorted(hashed) == ["edited.txt", "same.txt"]


def test_warns_when_the_listing_has_no_times(folder, storage: FakeStorage):
    write(folder, "a.txt", b"a")
    storage.put("b", "a.txt", b"x", last_modified=None)

    with pytest.warns(SyncWarning, match="no modification times"):
        results = sync_to_bucket(folder, "b", storage, **NO_WAIT)

    assert statuses(results)["a.txt"] == ("none", "unchanged", "")


def test_reports_events_in_order(folder, storage: FakeStorage):
    write(folder, "a.txt", b"a")
    write(folder, "b.txt", b"b")
    storage.put("b", "extra.txt", b"e")
    events: list[SyncEvent] = []

    sync_to_bucket(
        folder,
        "b",
        storage,
        SyncOptions(delete=True, workers=1),
        on_event=events.append,
        **NO_WAIT,
    )

    assert [(e.kind, e.path, e.index, e.total) for e in events] == [
        ("planned", "", 0, 0),
        ("upload", "a.txt", 1, 2),
        ("uploaded", "a.txt", 1, 2),
        ("upload", "b.txt", 2, 2),
        ("uploaded", "b.txt", 2, 2),
        ("delete", "extra.txt", 1, 1),
        ("deleted", "extra.txt", 1, 1),
        ("finished", "", 0, 0),
    ]
    assert len(events[0].results) == 3
    assert all(r.status == "done" for r in events[-1].results)


def test_uploads_with_several_workers(folder, storage: FakeStorage):
    for i in range(20):
        write(folder, f"f{i:02d}.txt", bytes([i]))

    results = sync_to_bucket(folder, "b", storage, SyncOptions(workers=5), **NO_WAIT)

    assert all(r.status == "done" for r in results)
    assert len(storage.uploaded) == 20


def test_missing_folder_is_an_error(tmp_path, storage: FakeStorage):
    with pytest.raises(NotADirectoryError):
        sync_to_bucket(tmp_path / "nope", "b", storage)


def test_options_are_validated():
    with pytest.raises(ValueError):
        SyncOptions(comparison="Hash")
    with pytest.raises(ValueError):
        SyncOptions(max_delete=-1)
    with pytest.raises(ValueError):
        SyncOptions(workers=0)
    assert SyncOptions().max_delete == math.inf
