"""Host of the virtual bucket browser page.

The page, virtualBucketBrowser.html of the EBRAINS MATLAB toolbox, is the
view alone: it sends the host requests such as "download" and receives
events such as "state", both with JSON data, as its header comment
describes. This module is the Python host, as
ebrains.bucket.app.VirtualBucketBrowser is the MATLAB one: VirtualBucket
holds the folder and the state of its files, Host carries out the
requests, and show_browser puts the page in a pywebview window.

Threads: pywebview calls the API object on a new thread for each request
of the page, so one request can run while another waits. Host.request
relies on that. An action that needs the user's consent sends the confirm
event and blocks until the page's "confirmed" request, which arrives on
another thread, answers it; "progressCancel" sets a flag the same way.
A host that calls request from a single thread must run each action on a
thread of its own, or the answer never arrives. The actions themselves
run one at a time.
"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from .engine import LocalFileChanged, download_object, never_synced
from .local import compute_md5, list_local_files, local_file_entry
from .model import BucketStorage, FileEntry, ListingProgressStorage
from .remote import remote_files_from_objects
from .text import count_text, format_size
from .virtual_bucket import (
    BucketInfo,
    VirtualBucketError,
    file_states,
    placeholders_of_removed_objects,
    read_bucket_info,
    read_manifest,
    virtual_bucket_refusal,
    write_bucket_info,
    write_manifest,
)

TIME_FORMAT = "%Y-%m-%d %H:%M"
"""How the page shows a time: local time, to the minute."""

FINDER_FILE = ".DS_Store"
"""Finder writes a file of this name into every folder it shows. It belongs
to the file system, not to the bucket, so the browser leaves it out."""

PROGRESS_INTERVAL = 0.25
"""Seconds between two progress events of a download. Each event is a
round trip to the page, so one per chunk would slow a download down."""

COLLABORATORY_BUCKET_URL = "https://wiki.ebrains.eu/bin/view/Collabs/{bucket}/Bucket"
"""The page of a bucket in the Collaboratory. A dataset bucket, named
d-<uuid>, has one under that name as well as a collab bucket does."""

ENV_PAGE = "EBRAINS_BUCKET_SYNC_BROWSER_PAGE"
"""Path of the page to show, in place of the copy in this package."""

PAGE_FILE = Path(__file__).parent / "resources" / "virtualBucketBrowser.html"
"""The page of the browser: a copy of the one of the EBRAINS MATLAB toolbox,
taken at the commit that resources/browser-page-source.json records (see
tools/update_browser_page.py)."""


class BrowserCancelled(Exception):
    """The user pressed Cancel in a progress dialog."""


@dataclass(frozen=True)
class FileRow:
    """One file of a virtual bucket, as the page shows it.

    Attributes:
        path: Path relative to the folder of the virtual bucket.
        state: One of virtual_bucket.STATES.
        local_bytes: Size of the local file, None if there is none.
        remote_bytes: Size of the object, None if it is not known.
        modified: Time of the last change of the local file, None if there is none.
    """

    path: str
    state: str
    local_bytes: int | None
    remote_bytes: int | None
    modified: datetime | None


class VirtualBucket:
    """The folder of a virtual bucket and the state of its files.

    The files are judged against the manifest, the objects of the last
    listing of the bucket, as spec/README.md#file-states says. Listing a
    bucket of many objects takes a while, so the manifest is only renewed
    by Host when asked to update from the bucket.

    The browser shows the files a sync may touch: a path that no sync
    touches (engine.never_synced), such as the file of an interrupted
    transfer, is left out of the scan of the folder, of a listing of the
    bucket, and of a manifest that another tool wrote with such an entry.

    Attributes:
        folder: Absolute path of the folder.
        info: The bucket the folder stands for and when it was listed.
        manifest: The objects of the last listing, or None if never listed.
        local_files: The files of the folder at the last scan, those a sync
            may touch and not Finder's.
        files: One FileRow per path of the folder or the manifest, sorted by path.
    """

    def __init__(self, folder: Path, info: BucketInfo, manifest: list[FileEntry] | None) -> None:
        self.folder = Path(folder).resolve()
        self.info = info
        self.manifest = None if manifest is None else _synced_entries(manifest)
        self.local_files: list[FileEntry] = []
        self.files: list[FileRow] = []
        # Checksums of local files by path, size and time, so that a scan
        # reads a file again only once it changed
        self._checksums: dict[tuple[str, int, datetime | None], str] = {}

    @classmethod
    def open(
        cls, folder: str | os.PathLike[str], bucket: str | None = None, prefix: str | None = None
    ) -> VirtualBucket:
        """The virtual bucket in folder, made one of bucket if it is not one yet.

        A folder that is a virtual bucket already is only read, so that it
        can be on a read-only location; bucket and prefix, if given, must
        be those it stands for. A folder that is not becomes one of bucket,
        which is then required, and is created if it does not exist. The
        files are scanned once. Raises VirtualBucketError when the folder
        stands for another bucket, when it is not a virtual bucket and no
        bucket is given, or when its .ebrains folder is of a format this
        version does not know.
        """
        folder = Path(folder)
        info = read_bucket_info(folder)
        if info is None:
            if not bucket:
                raise VirtualBucketError(
                    f"{str(folder)!r} is not a virtual bucket. Give the bucket to make it one."
                )
            info = BucketInfo(bucket, prefix or "")
            folder.mkdir(parents=True, exist_ok=True)
            write_bucket_info(folder, info)
        elif bucket:
            wanted_prefix = info.prefix if prefix is None else prefix
            refusal = virtual_bucket_refusal(info, bucket, wanted_prefix)
            if refusal is not None:
                raise VirtualBucketError(refusal.reason)

        virtual_bucket = cls(folder, info, read_manifest(folder))
        virtual_bucket.scan()
        return virtual_bucket

    @property
    def is_listed(self) -> bool:
        """Whether the states of the files come from a listing of the bucket."""
        return self.manifest is not None

    def scan(self) -> None:
        """Read the files of the folder again and give each a state."""
        self.local_files = [
            entry
            for entry in _synced_entries(list_local_files(self.folder))
            if entry.path.rsplit("/", 1)[-1] != FINDER_FILE
        ]
        self._assign_states()

    def rescan(self, paths: Iterable[str]) -> None:
        """Read the files of the given paths again and give each its state.

        An action that discards local data must judge a file as it is
        now, not as it was at the last scan: the file may have been edited
        in another application since. Only the given files are read, since
        the state of a path rests on its own file and manifest entry alone,
        and a folder of many files would take long to scan.
        """
        local_by_path = {entry.path: entry for entry in self.local_files}
        for path in set(paths):
            entry = None if never_synced(path) else local_file_entry(self.folder, path)
            if entry is None:
                local_by_path.pop(path, None)
            else:
                local_by_path[path] = entry
        self.local_files = sorted(local_by_path.values(), key=lambda entry: entry.path)
        self._assign_states()

    def _assign_states(self) -> None:
        """Give each path of the folder or the manifest its row, from local_files."""
        local_by_path = {entry.path: entry for entry in self.local_files}
        remote_by_path = {entry.path: entry for entry in self.manifest or ()}
        self.files = []
        for path, state in file_states(
            self.local_files, self.manifest, local_hash=self._local_checksum
        ):
            local_entry = local_by_path.get(path)
            remote_entry = remote_by_path.get(path)
            self.files.append(
                FileRow(
                    path,
                    state,
                    None if local_entry is None else local_entry.bytes,
                    None if remote_entry is None else remote_entry.bytes,
                    None if local_entry is None else local_entry.modified_time,
                )
            )

    def _local_checksum(self, entry: FileEntry) -> str:
        """The MD5 checksum of a local file, or "" if it cannot be read."""
        key = (entry.path, entry.bytes, entry.modified_time)
        if key not in self._checksums:
            try:
                self._checksums[key] = compute_md5(self.folder / entry.path)
            except OSError:
                return ""
        return self._checksums[key]

    def row(self, path: str) -> FileRow | None:
        """The row of a path, or None if the folder and the manifest have no such file."""
        for row in self.files:
            if row.path == path:
                return row
        return None

    def rows(self, paths: Iterable[str]) -> list[FileRow]:
        """The rows of the given paths, in the order of the files."""
        wanted = set(paths)
        return [row for row in self.files if row.path in wanted]

    def local_entries(self, paths: Iterable[str]) -> list[FileEntry]:
        """The files of the given paths as they were last read, in the order of the files."""
        wanted = set(paths)
        return [entry for entry in self.local_files if entry.path in wanted]

    def manifest_entry(self, path: str) -> FileEntry | None:
        """What the last listing said of the object of a path, or None."""
        for entry in self.manifest or ():
            if entry.path == path:
                return entry
        return None

    def record_listing(self, entries: list[FileEntry], listed_at: datetime) -> None:
        """Write a listing of the bucket as the manifest, and when it was made."""
        write_manifest(self.folder, entries)
        self.info = replace(self.info, listed_at=listed_at)
        write_bucket_info(self.folder, self.info)
        self.manifest = entries

    def state_data(self, *, message: str = "", reveal_label: str = "") -> dict[str, Any]:
        """The data of the state event, as the page's header describes it."""
        return {
            "bucketName": self.info.bucket,
            "localFolder": str(self.folder),
            "prefix": self.info.prefix,
            "isListed": self.is_listed,
            "listedAt": format_local_time(self.info.listed_at),
            "revealLabel": reveal_label,
            "message": message,
            "files": {
                "path": [row.path for row in self.files],
                "state": [row.state for row in self.files],
                "localBytes": [row.local_bytes for row in self.files],
                "remoteBytes": [row.remote_bytes for row in self.files],
                "modified": [format_local_time(row.modified) for row in self.files],
            },
        }


