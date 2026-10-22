"""sync_from_bucket: make a local folder match a bucket, or a folder of it."""

import os
import sys
from pathlib import Path

import pytest

from ebrains_bucket_sync.engine import (
    DOWNLOAD_PART_SUFFIX,
    SyncEvent,
    SyncRefused,
    sync_from_bucket,
    sync_to_bucket,
)
from ebrains_bucket_sync.local import compute_md5
from ebrains_bucket_sync.model import SyncOptions, SyncWarning

from .conftest import FakeStorage, utc, write

NO_WAIT = {"sleep": lambda seconds: None}
OBJECT_TIME = "2024-06-01T12:00:00"
OLD = FakeStorage.UPLOADED_BEFORE_LOCAL

needs_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="Symbolic links need privileges on Windows."
)


def statuses(results):
    return {r.path: (r.action, r.reason, r.status) for r in results}


def part_files(folder):
    return sorted(p.name for p in folder.rglob(f"*{DOWNLOAD_PART_SUFFIX}"))


def test_downloads_new_and_changed_objects_and_keeps_extraneous_files(folder, storage):
    write(folder, "same.txt", b"ss")
    write(folder, "grown.txt", b"g")
    write(folder, "edited.txt", b"e")
    write(folder, "extra.txt", b"x")
    storage.put("b", "new.txt", b"n", last_modified=OBJECT_TIME)
    storage.put("b", "same.txt", b"SS", last_modified=OLD)
    storage.put("b", "grown.txt", b"ggg", last_modified=OLD)
    storage.put("b", "edited.txt", b"E", last_modified=OBJECT_TIME.replace("2024", "2999"))

    results = sync_from_bucket("b", folder, storage, **NO_WAIT)

    assert statuses(results) == {
        "edited.txt": ("download", "newer", "done"),
        "extra.txt": ("none", "extraneous", ""),
        "grown.txt": ("download", "size", "done"),
        "new.txt": ("download", "new", "done"),
        "same.txt": ("none", "unchanged", ""),
    }
    assert (folder / "new.txt").read_bytes() == b"n"
    assert (folder / "grown.txt").read_bytes() == b"ggg"
    assert (folder / "edited.txt").read_bytes() == b"E"
    assert (folder / "same.txt").read_bytes() == b"ss"
    assert (folder / "extra.txt").read_bytes() == b"x"
    assert part_files(folder) == []


def test_by_size_ignores_time(folder, storage):
    write(folder, "same-size.txt", b"a")
    write(folder, "grown.txt", b"g")
    storage.put("b", "same-size.txt", b"b")
    storage.put("b", "grown.txt", b"gg", last_modified=OLD)

    results = sync_from_bucket("b", folder, storage, SyncOptions(comparison="Size"), **NO_WAIT)

    assert statuses(results) == {
        "grown.txt": ("download", "size", "done"),
        "same-size.txt": ("none", "unchanged", ""),
    }
    assert (folder / "same-size.txt").read_bytes() == b"a"


def test_by_checksum_downloads_changed_content_only(folder, storage):
    write(folder, "edited.txt", b"abd")
    write(folder, "same.txt", b"xyz")
    write(folder, "no-remote-hash.txt", b"qqq")
    storage.put("b", "edited.txt", b"abc", last_modified=OLD)
    storage.put("b", "same.txt", b"xyz")
    storage.put("b", "no-remote-hash.txt", b"ppp", hash=None, last_modified=OLD)
    hashed = []

    def md5(path):
        hashed.append(path.name)
        return compute_md5(path)

    results = sync_from_bucket(
        "b", folder, storage, SyncOptions(comparison="Checksum"), md5=md5, **NO_WAIT
    )

    assert statuses(results) == {
        "edited.txt": ("download", "checksum", "done"),
        "no-remote-hash.txt": ("none", "unchanged", ""),
        "same.txt": ("none", "unchanged", ""),
    }
    assert sorted(hashed) == ["edited.txt", "same.txt"]
    assert (folder / "edited.txt").read_bytes() == b"abc"


def test_downloads_get_the_time_of_their_object_so_a_push_finds_them_unchanged(folder, storage):
    storage.put("b", "sub/a.txt", b"abc", last_modified=OBJECT_TIME)

    sync_from_bucket("b", folder, storage, **NO_WAIT)

    assert (folder / "sub" / "a.txt").stat().st_mtime == utc(2024, 6, 1, 12).timestamp()
    pushed = sync_to_bucket(folder, "b", storage, **NO_WAIT)
    assert statuses(pushed) == {"sub/a.txt": ("none", "unchanged", "")}
    assert sync_from_bucket("b", folder, storage, **NO_WAIT)[0].action == "none"


