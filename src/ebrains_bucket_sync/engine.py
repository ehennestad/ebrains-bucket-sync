"""Make a bucket match a local folder, or a local folder match a bucket.

sync_to_bucket and sync_from_bucket work in four steps, as
ebrains.bucket.sync.toBucket and fromBucket of the MATLAB toolbox do:
    1. List the files on both sides, without the excluded ones.
    2. Plan what to copy and delete (plan.plan_sync).
    3. Copy the new and changed files.
    4. Delete the extraneous files, if asked to and no copy failed.
A copy that fails does not stop the sync: the other files are copied,
the failure is recorded in the result, and nothing is deleted.

download_object fetches one object into a local file the way a sync from
the bucket does, which the virtual bucket browser uses to fill a
placeholder.

When the local folder is a virtual bucket (see virtual_bucket.read_bucket_info),
the sync must be of its bucket and prefix, a sync to the bucket leaves its
placeholders alone, a sync from the bucket deletes the placeholders of
removed objects, and its manifest is updated from the listing the sync
made. The .ebrains folder that describes it is left out of both sides of
every sync, and so are the files of interrupted transfers (see
never_synced).
"""

from __future__ import annotations

import os
import re
import tempfile
import time
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

from .exclude import exclude_files
from .local import (
    LocalListing,
    compute_md5,
    is_case_insensitive,
    is_link,
    local_file_entry,
    scan_local_folder,
)
from .model import (
    ActionResult,
    BucketStorage,
    DownloadProgress,
    FileEntry,
    SyncOptions,
    SyncWarning,
)
from .paths import DOWNLOAD_PART_SUFFIX, is_transfer_leftover, normalize_prefix
from .plan import deletion_refusal, match_key, plan_sync
from .remote import list_remote_files
from .storage import is_transient_error
from .virtual_bucket import (
    BucketInfo,
    delete_removed_placeholders,
    describe,
    is_metadata_path,
    manifest_after_push,
    protect_placeholders,
    read_bucket_info,
    read_manifest,
    virtual_bucket_refusal,
    write_bucket_info,
    write_manifest,
)

_ACTION_NAMES = {"copy": "upload", "delete": "delete", "none": "none"}
_DOWNLOAD_ACTION_NAMES = {"copy": "download", "delete": "delete", "none": "none"}
_SIGNED_QUERY = re.compile(r"\?[^\s'\"()]*?[\w-]*sig[\w-]*=[^\s'\"()]*", re.IGNORECASE)
"""The query string of a URL with a signature parameter, such as X-Amz-Signature."""
_T = TypeVar("_T")
_UNCHECKED: Any = object()
"""Default of the judged_file argument of download_object: no check."""


def never_synced(path: str) -> bool:
    """Whether a path is left out of both sides of every sync, before the exclude patterns.

    These are the .ebrains folder and every path below it, which describe
    a virtual bucket (virtual_bucket.is_metadata_path), and the files of
    interrupted transfers (paths.is_transfer_leftover). Such a path is
    never uploaded, downloaded or deleted, is never recorded in the
    manifest of a virtual bucket, and is not shown by the virtual bucket
    browser.
    """
    return is_metadata_path(path) or is_transfer_leftover(path)