class Page(Protocol):
    """Where the host sends its events: the page in a window, or a test double."""

    def send(self, name: str, data: dict[str, Any]) -> None: ...


class _Confirmation:
    """A confirm event waiting for its answer."""

    def __init__(self) -> None:
        self.answered = threading.Event()
        self.confirmed = False


class Host:
    """Carries out the requests of the page on a virtual bucket.

    Args:
        virtual_bucket: The folder shown.
        storage: Where its bucket is listed and the objects are fetched from.
        page: Where the events go.
        run_command: Runs a command of the operating system, as
            subprocess.run with a list does. For tests.
        open_url: Opens a web page, as webbrowser.open does. For tests.
        update_on_ready: List the bucket as soon as the page is ready, for
            a folder that was just created and has no files yet.
    """

    def __init__(
        self,
        virtual_bucket: VirtualBucket,
        storage: BucketStorage,
        page: Page,
        *,
        run_command: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
        open_url: Callable[[str], object] | None = None,
        update_on_ready: bool = False,
    ) -> None:
        self.bucket = virtual_bucket
        self._storage = storage
        self._page = page
        self._run_command = run_command or _run_command
        self._open_url = open_url or webbrowser.open
        self._update_on_ready = update_on_ready
        self._message = ""
        self._page_ready = False
        self._closed = False
        self._action_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._confirmations: dict[str, _Confirmation] = {}
        self._cancelled: set[str] = set()
        """Ids of the progress dialogs whose Cancel was pressed."""
        self._ids = itertools.count(1)

    # ---------------------------------------------------------------- Requests

    def request(self, name: str, data: str | None = None) -> None:
        """Carry out a request of the page. data is the JSON text the page sends.

        The replies "confirmed" and "progressCancel" return at once. Every
        other request is an action, and actions run one at a time.
        """
        if self._closed:
            return
        payload = json.loads(data) if data else {}
        if name == "confirmed":
            self._answer(str(payload["id"]), bool(payload["confirmed"]))
            return
        if name == "progressCancel":
            with self._state_lock:
                self._cancelled.add(str(payload["id"]))
            return

        paths = [str(path) for path in payload.get("paths", [])]
        with self._action_lock:
            try:
                self._act(name, paths)
            except Exception as error:
                self._alert("Something went wrong", str(error) or type(error).__name__)

    def _act(self, name: str, paths: list[str]) -> None:
        if name == "ready":
            self._page_ready = True
            self._send_state()
            if self._update_on_ready:
                self._update_on_ready = False
                self._update()
        elif name == "refresh":
            self._message = ""
            self._refresh()
        elif name == "update":
            self._update()
        elif name == "download":
            self._download(paths)
        elif name == "onlineOnly":
            self._make_online_only(paths)
        elif name == "open":
            self._open(paths[0])
        elif name == "reveal":
            self._reveal(paths[0])
        elif name == "openOnline":
            self._open_url(bucket_web_url(self.bucket.info.bucket))
        # The page sends no other requests

    def close(self) -> None:
        """Stop taking requests, and answer the pending confirmations with no."""
        self._closed = True
        with self._state_lock:
            pending = list(self._confirmations.values())
        for confirmation in pending:
            confirmation.answered.set()

    # ---------------------------------------------------------------- Actions

    def _refresh(self) -> None:
        self.bucket.scan()
        self._send_state()

    def _update(self) -> None:
        """List the bucket, renew the placeholders and compare the files.

        Objects that the folder has no file for get an empty placeholder,
        and the placeholders of objects that are gone are deleted. Other
        local files that the bucket does not have are kept.
        """
        bucket = self.bucket.info.bucket
        self._send("busy", {"busy": True})
        self._open_progress(
            "update",
            title="Update from bucket",
            message=f'Counting the objects of bucket "{bucket}"...',
            cancelable=True,
        )
        try:
            listed_at = datetime.now(timezone.utc)
            try:
                entries = self._list_bucket()
            except BrowserCancelled:
                return
            except Exception as error:
                self._alert("Could not list the bucket", str(error) or type(error).__name__)
                return

            self._progress("update", message="Updating the placeholders...")
            # The files may have changed while the bucket was listed
            self.bucket.scan()
            local_paths = {entry.path for entry in self.bucket.local_files}
            new_paths = [entry.path for entry in entries if entry.path not in local_paths]
            # A path that something else is in the way of gets no
            # placeholder; the listing is recorded all the same, so the
            # manifest knows the object and the next update tries again
            without_placeholder = create_placeholders(self.bucket.folder, new_paths)
            removed = placeholders_of_removed_objects(
                self.bucket.local_files, entries, self.bucket.manifest
            )
            kept = delete_placeholders(self.bucket.folder, [entry.path for entry in removed])
            # A placeholder that could not be deleted keeps its manifest
            # entry, so that a push still knows it for a placeholder
            kept_entries = [entry for entry in removed if entry.path in kept]
            self.bucket.record_listing(
                sorted(entries + kept_entries, key=lambda entry: entry.path), listed_at
            )
            n_added = len(new_paths) - len(without_placeholder)
            message = f"Added placeholders for {count_text(n_added, 'new object')}."
            if without_placeholder:
                message += (
                    f" {count_text(len(without_placeholder), 'object')} got no placeholder, "
                    "since a file or folder of the folder is in the way, for example "
                    f'"{without_placeholder[0]}".'
                )
            n_deleted = len(removed) - len(kept_entries)
            if n_deleted > 0:
                message += (
                    f" Deleted the placeholders of {count_text(n_deleted, 'removed object')}."
                )
            self._message = message
            self._refresh()
        finally:
            self._close_progress("update")
            self._send("busy", {"busy": False})

    def _list_bucket(self) -> list[FileEntry]:
        """The files of the bucket below the prefix, with the progress shown.

        A storage that can count the objects and report the listing as it
        goes gets a progress bar, with the time left estimated from the
        rate so far; any other storage gets an indeterminate dialog. The
        count is of the whole bucket, so for a folder of it only the
        number listed is shown.
        """
        bucket = self.bucket.info.bucket
        prefix = self.bucket.info.prefix
        storage = self._storage

        if isinstance(storage, ListingProgressStorage):
            count = storage.object_count(bucket)
            started = time.monotonic()

            def report(n_listed: int) -> None:
                self._check_cancelled("update")
                if prefix:
                    message = (
                        f"Listing the objects of the folder. {n_listed:,} listed so far. "
                        f"The bucket holds about {count:,}."
                    )
                    self._progress("update", message=message, value=None)
                    return
                message = f"Listed {n_listed:,} of about {count:,} objects."
                if 0 < n_listed < count:
                    seconds_left = (time.monotonic() - started) / n_listed * (count - n_listed)
                    message += f" About {format_duration(seconds_left)} left."
                self._progress("update", message=message, value=min(n_listed / max(count, 1), 1))

            report(0)
            objects = storage.list_objects(bucket, prefix, on_progress=report)
        else:
            self._progress("update", message=f'Listing the objects of bucket "{bucket}"...')
            objects = storage.list_objects(bucket, prefix)
            # A listing that reports no progress cannot stop early, so a
            # Cancel pressed during it takes effect once it is done
            self._check_cancelled("update")

        return _synced_entries(remote_files_from_objects(objects, bucket, prefix))

    def _download(self, paths: list[str]) -> None:
        """Download the files of the given paths that are online only or differ."""
        # The files may have been edited since the last scan
        self.bucket.rescan(paths)
        rows = [row for row in self.bucket.rows(paths) if row.state in ("online_only", "differs")]
        if not rows:
            self._alert(
                "Nothing to download", "The selected files are downloaded already.", icon="info"
            )
            return

        n_differing = sum(1 for row in rows if row.state == "differs")
        if n_differing > 0:
            message = (
                f"{n_differing} of the files differ from the bucket. Downloading them replaces "
                "the local copies, and changes made to those copies are lost."
            )
            if not self._confirm(message, "Replace local copies?", "Download"):
                return

        self._send("busy", {"busy": True})
        try:
            self._download_rows(rows)
        finally:
            self._send("busy", {"busy": False})

    def _download_rows(self, rows: list[FileRow]) -> bool:
        """Download the files one by one, and say how far it got. True if all were."""
        n_files = len(rows)
        completed = False
        failure = ""
        self._open_progress("download", title="Download", message="", cancelable=True)
        try:
            for index, row in enumerate(rows, 1):
                self._progress(
                    "download",
                    message=f"Downloading {row.path} ({index} of {n_files})...",
                    value=(index - 1) / n_files,
                )
                try:
                    self._download_one(row.path, index, n_files)
                except BrowserCancelled:
                    self._message = f"{index - 1} of {n_files} files were downloaded."
                    break
                except Exception as error:
                    self._message = f"{index - 1} of {n_files} files were downloaded."
                    failure = (
                        f'{index - 1} of {n_files} files were downloaded. "{row.path}" could not '
                        f"be downloaded:\n\n{str(error) or type(error).__name__}"
                    )
                    break
            else:
                completed = True
                self._message = f"Downloaded {count_text(n_files, 'file')}."
        finally:
            self._close_progress("download")
        self._refresh()
        if failure:
            self._alert("Download stopped", failure)
        return completed

    def _download_one(self, path: str, index: int, n_files: int) -> None:
        """Download an object into its file, as a sync from the bucket does.

        The file gets the size check and the time of its object from the
        last listing; without one the download is only put in place. The
        download replaces the file only while it still is as it was last
        read, which the decision to replace it rested on: a file edited
        meanwhile holds data that was not judged, so the download is
        discarded with an error.
        """
        entry = self.bucket.manifest_entry(path)
        expected_bytes = None if entry is None else entry.bytes
        modified_time = None if entry is None else entry.modified_time
        judged = self.bucket.local_entries([path])
        last_report = 0.0

        def on_progress(received: int, total: int | None) -> None:
            nonlocal last_report
            self._check_cancelled("download")
            size = total or expected_bytes
            now = time.monotonic()
            if now - last_report < PROGRESS_INTERVAL and received != size:
                return
            last_report = now
            fraction = min(received / size, 1.0) if size else 0.0
            self._progress("download", value=(index - 1 + fraction) / n_files)

        download_object(
            self._storage,
            self.bucket.info.bucket,
            self.bucket.info.prefix + path,
            self.bucket.folder / path,
            expected_bytes=expected_bytes,
            modified_time=modified_time,
            on_progress=on_progress,
            judged_file=judged[0] if judged else None,
        )

    def _make_online_only(self, paths: list[str]) -> None:
        """Replace the downloaded files of the given paths with placeholders."""
        # The files may have been edited since the last scan
        self.bucket.rescan(paths)
        selected = self.bucket.rows(paths)
        n_local_only = sum(1 for row in selected if row.state == "local_only")
        rows = [row for row in selected if row.state in ("downloaded", "differs")]
        if not rows:
            if n_local_only > 0:
                message = (
                    "The bucket does not have the selected files, so their local copies are "
                    "the only ones and are kept."
                )
            else:
                message = "The selected files are online only already."
            self._alert("Nothing to make online only", message, icon="info")
            return

        # A file that matches its object can be downloaded again, so it is
        # replaced without asking. Any other file may hold changes that
        # exist nowhere else.
        n_differing = sum(1 for row in rows if row.state == "differs")
        freed = format_size(sum(row.local_bytes or 0 for row in rows))
        if not self.bucket.is_listed:
            message = (
                f"Replace {len(rows)} local files ({freed}) with empty placeholders? The bucket "
                "has not been listed, so the files cannot be checked against their objects. "
                "Changes made to the local copies are lost."
            )
        elif n_differing > 0:
            message = (
                f"{n_differing} of the {len(rows)} files differ from the bucket. Replacing them "
                "with empty placeholders discards the local copies, and changes made to them "
                "are lost."
            )
        else:
            message = ""
        if message and not self._confirm(message, "Make online only?", "Make online only"):
            return

        try:
            replace_with_placeholders(
                self.bucket.folder, self.bucket.local_entries(row.path for row in rows)
            )
        except OSError as error:
            self._refresh()
            self._alert("Could not make the files online only", str(error))
            return

        self._message = f"Freed {freed} from {count_text(len(rows), 'file')}."
        if n_local_only > 0:
            self._message += (
                f" Kept {count_text(n_local_only, 'file')} that the bucket does not have."
            )
        self._refresh()

    def _open(self, path: str) -> None:
        """Open a file with the default application of the system, downloading it first."""
        row = self.bucket.row(path)
        if row is not None and row.state == "online_only":
            message = f'"{path}" is online only. Download it and open it?'
            if not self._confirm(message, "Download file?", "Download and open"):
                return
            self._send("busy", {"busy": True})
            try:
                downloaded = self._download_rows([row])
            finally:
                self._send("busy", {"busy": False})
            if not downloaded:
                return
        self._run_system_command(open_file_command(self.bucket.folder / path))

    def _reveal(self, path: str) -> None:
        """Show a file or folder in the file manager of the system."""
        self._run_system_command(reveal_command(self.bucket.folder / path))

    def _run_system_command(self, command: list[str]) -> None:
        """Run a command of the operating system, and report a failure.

        Explorer exits with status 1 when it succeeds, so the status of a
        command on Windows says nothing.
        """
        completed = self._run_command(command)
        if completed.returncode != 0 and sys.platform != "win32":
            output = (completed.stderr or completed.stdout or "").strip()
            self._alert("The command failed", output or " ".join(command))

    # ---------------------------------------------------------------- Page

    def _send(self, name: str, data: dict[str, Any]) -> None:
        if self._page_ready and not self._closed:
            self._page.send(name, data)

    def _send_state(self) -> None:
        self._send(
            "state",
            self.bucket.state_data(message=self._message, reveal_label=reveal_action_name()),
        )

    def _alert(self, title: str, message: str, *, icon: str = "error") -> None:
        self._send("alert", {"title": title, "message": message, "icon": icon})

    def _confirm(self, message: str, title: str, confirm_text: str) -> bool:
        """Ask the user to confirm an action, and wait for the answer.

        The answer arrives as the "confirmed" request, on another thread.
        A page that is not ready cannot answer, and closing the window
        answers no.
        """
        if not self._page_ready or self._closed:
            return False
        confirmation = _Confirmation()
        confirmation_id = f"confirm-{next(self._ids)}"
        with self._state_lock:
            self._confirmations[confirmation_id] = confirmation
        self._send(
            "confirm",
            {
                "id": confirmation_id,
                "title": title,
                "message": message,
                "confirmText": confirm_text,
            },
        )
        confirmation.answered.wait()
        return confirmation.confirmed

    def _answer(self, confirmation_id: str, confirmed: bool) -> None:
        with self._state_lock:
            confirmation = self._confirmations.pop(confirmation_id, None)
        if confirmation is not None:
            confirmation.confirmed = confirmed
            confirmation.answered.set()

    def _open_progress(
        self, progress_id: str, *, title: str, message: str, cancelable: bool
    ) -> None:
        with self._state_lock:
            self._cancelled.discard(progress_id)
        self._send(
            "progress",
            {
                "id": progress_id,
                "title": title,
                "message": message,
                "value": None,
                "cancelable": cancelable,
            },
        )

    def _progress(self, progress_id: str, **fields: Any) -> None:
        """Update a progress dialog. Fields left out keep their value."""
        self._send("progress", {"id": progress_id, **fields})

    def _close_progress(self, progress_id: str) -> None:
        self._send("progressClose", {"id": progress_id})

    def _check_cancelled(self, progress_id: str) -> None:
        with self._state_lock:
            cancelled = progress_id in self._cancelled
        if cancelled:
            raise BrowserCancelled(f"The {progress_id} was cancelled.")


