"""What the live tests share: the bucket, the storage, and a folder of the bucket per test.

The live tests run against the real EBRAINS Data Proxy, in a bucket the
stored login (``ebrains-bucket-sync login``, or EBRAINS_BUCKET_SYNC_TOKEN)
can write to. They are skipped unless EBRAINS_BUCKET_SYNC_TEST_BUCKET names
that bucket::

    EBRAINS_BUCKET_SYNC_TEST_BUCKET=<bucket> uv run pytest tests/live

Each test works below a folder of its own under "live-test/" of the bucket
and deletes the objects it made, so the tests are independent of each
other, leave nothing behind, and can run side by side with the live tests
of the EBRAINS MATLAB toolbox, which use folders of the same form.
"""

from __future__ import annotations

import json
import os
import secrets
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from ebrains_bucket_sync import DeviceFlowAuthenticator, EbrainsDriveStorage
from ebrains_bucket_sync.browser import Host, VirtualBucket
from ebrains_bucket_sync.model import BucketStorage, FileEntry
from ebrains_bucket_sync.remote import list_remote_files

BUCKET = os.environ.get("EBRAINS_BUCKET_SYNC_TEST_BUCKET", "")

LIVE_MARKS = [
    pytest.mark.live,
    pytest.mark.skipif(not BUCKET, reason="EBRAINS_BUCKET_SYNC_TEST_BUCKET is not set"),
]
"""The marks of every live test module, as its pytestmark."""

CLOCK_TOLERANCE = timedelta(minutes=5)
"""Largest accepted difference between a time the listing reports and the
clock of this machine. The two come from different clocks, whereas a time
zone error shifts a time by an hour or more."""

FILE_TIME_TOLERANCE = timedelta(seconds=2)
"""Largest accepted difference between the time of a downloaded file and the
time of its object, the tolerance of the sync contract."""


@pytest.fixture
def storage() -> EbrainsDriveStorage:
    return EbrainsDriveStorage(DeviceFlowAuthenticator())


def make_prefix(stem: str) -> str:
    """A folder of the bucket for one test, such as "live-test/sync-20240102-030405678-1a2b/"."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S%f")[:-3]
    return f"live-test/{stem}-{stamp}-{secrets.token_hex(2)}/"


def delete_objects_below(storage: BucketStorage, prefix: str) -> None:
    """Best-effort cleanup of the objects a test made."""
    try:
        objects = storage.list_objects(BUCKET, prefix)
    except Exception as error:
        warnings.warn(
            f"Could not list the objects below {prefix!r} of bucket {BUCKET!r} for cleanup: "
            f"{error}",
            stacklevel=2,
        )
        return
    for obj in objects:
        try:
            storage.delete_object(BUCKET, obj.name)
        except Exception as error:
            warnings.warn(
                f"Could not delete {obj.name!r} of bucket {BUCKET!r}: {error}", stacklevel=2
            )


def list_files(storage: BucketStorage, prefix: str) -> list[FileEntry]:
    """The files below the prefix of a test, as the sync lists them, sorted by path."""
    return list_remote_files(storage, BUCKET, prefix)


def download_bytes(storage: BucketStorage, object_name: str, folder: Path) -> bytes:
    """The content of an object, fetched into a file of folder."""
    target = folder / ("downloaded-" + object_name.replace("/", "-"))
    storage.download(BUCKET, object_name, target)
    return target.read_bytes()


def file_time(path: Path) -> datetime:
    """Modification time of a file, in UTC."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def set_file_time(path: Path, time: datetime) -> None:
    """Give a file a modification time."""
    seconds = time.timestamp()
    os.utime(path, (seconds, seconds))


class RecordingPage:
    """A page of the browser that records the events of the host.

    A confirm event is answered with no at once, on the calling thread, so
    that a host which asks for a confirmation the test did not expect
    fails its assertion on the recorded events instead of waiting for an
    answer that never comes.
    """

    def __init__(self) -> None:
        self.host: Host | None = None
        self.events: list[tuple[str, dict[str, Any]]] = []

    def send(self, name: str, data: dict[str, Any]) -> None:
        self.events.append((name, data))
        if name == "confirm":
            assert self.host is not None
            self.host.request("confirmed", json.dumps({"id": data["id"], "confirmed": False}))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [data for event_name, data in self.events if event_name == name]

    @property
    def alerts(self) -> list[str]:
        """The alerts the host sent, as "title: message"."""
        return [f"{data['title']}: {data['message']}" for data in self.named("alert")]


def open_host(
    folder: Path,
    prefix: str,
    storage: BucketStorage,
    *,
    update_on_ready: bool = False,
    open_url=None,
) -> tuple[Host, RecordingPage]:
    """Open the browser host on a folder of the test bucket, with its page ready.

    The folder becomes a virtual bucket of the test bucket if it is not
    one yet. With update_on_ready the bucket is listed at once, as the
    browse command does for a new folder.
    """
    page = RecordingPage()
    host = Host(
        VirtualBucket.open(folder, BUCKET, prefix),
        storage,
        page,
        open_url=open_url,
        update_on_ready=update_on_ready,
    )
    page.host = host
    host.request("ready")
    return host, page


def request(host: Host, name: str, **data: Any) -> None:
    """Send a request to the host as its page does."""
    host.request(name, json.dumps(data) if data else None)


def list_into_virtual_bucket(folder: Path, prefix: str, storage: BucketStorage) -> None:
    """Make folder a virtual bucket of prefix and fill it from a listing of the bucket.

    This is what ``browse`` does for a new folder, and the counterpart of
    createVirtualBucket of the MATLAB toolbox: it writes bucket.json and
    the manifest, creates a placeholder for each object, and deletes the
    placeholders of objects removed since the last listing. The listing
    must succeed; an alert of the host fails the test.
    """
    host, page = open_host(folder, prefix, storage)
    request(host, "update")
    assert page.alerts == [], "The listing of the bucket failed."