class SyncRefused(Exception):
    """The sync stopped before changing anything: the plan looked like a mistake."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class SizeMismatchError(Exception):
    """A downloaded object has another size than the listing gave it."""


class LocalPathError(Exception):
    """A sync from the bucket left a local path as it is: replacing or deleting it was unsafe."""


class LocalFileChanged(OSError):
    """A file changed between the moment it was judged and the moment it was to be replaced."""


@dataclass(frozen=True)
class SyncEvent:
    """What the engine reports as it goes, to a console or a window.

    kind is one of:
        "planned"       The plan is made; results holds it. message holds
                        the reason a real run would refuse it, or "".
        "upload"        An upload starts: path, bytes, index of total.
        "uploaded"      It finished.
        "upload_failed" It failed; message says why.
        "download", "downloaded", "download_failed"
                        The same for downloads.
        "delete", "deleted", "delete_failed"
                        The same for deletions.
        "finished"      The sync is done; results holds the outcome.

    Transfers run on several threads, so the handler is called from any of
    them, and the ActionResult objects in results are updated in place as
    the sync goes.
    """

    kind: str
    path: str = ""
    bytes: int = 0
    index: int = 0
    total: int = 0
    message: str = ""
    results: tuple[ActionResult, ...] = ()


EventHandler = Callable[[SyncEvent], None]


def sync_to_bucket(
    local_folder: str | os.PathLike[str],
    bucket: str,
    storage: BucketStorage,
    options: SyncOptions | None = None,
    *,
    on_event: EventHandler | None = None,
    attempts: int = 3,
    retry_wait: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    md5: Callable[[Path], str] = compute_md5,
) -> list[ActionResult]:
    """Upload the files of local_folder that the bucket lacks or that changed.

    Returns one ActionResult per file found on either side, sorted by
    path. With options.dry_run the plan is returned and nothing is
    changed. Raises SyncRefused, before anything is changed, when the
    planned deletions look like a mistake (see plan.deletion_refusal) or
    when local_folder is a virtual bucket of another bucket or folder
    (code "OtherBucket"), and VirtualBucketError when its .ebrains folder
    is of a format this version does not know.

    A linked or unreadable local folder holds back all deletions, as a
    failed upload does: its files are missing from the listing, so the plan
    would take their objects for extraneous. Raises NotADirectoryError
    when local_folder is not a folder and PermissionError when it cannot
    be read.

    A failed upload is tried again up to `attempts` times in all when the
    error looks transient, waiting retry_wait seconds, doubled each time.
    """
    options = options or SyncOptions()
    local_folder = Path(local_folder)
    emit = on_event or (lambda event: None)
    prefix = normalize_prefix(options.prefix)

    # A virtual bucket of another bucket or folder would end up holding the
    # objects of two, so the sync is refused before the bucket is listed. A
    # dry run changes nothing, so it shows the plan with the refusal instead.
    bucket_info = read_bucket_info(local_folder)
    other_bucket = virtual_bucket_refusal(bucket_info, bucket, prefix)
    if other_bucket is not None and not options.dry_run:
        raise SyncRefused(other_bucket.code, other_bucket.reason)
    is_virtual_bucket = bucket_info is not None and other_bucket is None
    manifest = read_manifest(local_folder) if is_virtual_bucket else None

    # 1. List both sides. The listing before exclude patterns becomes the
    # manifest of a virtual bucket.
    local_listing = scan_local_folder(local_folder)
    if "" in local_listing.unreadable_folders:
        raise PermissionError(
            f"The folder {str(local_folder)!r} could not be read. "
            "Check that you have permission to read it."
        )
    unexcluded_local_files = _synced_files(local_listing.files)
    listed_at = datetime.now(timezone.utc)
    listing = _synced_files(list_remote_files(storage, bucket, prefix))
    patterns = tuple(options.exclude)
    local_files = exclude_files(unexcluded_local_files, patterns)
    remote_files = exclude_files(listing, patterns)
    hold_back = _hold_back_reason(local_listing, patterns, False, local_folder)

    _warn_if_remote_times_unknown(local_files, remote_files, options.comparison, bucket, prefix)

    if options.comparison == "Checksum":
        local_files = _add_local_checksums(local_files, remote_files, local_folder, md5)

    # 2. Plan
    plan = plan_sync(
        local_files, remote_files, comparison=options.comparison, delete=options.delete
    )
    if is_virtual_bucket:
        # The listing says which objects are not empty now, and the manifest
        # which were not empty when the placeholders were made, so that a
        # placeholder of an object removed since is not uploaded as a new
        # empty object.
        plan = protect_placeholders(plan, local_files, listing, manifest)
    results = [
        ActionResult(item.path, _ACTION_NAMES[item.action], item.reason, item.bytes)
        for item in plan
    ]
    refusal = other_bucket or deletion_refusal(plan, len(local_files), options.max_delete)
    upload_indices = [k for k, result in enumerate(results) if result.action == "upload"]
    delete_indices = [k for k, result in enumerate(results) if result.action == "delete"]

    if options.dry_run:
        # A dry run shows the plan, a refusal included, rather than stop. A
        # deletion refusal is reported on the deletions; OtherBucket stops
        # the uploads as well.
        _mark_dry_run(results, upload_indices, other_bucket.reason if other_bucket else "")
        _mark_dry_run(results, delete_indices, refusal.reason if refusal else hold_back)
        emit(
            SyncEvent("planned", results=tuple(results), message=refusal.reason if refusal else "")
        )
        return results

    emit(SyncEvent("planned", results=tuple(results)))
    if refusal is not None:
        raise SyncRefused(refusal.code, refusal.reason)

    # 3. Upload. The manifest of a virtual bucket records the checksum of
    # each uploaded file, read right before the upload so that it is of the
    # content that is sent; a file that cannot be read fails its row, as one
    # that cannot be uploaded does.
    checksums = {entry.path: entry.hash for entry in local_files if entry.hash}

    def upload(result: ActionResult) -> None:
        if is_virtual_bucket and result.path not in checksums:
            checksums[result.path] = md5(local_folder / result.path)
        _with_attempts(
            lambda: storage.upload(bucket, prefix + result.path, local_folder / result.path),
            attempts,
            retry_wait,
            sleep,
        )

    _run_actions(results, upload_indices, "upload", upload, options.workers, emit)

    # 4. Delete. A failed upload may mean the folder was listed wrongly or
    # the connection is lost, so nothing is deleted after one, the way
    # rsync and rclone hold back deletions after an error.
    if not hold_back and any(result.status == "failed" for result in results):
        hold_back = "Not deleted, since a file failed to upload."
    if hold_back:
        _hold_back(results, delete_indices, hold_back)
        delete_indices = []

    _run_actions(
        results,
        delete_indices,
        "delete",
        lambda result: storage.delete_object(bucket, prefix + result.path),
        None,
        emit,
    )

    if is_virtual_bucket:
        _update_metadata(
            local_folder,
            bucket_info,
            listed_at,
            listing,
            unexcluded_local_files,
            manifest,
            local_files,
            results,
            checksums,
        )

    emit(SyncEvent("finished", results=tuple(results)))
    return results


def sync_from_bucket(
    bucket: str,
    local_folder: str | os.PathLike[str],
    storage: BucketStorage,
    options: SyncOptions | None = None,
    *,
    on_event: EventHandler | None = None,
    attempts: int = 3,
    retry_wait: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    md5: Callable[[Path], str] = compute_md5,
    ignore_case: bool | None = None,
) -> list[ActionResult]:
    """Download the objects that local_folder lacks or that changed.

    local_folder is created if it does not exist. Returns one
    ActionResult per file found on either side, sorted by path, with
    action "download", "delete" or "none". options.delete deletes the
    local files that the bucket does not have. With options.dry_run the
    plan is returned and nothing is changed. Raises SyncRefused, before
    anything is changed, when the planned deletions look like a mistake
    (see plan.deletion_refusal) or when local_folder is a virtual bucket
    of another bucket or folder (code "OtherBucket"), VirtualBucketError
    when its .ebrains folder is of a format this version does not know,
    and NotADirectoryError when local_folder is a file.

    Every local file the sync replaces or deletes is one that the listing
    of local_folder found. Each object is downloaded into a temporary
    file, which replaces the local file only once it is complete and has
    the size the listing gave the object, and the file gets the time of
    the object. A download is refused, as a failed row, when its local
    path is a folder, lies below a linked folder, or holds something the
    listing did not find as a file. A linked or unreadable local folder
    holds back all deletions, as a failed download does.

    ignore_case says whether the file system of local_folder ignores the
    case of names, where a local file and an object whose names differ
    only in case are one file; None probes it (see
    local.is_case_insensitive). Where it does, objects whose names differ
    from each other only in case are left out but the first, with a
    warning, since the folder cannot hold them apart.

    A failed download is tried again up to `attempts` times in all when
    the error looks transient, waiting retry_wait seconds, doubled each
    time.
    """
    options = options or SyncOptions()
    local_folder = Path(local_folder)
    emit = on_event or (lambda event: None)
    prefix = normalize_prefix(options.prefix)
    if local_folder.exists() and not local_folder.is_dir():
        raise NotADirectoryError(f"{str(local_folder)!r} is a file. Give the path of a folder.")

    # A virtual bucket of another bucket or folder would end up holding the
    # objects of two, so the sync is refused before the bucket is listed. A
    # dry run changes nothing, so it shows the plan with the refusal instead.
    bucket_info = read_bucket_info(local_folder)
    other_bucket = virtual_bucket_refusal(bucket_info, bucket, prefix)
    if other_bucket is not None and not options.dry_run:
        raise SyncRefused(other_bucket.code, other_bucket.reason)
    is_virtual_bucket = bucket_info is not None and other_bucket is None
    manifest = read_manifest(local_folder) if is_virtual_bucket else None

    # 1. List both sides. The listing before exclude patterns becomes the
    # manifest of a virtual bucket.
    local_listing = scan_local_folder(local_folder) if local_folder.is_dir() else LocalListing()
    if "" in local_listing.unreadable_folders:
        raise PermissionError(
            f"The folder {str(local_folder)!r} could not be read. "
            "Check that you have permission to read it."
        )
    unexcluded_local_files = _synced_files(local_listing.files)
    listed_at = datetime.now(timezone.utc)
    listing = _synced_files(list_remote_files(storage, bucket, prefix))

    # On a file system that ignores case, a local file and an object whose
    # names differ only in case are one file, so they are matched as one,
    # and so are the exclude patterns: an object whose name differs only in
    # case from an excluded local file would otherwise be downloaded over it.
    if ignore_case is None:
        ignore_case = is_case_insensitive(
            local_folder, [entry.path for entry in local_listing.files]
        )
    key = match_key(ignore_case)
    patterns = tuple(options.exclude)
    local_files = exclude_files(unexcluded_local_files, patterns, ignore_case=ignore_case)
    remote_files = exclude_files(listing, patterns, ignore_case=ignore_case)
    hold_back = _hold_back_reason(local_listing, patterns, ignore_case, local_folder)
    if ignore_case:
        remote_files = _drop_case_collisions(remote_files, bucket, prefix)

    _warn_if_remote_times_unknown(
        local_files, remote_files, options.comparison, bucket, prefix, key
    )

    if options.comparison == "Checksum":
        local_files = _add_local_checksums(local_files, remote_files, local_folder, md5, key)

    # 2. Plan
    plan = plan_sync(
        remote_files,
        local_files,
        comparison=options.comparison,
        delete=options.delete,
        ignore_case=ignore_case,
    )
    if is_virtual_bucket:
        # The folder stands for the bucket, so the placeholder of an object
        # removed since the manifest stands for nothing and holds no data.
        plan = delete_removed_placeholders(plan, local_files, listing, manifest)
    results = [
        ActionResult(item.path, _DOWNLOAD_ACTION_NAMES[item.action], item.reason, item.bytes)
        for item in plan
    ]
    refusal = other_bucket or deletion_refusal(plan, len(remote_files), options.max_delete)
    download_indices = [k for k, result in enumerate(results) if result.action == "download"]
    delete_indices = [k for k, result in enumerate(results) if result.action == "delete"]

    if options.dry_run:
        _mark_dry_run(results, download_indices, other_bucket.reason if other_bucket else "")
        _mark_dry_run(results, delete_indices, refusal.reason if refusal else hold_back)
        emit(
            SyncEvent("planned", results=tuple(results), message=refusal.reason if refusal else "")
        )
        return results

    emit(SyncEvent("planned", results=tuple(results)))
    if refusal is not None:
        raise SyncRefused(refusal.code, refusal.reason)

    # 3. Download. A row is planned under the name of its object, and a file
    # it replaces or deletes is found under its listed name, which differs
    # in case where case is ignored.
    local_folder.mkdir(parents=True, exist_ok=True)
    local_by_path = {entry.path: entry for entry in local_files}
    local_by_key = {key(entry.path): entry for entry in local_files}
    remote_by_path = {entry.path: entry for entry in remote_files}

    def download(result: ActionResult) -> None:
        remote = remote_by_path[result.path]
        local = local_by_key.get(key(result.path))
        relative_path = remote.path if local is None else local.path
        object_name = prefix + remote.path
        _check_download_path(local_folder, relative_path, object_name, is_listed=local is not None)
        _with_attempts(
            lambda: download_object(
                storage,
                bucket,
                object_name,
                local_folder / relative_path,
                expected_bytes=remote.bytes,
                modified_time=remote.modified_time,
            ),
            attempts,
            retry_wait,
            sleep,
        )

    _run_actions(results, download_indices, "download", download, options.workers, emit)

    # 4. Delete. A failed download may mean the bucket was listed wrongly
    # or the connection is lost, so nothing is deleted after one, the way
    # rsync and rclone hold back deletions after an error.
    if not hold_back and any(result.status == "failed" for result in results):
        hold_back = "Not deleted, since a file failed to download."
    if hold_back:
        _hold_back(results, delete_indices, hold_back)
        delete_indices = []

    def delete(result: ActionResult) -> None:
        # The path of a row to delete is that of a listed local file
        path = local_folder / local_by_path[result.path].path
        if result.reason == "placeholder" and path.stat().st_size > 0:
            raise LocalPathError(
                f'The placeholder "{result.path}" was not deleted, since it holds data now.'
            )
        path.unlink()

    _run_actions(results, delete_indices, "delete", delete, None, emit)

    if is_virtual_bucket:
        kept = _undeleted_placeholders(results, local_folder, local_by_path, manifest)
        write_manifest(local_folder, sorted(listing + kept, key=lambda entry: entry.path))
        write_bucket_info(local_folder, replace(bucket_info, listed_at=listed_at))

    emit(SyncEvent("finished", results=tuple(results)))
    return results


def download_object(
    storage: BucketStorage,
    bucket: str,
    object_name: str,
    target: str | os.PathLike[str],
    *,
    expected_bytes: int | None = None,
    modified_time: datetime | None = None,
    on_progress: DownloadProgress | None = None,
    judged_file: FileEntry | None = _UNCHECKED,
) -> None:
    """Download an object into target, replacing it only once the download is complete.

    The object is received in a temporary file next to the target, named
    with DOWNLOAD_PART_SUFFIX so that every sync leaves it out, so that the
    replacement is a rename on the same file system and a failed download
    leaves the target as it was: the empty placeholder of a virtual
    bucket, or an older copy. The folder of the target is created if
    needed.

    expected_bytes is the size the listing reports for the object. A
    download of another size raises SizeMismatchError and leaves the
    target untouched, since the object was replaced between the listing
    and the download. modified_time is the time of the object, which the
    file is given: a file with the time of its object is unchanged to the
    time comparison of a later sync in either direction, where one with
    the time of the download would be newer than its object and be
    uploaded again. on_progress is passed to the storage.

    judged_file is the entry of the file at target as it was read when the
    download was decided, or None when there was no file. Right before the
    download is put in place, the file must still have the size and time
    of that entry, or still be missing; otherwise the download is
    discarded with LocalFileChanged, since the file holds data that was
    not judged. Left out, the file is not checked.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        dir=target.parent, prefix=target.name + ".", suffix=DOWNLOAD_PART_SUFFIX
    )
    os.close(handle)
    temporary = Path(name)
    try:
        storage.download(bucket, object_name, temporary, on_progress=on_progress)
        size = temporary.stat().st_size
        if expected_bytes is not None and size != expected_bytes:
            raise SizeMismatchError(
                f"The downloaded file {object_name!r} has {size} bytes, "
                f"where the object has {expected_bytes}."
            )
        if modified_time is not None:
            seconds = modified_time.timestamp()
            os.utime(temporary, (seconds, seconds))
        if judged_file is not _UNCHECKED and not _is_as_judged(target, judged_file):
            raise LocalFileChanged(
                f'The file "{target}" changed while the object was downloaded, so the '
                "download was discarded. Refresh and try again."
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _is_as_judged(target: Path, judged_file: FileEntry | None) -> bool:
    """Whether the file at target still has the size and time of its entry, or is still missing."""
    current = local_file_entry(target.parent, target.name)
    if current is None or judged_file is None:
        return current is None and judged_file is None
    return (current.bytes, current.modified_time) == (judged_file.bytes, judged_file.modified_time)


def _synced_files(files: list[FileEntry]) -> list[FileEntry]:
    """The files a sync may touch: those outside .ebrains that are not leftovers of a transfer."""
    return [entry for entry in files if not never_synced(entry.path)]


def _without_metadata(files: list[FileEntry]) -> list[FileEntry]:
    """The files outside .ebrains, which describes a virtual bucket and is never synced."""
    return [entry for entry in files if not is_metadata_path(entry.path)]


def _mark_dry_run(results: list[ActionResult], indices: list[int], skip_reason: str) -> None:
    """Report actions of a dry run as planned, or as skipped for skip_reason if it is not ""."""
    for k in indices:
        if skip_reason:
            results[k].status = "skipped"
            results[k].message = skip_reason
        else:
            results[k].status = "planned"


def _hold_back(results: list[ActionResult], indices: list[int], reason: str) -> None:
    """Report actions as skipped for reason, without running them."""
    for k in indices:
        results[k].status = "skipped"
        results[k].message = reason


_EVENT_KINDS = {
    "upload": ("upload", "uploaded", "upload_failed"),
    "download": ("download", "downloaded", "download_failed"),
    "delete": ("delete", "deleted", "delete_failed"),
}


def _run_actions(
    results: list[ActionResult],
    indices: list[int],
    kind: str,
    operation: Callable[[ActionResult], None],
    workers: int | None,
    emit: EventHandler,
) -> None:
    """Run operation on results[k] for each k of indices, on `workers` threads.

    The status of each result becomes "done", or "failed" with the error
    in its message, and the events of kind ("upload", "download" or
    "delete") report each start and outcome. With workers None the actions
    run one after another in the calling thread, so that an interrupt
    stops the ones not yet started.
    """
    start, done, failed = _EVENT_KINDS[kind]
    total = len(indices)

    def run(index: int, k: int) -> None:
        result = results[k]
        emit(SyncEvent(start, result.path, result.bytes, index, total))
        try:
            operation(result)
        except Exception as error:
            result.status = "failed"
            result.message = _error_message(error)
            emit(SyncEvent(failed, result.path, result.bytes, index, total, result.message))
        else:
            result.status = "done"
            emit(SyncEvent(done, result.path, result.bytes, index, total))

    if workers is None:
        for index, k in enumerate(indices, 1):
            run(index, k)
        return

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, index, k) for index, k in enumerate(indices, 1)]
        for future in futures:
            future.result()


