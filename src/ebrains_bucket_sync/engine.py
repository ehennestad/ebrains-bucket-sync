"""Make a bucket match a local folder.

The sync works in four steps, as ebrains.bucket.sync.toBucket of the MATLAB
toolbox does:
    1. List the files on both sides, without the excluded ones.
    2. Plan what to upload and delete (plan.plan_sync).
    3. Upload the new and changed files.
    4. Delete the extraneous objects, if asked to and no upload failed.
An upload that fails does not stop the sync: the other files are
uploaded, the failure is recorded in the result, and nothing is deleted.

download_object fetches one object into a local file the way a sync from
the bucket does, which the virtual bucket browser uses to fill a
placeholder.

When the local folder is a virtual bucket (see virtual_bucket.read_bucket_info),
the sync must be of its bucket and prefix, its placeholders are left alone,
and its manifest is updated from the listing the sync made. The .ebrains
folder that describes it is left out of both sides of every sync.
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
from typing import TypeVar

from .exclude import exclude_files
from .local import compute_md5, list_local_files
from .model import (
    ActionResult,
    BucketStorage,
    DownloadProgress,
    FileEntry,
    SyncOptions,
    SyncWarning,
)
from .paths import normalize_prefix
from .plan import Refusal, deletion_refusal, plan_sync
from .remote import list_remote_files
from .storage import is_transient_error
from .virtual_bucket import (
    BucketInfo,
    is_metadata_path,
    manifest_after_push,
    protect_placeholders,
    read_bucket_info,
    read_manifest,
    virtual_bucket_refusal,
    write_bucket_info,
    write_manifest,
)

ALWAYS_EXCLUDED = ("*.multipart_manifest.json",)
"""The manifest ebrains_drive keeps next to a file while a multipart upload
is in progress. It is not data, so it is never uploaded."""

DOWNLOAD_PART_SUFFIX = ".sync-part"
"""Suffix of the temporary file an object is received in, next to its target."""

_ACTION_NAMES = {"copy": "upload", "delete": "delete", "none": "none"}
_SIGNED_QUERY = re.compile(r"\?[^\s'\"()]*?[\w-]*sig[\w-]*=[^\s'\"()]*", re.IGNORECASE)
"""The query string of a URL with a signature parameter, such as X-Amz-Signature."""
_T = TypeVar("_T")


class SyncRefused(Exception):
    """The sync stopped before changing anything: the plan looked like a mistake."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class SizeMismatchError(Exception):
    """A downloaded object has another size than the listing gave it."""