def test_paths_are_relative_to_the_prefix(folder, storage):
    storage.put("b", "results/sub/a.txt", b"a")
    storage.put("b", "elsewhere/x.txt", b"x")

    results = sync_from_bucket("b", folder, storage, SyncOptions(prefix="/results"), **NO_WAIT)

    assert [r.path for r in results] == ["sub/a.txt"]
    assert storage.downloaded == [("b", "results/sub/a.txt")]
    assert sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*")) == [
        "sub",
        "sub/a.txt",
    ]


def test_creates_the_folder_if_it_does_not_exist(tmp_path, storage):
    target = tmp_path / "new" / "mirror"
    storage.put("b", "set/a.txt", b"abc")
    storage.put("b", "set/sub/b.txt", b"abc")

    results = sync_from_bucket("b", target, storage, SyncOptions(prefix="set"), **NO_WAIT)

    assert [(r.action, r.status) for r in results] == [("download", "done")] * 2
    assert (target / "sub" / "b.txt").read_bytes() == b"abc"


def test_dry_run_changes_nothing(tmp_path, folder, storage):
    write(folder, "a.txt", b"old")
    write(folder, "extra.txt", b"e")
    storage.put("b", "a.txt", b"newer")
    storage.put("b", "b.txt", b"b")

    results = sync_from_bucket("b", folder, storage, SyncOptions(dry_run=True, delete=True))

    assert statuses(results) == {
        "a.txt": ("download", "size", "planned"),
        "b.txt": ("download", "new", "planned"),
        "extra.txt": ("delete", "extraneous", "planned"),
    }
    assert storage.downloaded == []
    assert sorted(p.name for p in folder.iterdir()) == ["a.txt", "extra.txt"]
    assert (folder / "a.txt").read_bytes() == b"old"

    missing = tmp_path / "missing"
    sync_from_bucket("b", missing, storage, SyncOptions(dry_run=True))
    assert not missing.exists()


def test_deletes_extraneous_local_files_only_when_asked(folder, storage):
    write(folder, "a.txt", b"a")
    write(folder, "sub/extra.txt", b"e")
    storage.put("b", "a.txt", b"a", last_modified=OLD)

    kept = sync_from_bucket("b", folder, storage, **NO_WAIT)
    assert statuses(kept)["sub/extra.txt"] == ("none", "extraneous", "")
    assert (folder / "sub" / "extra.txt").is_file()

    results = sync_from_bucket("b", folder, storage, SyncOptions(delete=True), **NO_WAIT)

    assert statuses(results) == {
        "a.txt": ("none", "unchanged", ""),
        "sub/extra.txt": ("delete", "extraneous", "done"),
    }
    assert not (folder / "sub" / "extra.txt").exists()
    assert (folder / "sub").is_dir()
    assert (folder / "a.txt").is_file()


def test_refuses_to_empty_the_folder_from_an_empty_prefix(folder, storage):
    write(folder, "a.txt", b"a")
    storage.put("b", "results/x.txt", b"x")

    with pytest.raises(SyncRefused) as info:
        sync_from_bucket("b", folder, storage, SyncOptions(prefix="typo", delete=True))

    assert info.value.code == "EmptySource"
    assert (folder / "a.txt").is_file()


def test_dry_run_shows_a_refusal_instead_of_stopping(folder, storage):
    write(folder, "a.txt", b"a")
    events = []

    results = sync_from_bucket(
        "b", folder, storage, SyncOptions(dry_run=True, delete=True), on_event=events.append
    )

    assert statuses(results) == {"a.txt": ("delete", "extraneous", "skipped")}
    assert "would delete all 1 file(s)" in results[0].message
    assert events[0].kind == "planned" and "would delete all" in events[0].message


def test_stops_before_more_deletions_than_allowed(folder, storage):
    write(folder, "x.txt", b"x")
    write(folder, "y.txt", b"y")
    storage.put("b", "a.txt", b"a")

    with pytest.raises(SyncRefused) as info:
        sync_from_bucket("b", folder, storage, SyncOptions(delete=True, max_delete=1))

    assert info.value.code == "TooManyDeletions"
    assert storage.downloaded == []
    assert sorted(p.name for p in folder.iterdir()) == ["x.txt", "y.txt"]


def test_leaves_excluded_files_alone_on_both_sides(folder, storage):
    write(folder, "keep.txt", b"k")
    write(folder, "scratch.tmp", b"t")
    storage.put("b", "keep.txt", b"k", last_modified=OLD)
    storage.put("b", "old.tmp", b"o")
    options = SyncOptions(delete=True, exclude=["*.tmp"])

    results = sync_from_bucket("b", folder, storage, options, **NO_WAIT)

    assert [r.path for r in results] == ["keep.txt"]
    assert (folder / "scratch.tmp").read_bytes() == b"t"
    assert not (folder / "old.tmp").exists()