def _update_metadata(
    local_folder: Path,
    info: BucketInfo,
    listed_at: datetime,
    listing: list[FileEntry],
    unexcluded_local_files: list[FileEntry],
    manifest: list[FileEntry] | None,
    local_files: list[FileEntry],
    results: list[ActionResult],
    checksums: dict[str, str],
) -> None:
    """Record the listing of the sync as the manifest of the virtual bucket.

    The sync changed the bucket after the listing, so the files it uploaded
    and the objects it deleted are applied to the listing. A failed
    transfer leaves the entry as the listing gave it. The placeholders of
    removed objects keep their entries from the manifest before the sync,
    an excluded one too, since the manifest is made before exclude patterns.
    checksums holds the MD5 checksum of each uploaded file by path, read
    before its upload: the file states compare an uploaded file by its
    checksum until a listing gives its object a time.
    """
    uploaded_paths = {r.path for r in results if r.action == "upload" and r.status == "done"}
    deleted_paths = [r.path for r in results if r.action == "delete" and r.status == "done"]
    uploaded = [
        replace(entry, hash=checksums.get(entry.path, ""))
        for entry in local_files
        if entry.path in uploaded_paths
    ]
    new_manifest = manifest_after_push(
        listing, uploaded, deleted_paths, local=unexcluded_local_files, manifest=manifest
    )
    write_manifest(local_folder, new_manifest)
    write_bucket_info(local_folder, replace(info, listed_at=listed_at))