# ---------------------------------------------------------------- Files


def _synced_entries(entries: Iterable[FileEntry]) -> list[FileEntry]:
    """The entries whose paths a sync may touch (see engine.never_synced), in the same order."""
    return [entry for entry in entries if not never_synced(entry.path)]


def create_placeholders(root: Path, paths: Iterable[str]) -> list[str]:
    """Create an empty file for each path below root, and give the paths that got none.

    A file that exists is kept: it may hold downloaded data. A path gets
    no placeholder when something else is in its way: a folder or a
    symbolic link at the path, which the empty file must not be created
    through, or a file where a folder of the path must go. Such a path is
    returned, and the other paths get their placeholders.
    """
    without_placeholder = []
    for path in paths:
        file_path = Path(root) / path
        if file_path.is_file():
            continue
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # Creating the file anew, rather than touching whatever is at
            # the path, fails on a dangling link instead of following it
            file_path.touch(exist_ok=False)
        except OSError:
            without_placeholder.append(path)
    return without_placeholder


def delete_placeholders(root: Path, paths: Iterable[str]) -> set[str]:
    """Delete the empty files of the paths below root, and give the paths of those kept.

    A file that is no longer empty holds data and is left. A placeholder
    that cannot be deleted is kept and its path given. A path that is
    no file is skipped.
    """
    kept = set()
    for path in paths:
        file_path = Path(root) / path
        try:
            if file_path.stat().st_size > 0:
                continue
            file_path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            kept.add(path)
    return kept