def test_where_case_is_ignored_excludes_match_objects_whatever_their_case(folder, storage):
    write(folder, "cache.tmp", b"local")
    storage.put("b", "cache.TMP", b"remote")

    results = sync_from_bucket(
        "b", folder, storage, SyncOptions(exclude=["*.tmp"]), ignore_case=True, **NO_WAIT
    )

    assert results == []
    assert (folder / "cache.tmp").read_bytes() == b"local"
    assert storage.downloaded == []


def test_where_case_is_ignored_an_object_replaces_the_file_of_its_name_in_another_case(
    folder, storage
):
    write(folder, "data.txt", b"ab")
    storage.put("b", "Data.txt", b"abc", last_modified=OLD)

    results = sync_from_bucket(
        "b", folder, storage, SyncOptions(delete=True), ignore_case=True, **NO_WAIT
    )

    assert statuses(results) == {"Data.txt": ("download", "size", "done")}
    assert [p.name for p in folder.iterdir()] == ["data.txt"]
    assert (folder / "data.txt").read_bytes() == b"abc"


def test_where_case_is_ignored_objects_that_differ_only_in_case_are_left_out(folder, storage):
    storage.put("b", "A.txt", b"A")
    storage.put("b", "a.txt", b"a")

    with pytest.warns(SyncWarning, match="differs from another only in case"):
        results = sync_from_bucket("b", folder, storage, ignore_case=True, **NO_WAIT)

    assert statuses(results) == {"A.txt": ("download", "new", "done")}
    assert storage.downloaded == [("b", "A.txt")]


def test_failed_download_leaves_the_old_file_and_holds_back_deletions(folder, storage):
    write(folder, "a.txt", b"old")
    write(folder, "extra.txt", b"e")
    storage.put("b", "a.txt", b"new content")
    storage.put("b", "b.txt", b"b")
    storage.errors["a.txt"] = ConnectionError("connection lost")
    waits = []

    results = sync_from_bucket(
        "b", folder, storage, SyncOptions(delete=True), attempts=3, sleep=waits.append
    )

    assert statuses(results) == {
        "a.txt": ("download", "size", "failed"),
        "b.txt": ("download", "new", "done"),
        "extra.txt": ("delete", "extraneous", "skipped"),
    }
    assert results[0].message == "connection lost"
    assert results[2].message == "Not deleted, since a file failed to download."
    assert waits == [1.0, 2.0]
    assert (folder / "a.txt").read_bytes() == b"old"
    assert (folder / "extra.txt").is_file()
    assert part_files(folder) == []


def test_download_of_another_size_than_listed_leaves_the_old_file(folder):
    class ReplacedObjectStorage(FakeStorage):
        """The object was replaced between the listing and the download."""

        def download(self, bucket, object_name, local_path, *, on_progress=None):
            Path(local_path).write_bytes(b"abc")

    storage = ReplacedObjectStorage()
    write(folder, "a.txt", b"previous")
    storage.put("b", "a.txt", b"0123456789")

    results = sync_from_bucket("b", folder, storage, **NO_WAIT)

    assert statuses(results) == {"a.txt": ("download", "size", "failed")}
    assert "has 3 bytes, where the object has 10" in results[0].message
    assert (folder / "a.txt").read_bytes() == b"previous"
    assert [p.name for p in folder.iterdir()] == ["a.txt"]


def test_refuses_an_object_whose_local_path_is_a_folder(folder, storage):
    # The object "results" is not taken for a folder, since no object below
    # it is listed. Its failure holds back the deletion of keep.txt.
    write(folder, "results/keep.txt", b"k")
    storage.put("b", "results", b"abc")

    results = sync_from_bucket("b", folder, storage, SyncOptions(delete=True), **NO_WAIT)

    assert statuses(results) == {
        "results": ("download", "new", "failed"),
        "results/keep.txt": ("delete", "extraneous", "skipped"),
    }
    assert "is a folder" in results[0].message
    assert storage.downloaded == []
    assert [p.name for p in (folder / "results").iterdir()] == ["keep.txt"]


@needs_symlinks
def test_refuses_to_replace_what_the_listing_did_not_find(folder, tmp_path, storage):
    target = tmp_path / "outside.txt"
    target.write_bytes(b"outside")
    (folder / "a.txt").symlink_to(tmp_path / "missing.txt")  # a broken link is not listed
    storage.put("b", "a.txt", b"abc")

    results = sync_from_bucket("b", folder, storage, **NO_WAIT)

    assert statuses(results) == {"a.txt": ("download", "new", "failed")}
    assert "holds something the listing of the folder did not find" in results[0].message
    assert (folder / "a.txt").is_symlink()
    assert storage.downloaded == []