def _undeleted_placeholders(
    results: list[ActionResult],
    local_folder: Path,
    local_by_path: dict[str, FileEntry],
    manifest: list[FileEntry] | None,
) -> list[FileEntry]:
    """The manifest entries of the placeholders of removed objects that a pull left in place.

    A placeholder whose deletion was held back or failed still stands for
    its object, so its entry stays in the manifest, as after a push, and
    the next push does not upload the empty file as a new object. A file
    that holds data by now is no placeholder and gets no entry.
    """
    manifest_by_path = {entry.path: entry for entry in manifest or ()}
    kept = []
    for result in results:
        if result.reason != "placeholder" or result.status == "done":
            continue
        try:
            is_empty = (local_folder / local_by_path[result.path].path).stat().st_size == 0
        except OSError:
            is_empty = False
        if is_empty:
            kept.append(manifest_by_path[result.path])
    return kept


def _hold_back_reason(
    listing: LocalListing, patterns: tuple[str, ...], ignore_case: bool, local_folder: Path
) -> str:
    """Why the deletions of a sync from the bucket must be held back, or "".

    The files of an unreadable local folder are missing from the listing,
    and so are those of a linked folder, which are stored elsewhere: the
    plan would take them for files the folder lacks. Nothing is deleted
    then, the way rsync holds back deletions after an error while listing.
    A folder that is excluded does not count.
    """

    def kept(paths: list[str]) -> list[str]:
        entries = _without_metadata([FileEntry(path, 0) for path in paths])
        return [entry.path for entry in exclude_files(entries, patterns, ignore_case=ignore_case)]

    reason = ""
    unreadable = kept(listing.unreadable_folders)
    if unreadable:
        warnings.warn(
            f"{len(unreadable)} folder(s) below {str(local_folder)!r} could not be read, for "
            f"example {unreadable[0]!r}. Their files could not be listed, so nothing is deleted.",
            SyncWarning,
            stacklevel=3,
        )
        reason = "Not deleted, since a local folder could not be read."

    linked = kept(listing.linked_folders)
    if linked:
        warnings.warn(
            f"{len(linked)} folder(s) below {str(local_folder)!r} are symbolic links, for "
            f"example {linked[0]!r}. Their files are not synced, so nothing is deleted. "
            "Exclude the links to sync the rest with deletions.",
            SyncWarning,
            stacklevel=3,
        )
        reason = reason or "Not deleted, since a local folder is a symbolic link."
    return reason