def replace_with_placeholders(root: Path, files: Iterable[FileEntry]) -> None:
    """Empty the files below root, so that they are online only.

    files are the entries of the files as they were last read, on which
    the decision to discard their data rests. The data is discarded only
    while each file still has the size and the time of its entry: a file
    that is missing or changed since holds something that was not judged,
    which is an error, raised before any file is changed.
    """
    files = list(files)
    for entry in files:
        file_path = Path(root) / entry.path
        if file_path.is_symlink():
            # Opening the link for writing would empty the file it points
            # to, which is not the virtual bucket's to discard
            raise OSError(
                f'"{file_path}" is a symbolic link to a file elsewhere, which cannot be made '
                "online only, so no file was. Remove the link to make the object online only."
            )
        current = local_file_entry(root, entry.path)
        if current is None:
            raise FileNotFoundError(
                f'The file "{file_path}" does not exist, so no file was made online only.'
            )
        if (current.bytes, current.modified_time) != (entry.bytes, entry.modified_time):
            raise LocalFileChanged(
                f'The file "{file_path}" changed since it was read, so no file was made online '
                "only. Refresh and try again."
            )
    for entry in files:
        # Opening a file for writing empties it and keeps its name, which
        # is all a placeholder of a virtual bucket is
        with open(Path(root) / entry.path, "wb"):
            pass


