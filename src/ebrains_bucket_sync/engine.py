"""Make a bucket match a local folder.

The sync works in four steps, as ebrains.bucket.sync.toBucket of the MATLAB
toolbox does:
    1. List the files on both sides, without the excluded ones.
    2. Plan what to upload and delete (plan.plan_sync).
    3. Upload the new and changed files.
    4. Delete the extraneous objects, if asked to and no upload failed.
An upload that fails does not stop the sync: the other files are
uploaded, the failure is recorded in the result, and nothing is deleted.
"""

from __future__ import annotations

import os
import re
import time
import warnings
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from .exclude import exclude_files
from .local import compute_md5, list_local_files
from .model import ActionResult, BucketStorage, FileEntry, SyncOptions, SyncWarning
from .paths import normalize_prefix
from .plan import deletion_refusal, plan_sync
from .remote import list_remote_files
from .storage import is_transient_error

ALWAYS_EXCLUDED = ("*.multipart_manifest.json",)
"""The manifest ebrains_drive keeps next to a file while a multipart upload
is in progress. It is not data, so it is never uploaded."""

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
    planned deletions look like a mistake (see plan.deletion_refusal).

    A failed upload is tried again up to `attempts` times in all when the
    error looks transient, waiting retry_wait seconds, doubled each time.
    """
    options = options or SyncOptions()
    local_folder = Path(local_folder)
    emit = on_event or (lambda event: None)
    prefix = normalize_prefix(options.prefix)

    # 1. List both sides
    local_files = list_local_files(local_folder)
    remote_files = list_remote_files(storage, bucket, prefix)
    patterns = tuple(options.exclude) + ALWAYS_EXCLUDED
    local_files = exclude_files(local_files, patterns)
    remote_files = exclude_files(remote_files, patterns)

    _warn_if_remote_times_unknown(local_files, remote_files, options.comparison, bucket, prefix)

    if options.comparison == "Checksum":
        local_files = _add_local_checksums(local_files, remote_files, local_folder, md5)

    # 2. Plan
    plan = plan_sync(
        local_files, remote_files, comparison=options.comparison, delete=options.delete
    )
    results = [
        ActionResult(item.path, _ACTION_NAMES[item.action], item.reason, item.bytes)
        for item in plan
    ]
    refusal = deletion_refusal(plan, len(local_files), options.max_delete)
    upload_indices = [k for k, result in enumerate(results) if result.action == "upload"]
    delete_indices = [k for k, result in enumerate(results) if result.action == "delete"]

    if options.dry_run:
        # A dry run shows the plan, a refusal included, rather than stop
        for k in upload_indices:
            results[k].status = "planned"
        for k in delete_indices:
            if refusal is None:
                results[k].status = "planned"
            else:
                results[k].status = "skipped"
                results[k].message = refusal.reason
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

    emit(SyncEvent("finished", results=tuple(results)))
    return results


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