def _drop_case_collisions(files: list[FileEntry], bucket: str, prefix: str) -> list[FileEntry]:
    """The objects a folder that ignores case can hold apart: the first of names equal but in case.

    All of the others would be downloaded into the same local file.
    """
    kept: dict[str, FileEntry] = {}
    dropped = []
    for entry in files:
        if entry.path.lower() in kept:
            dropped.append(entry.path)
        else:
            kept[entry.path.lower()] = entry
    if dropped:
        warnings.warn(
            f"{len(dropped)} object(s) of {describe(bucket, prefix)} have a name that differs "
            "from another only in case, which the local folder cannot hold apart, for example "
            f"{dropped[0]!r}. They are left out of the sync.",
            SyncWarning,
            stacklevel=3,
        )
    return list(kept.values())


def _check_download_path(
    local_folder: Path, relative_path: str, object_name: str, *, is_listed: bool
) -> None:
    """Refuse a download whose local path cannot take the object, before it starts.

    The download replaces only a file the listing found: a folder at the
    path, or anything else the listing did not find there, is left, and so
    is the content of a linked folder, which is stored outside the synced
    folder.
    """
    target = local_folder / relative_path
    for parent in reversed(Path(relative_path).parents[:-1]):
        if is_link(local_folder / parent):
            raise LocalPathError(
                f'The object "{object_name}" was not downloaded, since its local folder '
                f'"{local_folder / parent}" is a symbolic link.'
            )
    if target.is_dir():
        raise LocalPathError(
            f'The object "{object_name}" was not downloaded, since its local path "{target}" '
            "is a folder. Rename or remove the folder to download the object."
        )
    if not is_listed and os.path.lexists(target):
        raise LocalPathError(
            f'The object "{object_name}" was not downloaded, since its local path "{target}" '
            "holds something the listing of the folder did not find. Rename or remove it to "
            "download the object."
        )