@needs_symlinks
def test_linked_folder_is_not_downloaded_into_and_holds_back_deletions(folder, tmp_path, storage):
    outside = tmp_path / "outside"
    write(outside, "elsewhere.txt", b"e")
    write(folder, "a.txt", b"a")
    write(folder, "extra.txt", b"x")
    (folder / "link").symlink_to(outside, target_is_directory=True)
    storage.put("b", "a.txt", b"a", last_modified=OLD)
    storage.put("b", "link/elsewhere.txt", b"remote")
    storage.put("b", "link/new.txt", b"n")

    with pytest.warns(SyncWarning, match="are symbolic links"):
        results = sync_from_bucket("b", folder, storage, SyncOptions(delete=True), **NO_WAIT)

    assert statuses(results) == {
        "a.txt": ("none", "unchanged", ""),
        "extra.txt": ("delete", "extraneous", "skipped"),
        "link/elsewhere.txt": ("download", "new", "failed"),
        "link/new.txt": ("download", "new", "failed"),
    }
    assert "is a symbolic link" in results[2].message
    assert results[1].message == "Not deleted, since a local folder is a symbolic link."
    assert sorted(p.name for p in outside.iterdir()) == ["elsewhere.txt"]
    assert (outside / "elsewhere.txt").read_bytes() == b"e"
    assert (folder / "extra.txt").is_file()


@needs_symlinks
def test_excluded_linked_folder_does_not_hold_back_deletions(folder, tmp_path, storage):
    outside = tmp_path / "outside"
    outside.mkdir()
    write(folder, "a.txt", b"a")
    write(folder, "extra.txt", b"x")
    (folder / "link").symlink_to(outside, target_is_directory=True)
    storage.put("b", "a.txt", b"a", last_modified=OLD)

    results = sync_from_bucket(
        "b", folder, storage, SyncOptions(delete=True, exclude=["link"]), **NO_WAIT
    )

    assert statuses(results)["extra.txt"] == ("delete", "extraneous", "done")


def test_metadata_folder_is_never_downloaded_into_or_deleted(folder, storage):
    write(folder, "a.txt", b"a")
    write(folder, ".ebrains/notes.txt", b"n")
    storage.put("b", "results/a.txt", b"a", last_modified=OLD)
    storage.put("b", "results/.ebrains/notes.txt", b"remote")
    storage.put("b", "results/.EBRAINS/bucket.json", b"{}")

    results = sync_from_bucket(
        "b", folder, storage, SyncOptions(prefix="results", delete=True), **NO_WAIT
    )

    assert [r.path for r in results] == ["a.txt"]
    assert storage.downloaded == []
    assert (folder / ".ebrains" / "notes.txt").read_bytes() == b"n"
    assert [p.name for p in (folder / ".ebrains").iterdir()] == ["notes.txt"]


def test_a_file_as_the_local_folder_is_an_error(folder, storage):
    path = write(folder, "a.txt", b"a")

    with pytest.raises(NotADirectoryError):
        sync_from_bucket("b", path, storage)

    assert storage.listings == []


def test_reports_events_in_order(folder, storage):
    write(folder, "extra.txt", b"e")
    storage.put("b", "a.txt", b"a")
    storage.put("b", "b.txt", b"b")
    events: list[SyncEvent] = []

    sync_from_bucket(
        "b",
        folder,
        storage,
        SyncOptions(delete=True, workers=1),
        on_event=events.append,
        **NO_WAIT,
    )

    assert [(e.kind, e.path, e.index, e.total) for e in events] == [
        ("planned", "", 0, 0),
        ("download", "a.txt", 1, 2),
        ("downloaded", "a.txt", 1, 2),
        ("download", "b.txt", 2, 2),
        ("downloaded", "b.txt", 2, 2),
        ("delete", "extra.txt", 1, 1),
        ("deleted", "extra.txt", 1, 1),
        ("finished", "", 0, 0),
    ]
    assert all(r.status == "done" for r in events[-1].results)


def test_downloads_with_several_workers(folder, storage):
    for i in range(20):
        storage.put("b", f"sub/f{i:02d}.txt", bytes([i]))

    results = sync_from_bucket("b", folder, storage, SyncOptions(workers=5), **NO_WAIT)

    assert all(r.status == "done" for r in results)
    assert len(storage.downloaded) == 20
    assert sorted(os.listdir(folder / "sub")) == [f"f{i:02d}.txt" for i in range(20)]


def test_failure_messages_leave_out_url_signatures(folder, storage):
    storage.put("b", "a.txt", b"a")
    storage.errors["a.txt"] = ConnectionError(
        "403 Client Error: Forbidden for url: https://rgw.example/b/a.txt?X-Amz-Signature=abc123"
    )

    results = sync_from_bucket("b", folder, storage, attempts=1, **NO_WAIT)

    assert results[0].message == "403 Client Error: Forbidden for url: https://rgw.example/b/a.txt"