@dataclass(frozen=True)
class SyncEvent:
    """What the engine reports as it goes, to a console or a window.

    kind is one of:
        "planned"       The plan is made; results holds it. message holds
                        the reason a real run would refuse it, or "".
        "upload"        An upload starts: path, bytes, index of total.
        "uploaded"      It finished.
        "upload_failed" It failed; message says why.
        "delete", "deleted", "delete_failed"
                        The same for deletions.
        "finished"      The sync is done; results holds the outcome.

    Uploads run on several threads, so the handler is called from any of
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

    # 1. List both sides. The listing before exclude patterns becomes the
    # manifest of a virtual bucket.
    local_files = _without_metadata(list_local_files(local_folder))
    listed_at = datetime.now(timezone.utc)
    listing = _without_metadata(list_remote_files(storage, bucket, prefix))
    patterns = tuple(options.exclude) + ALWAYS_EXCLUDED
    local_files = exclude_files(local_files, patterns)
    remote_files = exclude_files(listing, patterns)

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
        plan = protect_placeholders(plan, local_files, listing, read_manifest(local_folder))
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
        _mark_dry_run(results, upload_indices, other_bucket)
        _mark_dry_run(results, delete_indices, refusal)
        emit(
            SyncEvent("planned", results=tuple(results), message=refusal.reason if refusal else "")
        )
        return results

    emit(SyncEvent("planned", results=tuple(results)))
    if refusal is not None:
        raise SyncRefused(refusal.code, refusal.reason)

    # 3. Upload
    total = len(upload_indices)

    def upload(index: int, k: int) -> None:
        result = results[k]
        emit(SyncEvent("upload", result.path, result.bytes, index, total))
        try:
            _with_attempts(
                lambda: storage.upload(bucket, prefix + result.path, local_folder / result.path),
                attempts,
                retry_wait,
                sleep,
            )
        except Exception as error:
            result.status = "failed"
            result.message = _error_message(error)
            emit(
                SyncEvent("upload_failed", result.path, result.bytes, index, total, result.message)
            )
        else:
            result.status = "done"
            emit(SyncEvent("uploaded", result.path, result.bytes, index, total))

    with ThreadPoolExecutor(max_workers=options.workers) as pool:
        futures = [pool.submit(upload, index, k) for index, k in enumerate(upload_indices, 1)]
        for future in futures:
            future.result()

    # 4. Delete. A failed upload may mean the folder was listed wrongly or
    # the connection is lost, so nothing is deleted after one, the way
    # rsync and rclone hold back deletions after an error.
    if any(result.status == "failed" for result in results):
        for k in delete_indices:
            results[k].status = "skipped"
            results[k].message = "Not deleted, since a file failed to upload."
        delete_indices = []

    total = len(delete_indices)
    for index, k in enumerate(delete_indices, 1):
        result = results[k]
        emit(SyncEvent("delete", result.path, result.bytes, index, total))
        try:
            storage.delete_object(bucket, prefix + result.path)
        except Exception as error:
            result.status = "failed"
            result.message = _error_message(error)
            emit(
                SyncEvent("delete_failed", result.path, result.bytes, index, total, result.message)
            )
        else:
            result.status = "done"
            emit(SyncEvent("deleted", result.path, result.bytes, index, total))

    if is_virtual_bucket:
        _update_metadata(local_folder, bucket_info, listed_at, listing, local_files, results)

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
) -> None:
    """Download an object into target, replacing it only once the download is complete.

    The object is received in a temporary file next to the target, so
    that the replacement is a rename on the same file system and a failed
    download leaves the target as it was: the empty placeholder of a
    virtual bucket, or an older copy. The folder of the target is created
    if needed.

    expected_bytes is the size the listing reports for the object. A
    download of another size raises SizeMismatchError and leaves the
    target untouched, since the object was replaced between the listing
    and the download. modified_time is the time of the object, which the
    file is given: a file with the time of its object is unchanged to the
    time comparison of a later sync in either direction, where one with
    the time of the download would be newer than its object and be
    uploaded again. on_progress is passed to the storage.
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
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _without_metadata(files: list[FileEntry]) -> list[FileEntry]:
    """The files outside .ebrains, which describes a virtual bucket and is never synced."""
    return [entry for entry in files if not is_metadata_path(entry.path)]


def _mark_dry_run(results: list[ActionResult], indices: list[int], refusal: Refusal | None) -> None:
    """Report actions of a dry run as planned, or as skipped by the refusal that stops them."""
    for k in indices:
        if refusal is None:
            results[k].status = "planned"
        else:
            results[k].status = "skipped"
            results[k].message = refusal.reason


def _update_metadata(
    local_folder: Path,
    info: BucketInfo,
    listed_at: datetime,
    listing: list[FileEntry],
    local_files: list[FileEntry],
    results: list[ActionResult],
) -> None:
    """Record the listing of the sync as the manifest of the virtual bucket.

    The sync changed the bucket after the listing, so the files it uploaded
    and the objects it deleted are applied to the listing. A failed
    transfer leaves the entry as the listing gave it.
    """
    uploaded_paths = {r.path for r in results if r.action == "upload" and r.status == "done"}
    deleted_paths = [r.path for r in results if r.action == "delete" and r.status == "done"]
    uploaded = [entry for entry in local_files if entry.path in uploaded_paths]
    write_manifest(local_folder, manifest_after_push(listing, uploaded, deleted_paths))
    write_bucket_info(local_folder, replace(info, listed_at=listed_at))


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
) -> None:
    """Say so when the time comparison cannot work.

    Without modification times, the default comparison judges every file of
    the same size on both sides unchanged, which should not pass in
    silence. Only files on both sides are compared by time, so the warning
    is for those.
    """
    if comparison != "SizeAndTime":
        return
    local_paths = {entry.path for entry in local_files}
    common = [entry for entry in remote_files if entry.path in local_paths]
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
) -> list[FileEntry]:
    """Compute the checksums the comparison needs.

    Only a local file whose object has the same size and a known checksum
    is read: for any other file the size or the time decides, and reading
    every file of a large folder would take long.
    """
    remote_by_path = {entry.path: entry for entry in remote_files}
    with_checksums = []
    for entry in local_files:
        counterpart = remote_by_path.get(entry.path)
        if counterpart is not None and counterpart.hash and counterpart.bytes == entry.bytes:
            entry = FileEntry(
                entry.path, entry.bytes, entry.modified_time, md5(local_folder / entry.path)
            )
        with_checksums.append(entry)
    return with_checksums