def _error_message(error: BaseException) -> str:
    """What an error says, without the query strings of signed URLs.

    An upload goes to a signed URL, which lets anyone who holds it write
    the object until it expires, and the HTTP libraries quote the URL a
    request failed on. The message is printed and written to plan files,
    so the query string with the signature is left out.
    """
    return _SIGNED_QUERY.sub("", str(error)) or type(error).__name__


def _with_attempts(
    operation: Callable[[], _T], attempts: int, wait: float, sleep: Callable[[float], None]
) -> _T:
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except Exception as error:
            if attempt == attempts or not is_transient_error(error):
                raise
            sleep(wait * 2 ** (attempt - 1))
    raise AssertionError("unreachable")


def _warn_if_remote_times_unknown(
    local_files: list[FileEntry],
    remote_files: list[FileEntry],
    comparison: str,
    bucket: str,
    prefix: str,
    key: Callable[[str], str] = str,
) -> None:
    """Say so when the time comparison cannot work.

    Without modification times, the default comparison judges every file of
    the same size on both sides unchanged, which should not pass in
    silence. Only files on both sides are compared by time, so the warning
    is for those.
    """
    if comparison != "SizeAndTime":
        return
    local_keys = {key(entry.path) for entry in local_files}
    common = [entry for entry in remote_files if key(entry.path) in local_keys]
    if common and all(entry.modified_time is None for entry in common):
        where = f"bucket {bucket!r}" + (f", folder {prefix!r}" if prefix else "")
        warnings.warn(
            f"The listing of {where} reports no modification times, so files of the same "
            'size on both sides are judged unchanged. Use comparison="Checksum" to compare '
            "their content instead.",
            SyncWarning,
            stacklevel=3,
        )


def _add_local_checksums(
    local_files: list[FileEntry],
    remote_files: list[FileEntry],
    local_folder: Path,
    md5: Callable[[Path], str],
    key: Callable[[str], str] = str,
) -> list[FileEntry]:
    """Compute the checksums the comparison needs.

    Only a local file whose object has the same size and a known checksum
    is read: for any other file the size or the time decides, and reading
    every file of a large folder would take long.
    """
    remote_by_key = {key(entry.path): entry for entry in remote_files}
    with_checksums = []
    for entry in local_files:
        counterpart = remote_by_key.get(key(entry.path))
        if counterpart is not None and counterpart.hash and counterpart.bytes == entry.bytes:
            entry = FileEntry(
                entry.path, entry.bytes, entry.modified_time, md5(local_folder / entry.path)
            )
        with_checksums.append(entry)
    return with_checksums