# ---------------------------------------------------------------- Text


def format_local_time(time: datetime | None) -> str:
    """A time in the local time zone, as the page shows it, or "" for None."""
    if time is None:
        return ""
    return time.astimezone().strftime(TIME_FORMAT)


def format_duration(seconds: float) -> str:
    """A rounded duration, such as "40 seconds" or "6 minutes"."""
    if seconds < 60:
        return f"{max(round(seconds / 10) * 10, 10)} seconds"
    return count_text(round(seconds / 60), "minute")


# ---------------------------------------------------------------- System


def bucket_web_url(bucket: str) -> str:
    """The address of the page of the bucket in the Collaboratory."""
    return COLLABORATORY_BUCKET_URL.format(bucket=quote(bucket, safe=""))


def reveal_action_name() -> str:
    """Label of the action that shows a file in the file manager of the system."""
    if sys.platform == "darwin":
        return "Show in Finder"
    if sys.platform == "win32":
        return "Show in Explorer"
    return "Show in folder"


def open_file_command(file_path: Path) -> list[str]:
    """Command that opens a file with the default application of the system."""
    if sys.platform == "darwin":
        return ["open", str(file_path)]
    if sys.platform == "win32":
        return ["cmd", "/c", "start", "", str(file_path)]
    return ["xdg-open", str(file_path)]


