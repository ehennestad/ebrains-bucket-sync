"""The host of the virtual bucket browser page, against a fake storage and a stub page."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

import pytest

from ebrains_bucket_sync.browser import (
    ENV_PAGE,
    Host,
    VirtualBucket,
    default_page_path,
    format_duration,
    format_local_time,
    open_file_command,
    reveal_action_name,
    reveal_command,
)
from ebrains_bucket_sync.engine import DOWNLOAD_PART_SUFFIX
from ebrains_bucket_sync.model import FileEntry, ListingProgressStorage
from ebrains_bucket_sync.virtual_bucket import (
    BucketInfo,
    VirtualBucketError,
    read_bucket_info,
    read_manifest,
    write_bucket_info,
    write_manifest,
)

from .conftest import FakeStorage, md5_of, utc, write

LISTED_AT = utc(2024, 1, 2, 3, 4, 5)
OBJECT_TIME = "2024-06-01T12:00:00"
OBJECT_TIME_UTC = utc(2024, 6, 1, 12)


class StubPage:
    """Records the events of the host, and answers its confirmations.

    answer is what every confirm event is answered with, at once and on the
    calling thread, or None to leave it pending for the test to answer.
    cancel_after is the number of progress events after which Cancel is
    pressed in the progress dialog, or None.
    """

    def __init__(self) -> None:
        self.host: Host | None = None
        self.events: list[tuple[str, dict]] = []
        self.answer: bool | None = True
        self.cancel_after: int | None = None

    def send(self, name: str, data: dict) -> None:
        self.events.append((name, data))
        if name == "confirm" and self.answer is not None:
            self.reply("confirmed", {"id": data["id"], "confirmed": self.answer})
        if name == "progress" and self.cancel_after is not None:
            if (
                sum(1 for event_name, _ in self.events if event_name == "progress")
                > self.cancel_after
            ):
                self.cancel_after = None
                self.reply("progressCancel", {"id": data["id"]})

    def reply(self, name: str, data: dict) -> None:
        assert self.host is not None
        self.host.request(name, json.dumps(data))

    def named(self, name: str) -> list[dict]:
        return [data for event_name, data in self.events if event_name == name]

    @property
    def state(self) -> dict:
        return self.named("state")[-1]

    @property
    def alerts(self) -> list[tuple[str, str]]:
        return [(data["title"], data["icon"]) for data in self.named("alert")]


class CountingStorage(FakeStorage):
    """A fake storage that counts its objects and reports a listing as it goes."""

    def __init__(self) -> None:
        super().__init__()
        self.progress_step = 1

    def object_count(self, bucket: str) -> int:
        return len(self.buckets[bucket])

    def list_objects(self, bucket, prefix, *, on_progress=None):
        objects = super().list_objects(bucket, prefix)
        if on_progress is not None:
            for n_listed in range(self.progress_step, len(objects) + 1, self.progress_step):
                on_progress(n_listed)
            on_progress(len(objects))
        return objects


def write_downloaded(folder, relative_path, content, modified_time=OBJECT_TIME_UTC):
    """Write a file as a download does: with the time of its object."""
    path = write(folder, relative_path, content)
    timestamp = modified_time.timestamp()
    os.utime(path, (timestamp, timestamp))
    return path


def make_virtual_bucket(folder, bucket="b", prefix="", manifest=None):
    listed_at = None if manifest is None else LISTED_AT
    write_bucket_info(folder, BucketInfo(bucket, prefix, listed_at))
    if manifest is not None:
        write_manifest(folder, manifest)


def make_host(folder, storage, *, run_command=None, update_on_ready=False, **open_args):
    page = StubPage()
    host = Host(
        VirtualBucket.open(folder, **open_args),
        storage,
        page,
        run_command=run_command,
        update_on_ready=update_on_ready,
    )
    page.host = host
    host.request("ready")
    return host, page


def request(host, name, **data):
    host.request(name, json.dumps(data) if data else None)


def local_time(time: datetime) -> str:
    return time.astimezone().strftime("%Y-%m-%d %H:%M")


def modified_text(folder, path: str) -> str:
    """The modified text the page gets for a local file."""
    return local_time(datetime.fromtimestamp((folder / path).stat().st_mtime, tz=timezone.utc))


# ---------------------------------------------------------------- Opening


def test_folder_that_is_not_a_virtual_bucket_needs_a_bucket(folder):
    with pytest.raises(VirtualBucketError, match="not a virtual bucket"):
        VirtualBucket.open(folder)


def test_folder_is_registered_without_a_listing(tmp_path):
    folder = tmp_path / "new"

    virtual_bucket = VirtualBucket.open(folder, "b", "results")

    assert read_bucket_info(folder) == BucketInfo("b", "results/", None)
    assert virtual_bucket.folder == folder.resolve()
    assert not virtual_bucket.is_listed
    assert virtual_bucket.files == []


def test_folder_of_another_bucket_is_refused(folder):
    make_virtual_bucket(folder, "b", "results/")

    with pytest.raises(VirtualBucketError, match="virtual bucket of bucket 'b'"):
        VirtualBucket.open(folder, "other")
    with pytest.raises(VirtualBucketError, match="virtual bucket of bucket 'b'"):
        VirtualBucket.open(folder, "b", "")
    assert VirtualBucket.open(folder, "b").info.prefix == "results/"


# ---------------------------------------------------------------- State


def test_ready_sends_the_state_of_the_files(folder, storage, monkeypatch):
    monkeypatch.setattr("ebrains_bucket_sync.browser.sys.platform", "darwin")
    manifest = [
        FileEntry("differs.txt", 3, OBJECT_TIME_UTC, md5_of(b"abc")),
        FileEntry("online.dat", 5, OBJECT_TIME_UTC),
        FileEntry("placeholder.dat", 4, OBJECT_TIME_UTC),
        FileEntry("same.txt", 2, OBJECT_TIME_UTC),
        FileEntry("sub/deep.txt", 1, OBJECT_TIME_UTC),
    ]
    make_virtual_bucket(folder, "b", "results", manifest)
    write(folder, "differs.txt", b"abcd")
    write(folder, "placeholder.dat", b"")
    write_downloaded(folder, "same.txt", b"ab")
    write_downloaded(folder, "sub/deep.txt", b"d")
    write(folder, "sub/mine.txt", b"m")
    write(folder, ".DS_Store", b"finder")
    write(folder, ".ebrains/notes.txt", b"n")

    host, page = make_host(folder, storage)

    assert page.state == {
        "bucketName": "b",
        "localFolder": str(folder.resolve()),
        "prefix": "results/",
        "isListed": True,
        "listedAt": local_time(LISTED_AT),
        "revealLabel": "Show in Finder",
        "message": "",
        "files": {
            "path": [
                "differs.txt",
                "online.dat",
                "placeholder.dat",
                "same.txt",
                "sub/deep.txt",
                "sub/mine.txt",
            ],
            "state": [
                "differs",
                "online_only",
                "online_only",
                "downloaded",
                "downloaded",
                "local_only",
            ],
            "localBytes": [4, None, 0, 2, 1, 1],
            "remoteBytes": [3, 5, 4, 2, 1, None],
            "modified": [
                modified_text(folder, "differs.txt"),
                "",
                modified_text(folder, "placeholder.dat"),
                modified_text(folder, "same.txt"),
                modified_text(folder, "sub/deep.txt"),
                modified_text(folder, "sub/mine.txt"),
            ],
        },
    }
    assert json.dumps(page.state)  # the page receives it as JSON


def test_never_listed_folder_is_judged_from_the_files_alone(folder, storage):
    make_virtual_bucket(folder, "b")
    write(folder, "empty.dat", b"")
    write(folder, "data.dat", b"xyz")

    host, page = make_host(folder, storage)

    state = page.state
    assert (state["isListed"], state["listedAt"]) == (False, "")
    assert state["files"]["path"] == ["data.dat", "empty.dat"]
    assert state["files"]["state"] == ["downloaded", "online_only"]
    assert state["files"]["remoteBytes"] == [None, None]


def test_refresh_reads_the_folder_again_and_clears_the_message(folder, storage):
    make_virtual_bucket(folder, "b")
    host, page = make_host(folder, storage)
    write(folder, "new.dat", b"n")

    request(host, "refresh")

    assert page.state["files"]["path"] == ["new.dat"]
    assert page.state["message"] == ""


# ---------------------------------------------------------------- Update


def test_update_creates_placeholders_and_writes_the_manifest(folder, storage):
    make_virtual_bucket(folder, "b", "results")
    write_downloaded(folder, "kept.dat", b"kept")
    write(folder, "mine.txt", b"m")
    storage.put("b", "results/kept.dat", b"kept", last_modified=OBJECT_TIME)
    storage.put("b", "results/new/one.dat", b"xyz", last_modified=OBJECT_TIME)
    storage.put("b", "results/new/", b"", content_type="application/directory")
    storage.put("b", "results/.ebrains/manifest.csv", b"m")
    storage.put("b", "elsewhere.dat", b"e")
    host, page = make_host(folder, storage)
    before = datetime.now(timezone.utc).replace(microsecond=0)

    request(host, "update")

    assert (folder / "new" / "one.dat").read_bytes() == b""
    assert (folder / "kept.dat").read_bytes() == b"kept"
    assert read_manifest(folder) == [
        FileEntry("kept.dat", 4, OBJECT_TIME_UTC, md5_of(b"kept")),
        FileEntry("new/one.dat", 3, OBJECT_TIME_UTC, md5_of(b"xyz")),
    ]
    info = read_bucket_info(folder)
    assert (info.bucket, info.prefix) == ("b", "results/")
    assert before <= info.listed_at <= datetime.now(timezone.utc)
    assert page.state["isListed"] is True
    assert page.state["listedAt"] == format_local_time(info.listed_at)
    assert page.state["message"] == "Added placeholders for 1 new object."
    assert page.state["files"]["state"] == ["downloaded", "local_only", "online_only"]
    assert [name for name, _ in page.events] == [
        "state",
        "busy",
        "progress",
        "progress",
        "progress",
        "state",
        "progressClose",
        "busy",
    ]
    assert page.named("progress")[0] == {
        "id": "update",
        "title": "Update from bucket",
        "message": 'Counting the objects of bucket "b"...',
        "value": None,
        "cancelable": True,
    }
    # The fake cannot report pages, so the dialog stays as it was opened: indeterminate
    assert page.named("progress")[1] == {
        "id": "update",
        "message": 'Listing the objects of bucket "b"...',
    }
    assert page.named("busy") == [{"busy": True}, {"busy": False}]


def test_update_shows_the_progress_of_a_storage_that_reports_it(folder, storage):
    make_virtual_bucket(folder, "b")
    storage = CountingStorage()
    for k in range(4):
        storage.put("b", f"f{k}.dat", b"x")
    host, page = make_host(folder, storage)

    request(host, "update")

    assert isinstance(storage, ListingProgressStorage)
    values = [data.get("value") for data in page.named("progress")[1:-1]]
    assert values == [0.0, 0.25, 0.5, 0.75, 1.0, 1.0]
    assert page.named("progress")[1]["message"] == "Listed 0 of about 4 objects."
    assert page.named("progress")[2]["message"].startswith("Listed 1 of about 4 objects. About ")
    assert page.named("progress")[-1]["message"] == "Updating the placeholders..."
    assert len(read_manifest(folder)) == 4


def test_update_deletes_the_placeholders_of_removed_objects(folder, storage):
    manifest = [
        FileEntry("gone.dat", 3, OBJECT_TIME_UTC),
        FileEntry("kept.dat", 4, OBJECT_TIME_UTC, md5_of(b"kept")),
        FileEntry("written.dat", 3, OBJECT_TIME_UTC),
    ]
    make_virtual_bucket(folder, "b", manifest=manifest)
    write(folder, "gone.dat", b"")
    write(folder, "kept.dat", b"")
    write(folder, "written.dat", b"")
    storage.put("b", "kept.dat", b"kept", last_modified=OBJECT_TIME)
    host, page = make_host(folder, storage)
    # Written into after the window opened, so it holds data now
    write(folder, "written.dat", b"new")

    request(host, "update")

    assert not (folder / "gone.dat").exists()
    assert (folder / "kept.dat").read_bytes() == b""
    assert (folder / "written.dat").read_bytes() == b"new"
    assert read_manifest(folder) == [FileEntry("kept.dat", 4, OBJECT_TIME_UTC, md5_of(b"kept"))]
    assert page.state["files"]["path"] == ["kept.dat", "written.dat"]
    assert page.state["files"]["state"] == ["online_only", "local_only"]
    assert page.state["message"] == (
        "Added placeholders for 0 new objects. Deleted the placeholders of 1 removed object."
    )


def test_update_keeps_the_entry_of_a_placeholder_it_cannot_delete(folder, storage, monkeypatch):
    removed = FileEntry("gone.dat", 3, OBJECT_TIME_UTC)
    make_virtual_bucket(folder, "b", manifest=[removed])
    write(folder, "gone.dat", b"")
    host, page = make_host(folder, storage)

    def refuse_unlink(path, *args, **kwargs):
        raise PermissionError(f"Not permitted: {path}")

    monkeypatch.setattr("pathlib.Path.unlink", refuse_unlink)
    request(host, "update")

    assert (folder / "gone.dat").exists()
    assert read_manifest(folder) == [removed]
    assert page.state["files"]["state"] == ["online_only"]
    assert page.state["message"] == "Added placeholders for 0 new objects."


def test_update_of_a_folder_of_the_bucket_shows_the_count_listed(folder):
    make_virtual_bucket(folder, "b", "results")
    storage = CountingStorage()
    storage.put("b", "results/a.dat", b"x")
    storage.put("b", "other.dat", b"x")
    host, page = make_host(folder, storage)

    request(host, "update")

    assert page.named("progress")[2] == {
        "id": "update",
        "message": "Listing the objects of the folder. 1 listed so far. The bucket holds about 2.",
        "value": None,
    }


def test_cancelled_update_changes_nothing(folder):
    make_virtual_bucket(folder, "b")
    storage = CountingStorage()
    storage.put("b", "a.dat", b"x")
    host, page = make_host(folder, storage)
    page.cancel_after = 2

    request(host, "update")

    assert read_manifest(folder) is None
    assert not (folder / "a.dat").exists()
    assert page.alerts == []
    assert page.named("progressClose") == [{"id": "update"}]
    assert page.named("busy") == [{"busy": True}, {"busy": False}]


def test_failed_listing_is_reported(folder, storage):
    make_virtual_bucket(folder, "b")

    def failing_listing(bucket, prefix):
        raise ConnectionError("lost")

    storage.list_objects = failing_listing
    host, page = make_host(folder, storage)

    request(host, "update")

    assert page.alerts == [("Could not list the bucket", "error")]
    assert page.named("alert")[0]["message"] == "lost"
    assert page.named("progressClose") == [{"id": "update"}]
    assert read_manifest(folder) is None


def test_new_folder_is_listed_when_the_page_is_ready(tmp_path, storage):
    storage.put("b", "a.dat", b"xyz")

    host, page = make_host(tmp_path / "new", storage, bucket="b", update_on_ready=True)

    assert (tmp_path / "new" / "a.dat").read_bytes() == b""
    assert page.state["files"]["state"] == ["online_only"]


# ---------------------------------------------------------------- Download


def test_download_fills_a_placeholder_and_gives_it_the_time_of_its_object(folder, storage):
    make_virtual_bucket(folder, "b", "results", [FileEntry("sub/a.dat", 3, OBJECT_TIME_UTC)])
    write(folder, "sub/a.dat", b"")
    storage.put("b", "results/sub/a.dat", b"xyz")
    host, page = make_host(folder, storage)

    request(host, "download", paths=["sub/a.dat"])

    assert (folder / "sub" / "a.dat").read_bytes() == b"xyz"
    assert (folder / "sub" / "a.dat").stat().st_mtime == OBJECT_TIME_UTC.timestamp()
    assert storage.downloaded == [("b", "results/sub/a.dat")]
    assert page.state["files"]["state"] == ["downloaded"]
    assert page.state["message"] == "Downloaded 1 file."
    assert page.named("confirm") == []
    assert [name for name, _ in page.events] == [
        "state",
        "busy",
        "progress",
        "progress",
        "progress",
        "progressClose",
        "state",
        "busy",
    ]
    assert page.named("progress")[1] == {
        "id": "download",
        "message": "Downloading sub/a.dat (1 of 1)...",
        "value": 0.0,
    }
    assert page.named("progress")[2] == {"id": "download", "value": 1.0}


def test_download_of_a_file_of_another_size_keeps_the_placeholder(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.dat", 3, OBJECT_TIME_UTC)])
    write(folder, "a.dat", b"")
    storage.put("b", "a.dat", b"xyzzy")
    host, page = make_host(folder, storage)

    request(host, "download", paths=["a.dat"])

    assert (folder / "a.dat").read_bytes() == b""
    assert [p.name for p in folder.iterdir() if p.name.endswith(DOWNLOAD_PART_SUFFIX)] == []
    assert page.alerts == [("Download stopped", "error")]
    assert page.named("alert")[0]["message"] == (
        '0 of 1 files were downloaded. "a.dat" could not be downloaded:\n\n'
        "The downloaded file 'a.dat' has 5 bytes, where the object has 3."
    )
    assert page.state["message"] == "0 of 1 files were downloaded."
    assert page.state["files"]["state"] == ["online_only"]


def test_download_of_differing_files_asks_first(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.txt", 3, OBJECT_TIME_UTC)])
    write(folder, "a.txt", b"edited")
    storage.put("b", "a.txt", b"abc")
    host, page = make_host(folder, storage)
    page.answer = False

    request(host, "download", paths=["a.txt"])

    assert (folder / "a.txt").read_bytes() == b"edited"
    assert page.named("confirm")[0] == {
        "id": "confirm-1",
        "title": "Replace local copies?",
        "message": "1 of the files differ from the bucket. Downloading them replaces the local "
        "copies, and changes made to those copies are lost.",
        "confirmText": "Download",
    }
    assert page.named("busy") == []

    page.answer = True
    request(host, "download", paths=["a.txt"])

    assert (folder / "a.txt").read_bytes() == b"abc"
    assert page.named("confirm")[1]["id"] == "confirm-2"


def test_confirmation_is_answered_from_another_thread(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.txt", 3, OBJECT_TIME_UTC)])
    write(folder, "a.txt", b"edited")
    storage.put("b", "a.txt", b"abc")
    host, page = make_host(folder, storage)
    page.answer = None  # the page answers later, as a user does

    def answer_when_asked():
        while not page.named("confirm"):
            time.sleep(0.01)
        time.sleep(0.05)
        page.reply("confirmed", {"id": page.named("confirm")[0]["id"], "confirmed": True})

    answerer = threading.Thread(target=answer_when_asked)
    answerer.start()
    request(host, "download", paths=["a.txt"])
    answerer.join()

    assert (folder / "a.txt").read_bytes() == b"abc"
    assert page.state["message"] == "Downloaded 1 file."


def test_closing_the_window_answers_no(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.txt", 3, OBJECT_TIME_UTC)])
    write(folder, "a.txt", b"edited")
    storage.put("b", "a.txt", b"abc")
    host, page = make_host(folder, storage)
    page.answer = None
    closer = threading.Timer(0.05, host.close)
    closer.start()

    request(host, "download", paths=["a.txt"])
    closer.join()

    assert (folder / "a.txt").read_bytes() == b"edited"


def test_download_of_downloaded_files_says_so(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.txt", 3, OBJECT_TIME_UTC)])
    write_downloaded(folder, "a.txt", b"abc")
    host, page = make_host(folder, storage)

    request(host, "download", paths=["a.txt"])

    assert page.alerts == [("Nothing to download", "info")]
    assert storage.downloaded == []


def test_download_stops_at_the_first_failure(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.dat", 1), FileEntry("b.dat", 1)])
    write(folder, "a.dat", b"")
    write(folder, "b.dat", b"")
    storage.put("b", "a.dat", b"x")
    storage.put("b", "b.dat", b"y")
    storage.errors["a.dat"] = ConnectionError("lost")
    host, page = make_host(folder, storage)

    request(host, "download", paths=["a.dat", "b.dat"])

    assert storage.downloaded == []
    assert page.named("alert")[0]["message"].startswith('0 of 2 files were downloaded. "a.dat"')


def test_cancelled_download_keeps_the_files_done_so_far(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.dat", 1), FileEntry("b.dat", 1)])
    write(folder, "a.dat", b"")
    write(folder, "b.dat", b"")
    storage.put("b", "a.dat", b"x")
    storage.put("b", "b.dat", b"y")
    host, page = make_host(folder, storage)
    page.cancel_after = 3  # after the opening, the message and the progress of the first file

    request(host, "download", paths=["a.dat", "b.dat"])

    assert (folder / "a.dat").read_bytes() == b"x"
    assert (folder / "b.dat").read_bytes() == b""
    assert page.state["message"] == "1 of 2 files were downloaded."
    assert page.alerts == []


# ---------------------------------------------------------------- Online only


def test_online_only_empties_downloaded_files_and_keeps_local_only_ones(folder, storage):
    manifest = [FileEntry("a.dat", 4, OBJECT_TIME_UTC), FileEntry("b.dat", 2, OBJECT_TIME_UTC)]
    make_virtual_bucket(folder, "b", manifest=manifest)
    write_downloaded(folder, "a.dat", b"aaaa")
    write_downloaded(folder, "b.dat", b"bb")
    write(folder, "mine.txt", b"mine")
    host, page = make_host(folder, storage)

    request(host, "onlineOnly", paths=["a.dat", "b.dat", "mine.txt"])

    assert (folder / "a.dat").read_bytes() == b""
    assert (folder / "b.dat").read_bytes() == b""
    assert (folder / "mine.txt").read_bytes() == b"mine"
    assert page.named("confirm") == []
    assert page.state["files"]["state"] == ["online_only", "online_only", "local_only"]
    assert page.state["message"] == (
        "Freed 6 B from 2 files. Kept 1 file that the bucket does not have."
    )


def test_online_only_of_local_only_files_alone_keeps_them(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[])
    write(folder, "mine.txt", b"mine")
    host, page = make_host(folder, storage)

    request(host, "onlineOnly", paths=["mine.txt"])

    assert (folder / "mine.txt").read_bytes() == b"mine"
    assert page.alerts == [("Nothing to make online only", "info")]
    assert "the only ones and are kept" in page.named("alert")[0]["message"]


def test_online_only_asks_when_a_file_differs(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.txt", 3, OBJECT_TIME_UTC)])
    write(folder, "a.txt", b"edited")
    host, page = make_host(folder, storage)
    page.answer = False

    request(host, "onlineOnly", paths=["a.txt"])

    assert (folder / "a.txt").read_bytes() == b"edited"
    assert page.named("confirm")[0]["title"] == "Make online only?"
    assert page.named("confirm")[0]["message"].startswith("1 of the 1 files differ")

    page.answer = True
    request(host, "onlineOnly", paths=["a.txt"])

    assert (folder / "a.txt").read_bytes() == b""


def test_online_only_asks_for_a_file_changed_after_its_download(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.txt", 3, OBJECT_TIME_UTC)])
    write_downloaded(folder, "a.txt", b"abc")
    # The same size, but later than its object
    write(folder, "a.txt", b"xyz")
    host, page = make_host(folder, storage)
    page.answer = False

    request(host, "onlineOnly", paths=["a.txt"])

    assert page.state["files"]["state"] == ["differs"]
    assert page.named("confirm")[0]["message"].startswith("1 of the 1 files differ")
    assert (folder / "a.txt").read_bytes() == b"xyz"


def test_online_only_asks_when_the_bucket_was_never_listed(folder, storage):
    make_virtual_bucket(folder, "b")
    write(folder, "a.txt", b"abc")
    host, page = make_host(folder, storage)

    request(host, "onlineOnly", paths=["a.txt"])

    assert page.named("confirm")[0]["message"].startswith(
        "Replace 1 local files (3 B) with empty placeholders? The bucket has not been listed"
    )
    assert (folder / "a.txt").read_bytes() == b""


# ---------------------------------------------------------------- Open and reveal


def recording_runner(returncode=0, stderr=""):
    commands = []

    def run(command):
        commands.append(command)
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    return run, commands


def test_open_of_an_online_only_file_downloads_it_first(folder, storage, monkeypatch):
    monkeypatch.setattr("ebrains_bucket_sync.browser.sys.platform", "darwin")
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.txt", 3, OBJECT_TIME_UTC)])
    write(folder, "a.txt", b"")
    storage.put("b", "a.txt", b"abc")
    run, commands = recording_runner()
    host, page = make_host(folder, storage, run_command=run)

    request(host, "open", paths=["a.txt"])

    assert page.named("confirm")[0]["title"] == "Download file?"
    assert (folder / "a.txt").read_bytes() == b"abc"
    assert commands == [["open", str(folder.resolve() / "a.txt")]]


def test_open_is_not_done_when_the_download_is_declined_or_fails(folder, storage):
    make_virtual_bucket(folder, "b", manifest=[FileEntry("a.txt", 3, OBJECT_TIME_UTC)])
    write(folder, "a.txt", b"")
    storage.put("b", "a.txt", b"abcd")  # another size than the listing says
    run, commands = recording_runner()
    host, page = make_host(folder, storage, run_command=run)

    page.answer = False
    request(host, "open", paths=["a.txt"])
    page.answer = True
    request(host, "open", paths=["a.txt"])

    assert commands == []
    assert page.alerts == [("Download stopped", "error")]


def test_reveal_runs_the_command_of_the_system_and_reports_a_failure(folder, storage, monkeypatch):
    monkeypatch.setattr("ebrains_bucket_sync.browser.sys.platform", "linux")
    make_virtual_bucket(folder, "b")
    write(folder, "sub/a.txt", b"a")
    run, commands = recording_runner(returncode=1, stderr="no file manager")
    host, page = make_host(folder, storage, run_command=run)

    request(host, "reveal", paths=["sub/a.txt"])
    request(host, "reveal", paths=["sub"])

    root = folder.resolve()
    assert commands == [["xdg-open", str(root / "sub")], ["xdg-open", str(root / "sub")]]
    assert page.alerts == [("The command failed", "error")] * 2
    assert page.named("alert")[0]["message"] == "no file manager"


def test_unexpected_error_is_reported(folder, storage):
    make_virtual_bucket(folder, "b")
    host, page = make_host(folder, storage)

    request(host, "reveal", paths=[])

    assert page.alerts == [("Something went wrong", "error")]


# ---------------------------------------------------------------- Text


def test_time_text():
    moment = datetime(2024, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
    assert format_local_time(None) == ""
    assert format_local_time(moment) == moment.astimezone().strftime("%Y-%m-%d %H:%M")
    assert format_duration(3) == "10 seconds"
    assert format_duration(44) == "40 seconds"
    assert format_duration(90) == "2 minutes"
    assert format_duration(61) == "1 minute"


@pytest.mark.parametrize(
    ("platform", "label", "open_command", "reveal_file_command"),
    [
        ("darwin", "Show in Finder", ["open", "{file}"], ["open", "-R", "{file}"]),
        (
            "win32",
            "Show in Explorer",
            ["cmd", "/c", "start", "", "{file}"],
            ["explorer", "/select,{file}"],
        ),
        ("linux", "Show in folder", ["xdg-open", "{file}"], ["xdg-open", "{folder}"]),
    ],
)
def test_system_commands_by_platform(
    monkeypatch, tmp_path, platform, label, open_command, reveal_file_command
):
    monkeypatch.setattr("ebrains_bucket_sync.browser.sys.platform", platform)
    file_path = tmp_path / "it's here" / "a b.txt"
    names = {"file": str(file_path), "folder": str(file_path.parent)}

    assert reveal_action_name() == label
    assert open_file_command(file_path) == [part.format(**names) for part in open_command]
    assert reveal_command(file_path) == [part.format(**names) for part in reveal_file_command]


def test_page_path_comes_from_the_environment_first(monkeypatch, tmp_path):
    page = tmp_path / "page.html"
    monkeypatch.setenv(ENV_PAGE, str(page))

    assert default_page_path() == page