def reveal_command(path: Path) -> list[str]:
    """Command that shows a file or folder in the file manager of the system."""
    if sys.platform == "darwin":
        return ["open", "-R", str(path)]
    if sys.platform == "win32":
        return ["explorer", f"/select,{path}"]
    return ["xdg-open", str(path if path.is_dir() else path.parent)]


def _run_command(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, check=False)


# ---------------------------------------------------------------- Window


class PywebviewPage:
    """Sends the events to the page in a pywebview window.

    The page receives an event through window.virtualBucketBrowser.receive,
    which takes the data as JSON text, so the text is put into the script
    as a JavaScript string literal: JSON twice.
    """

    def __init__(self) -> None:
        self.window: Any = None

    def send(self, name: str, data: dict[str, Any]) -> None:
        text = json.dumps(data)
        self.window.evaluate_js(
            f"window.virtualBucketBrowser.receive({json.dumps(name)}, {json.dumps(text)})"
        )


class _PageApi:
    """What the page reaches as window.pywebview.api."""

    def __init__(self, host: Host) -> None:
        self._host = host

    def request(self, name: str, data: str | None = None) -> None:
        self._host.request(name, data)


def show_browser(
    virtual_bucket: VirtualBucket,
    storage: BucketStorage,
    page_path: Path,
    *,
    update_on_ready: bool = False,
) -> None:
    """Show the page in a window, and return when the window is closed."""
    import webview  # loads the GUI toolkit, which only the browser needs

    page = PywebviewPage()
    host = Host(virtual_bucket, storage, page, update_on_ready=update_on_ready)
    window = webview.create_window(
        f"Virtual bucket - {virtual_bucket.info.bucket}",
        url=str(page_path),
        js_api=_PageApi(host),
        width=1100,
        height=680,
    )
    page.window = window
    window.events.closed += host.close
    webview.start()


def default_page_path() -> Path:
    """The page of the browser: the path in ENV_PAGE if it is set, else PAGE_FILE.

    Raises FileNotFoundError when PAGE_FILE is missing, as it is from an
    installation that left out the data files of the package.
    """
    from_environment = os.environ.get(ENV_PAGE)
    if from_environment:
        return Path(from_environment)
    if not PAGE_FILE.is_file():
        raise FileNotFoundError(
            f"The browser page is missing from the package at {PAGE_FILE}. "
            f"Reinstall ebrains-bucket-sync, or give --page."
        )
    return PAGE_FILE
