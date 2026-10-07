"""List the files of a bucket, or of a folder in it, for a sync."""

from __future__ import annotations

import re
import warnings
from datetime import datetime, timezone

from .model import BucketStorage, FileEntry, SyncWarning
from .paths import folder_object_flags, is_safe_relative_path

MAX_SINGLE_OBJECT_BYTES = 5 * 1024**3
"""Above this size the object store holds an object as segments behind a
manifest, whose checksum is one of the segment checksums, not of the content."""


def list_remote_files(storage: BucketStorage, bucket: str, prefix: str) -> list[FileEntry]:
    """One FileEntry per object below prefix, sorted by path.

    prefix is "" for the whole bucket or a folder name that ends with "/"
    (see paths.normalize_prefix). Paths are relative to it. Objects that
    mark folders are left out, and so are objects whose names cannot be
    used as paths below a folder, with a warning.

    modified_time is the time the object was uploaded, and None where the
    listing gives none that can be read. hash is the MD5 checksum the
    listing reports, and "" where there is none or where it cannot be the
    checksum of the content: the value of a multipart upload
    ("<md5>-<parts>"), or that of an object above 5 GiB. A smaller object
    uploaded in segments reports such a checksum too.
    """
    objects = storage.list_objects(bucket, prefix)
    if not objects:
        return []

    flags = folder_object_flags(objects)
    objects = [obj for obj, is_folder in zip(objects, flags, strict=True) if not is_folder]

    # The listing is narrowed to the prefix already; the check keeps a name
    # that does not start with it from turning into a wrong path.
    objects = [
        obj for obj in objects if obj.name.startswith(prefix) and len(obj.name) > len(prefix)
    ]

    unsafe = {obj.name for obj in objects if not is_safe_relative_path(obj.name[len(prefix) :])}
    if unsafe:
        warnings.warn(
            f"{len(unsafe)} object(s) of bucket {bucket!r} have names that do not stay below "
            f"the synced folder, for example {sorted(unsafe)[0]!r}, and are left out of the sync.",
            SyncWarning,
            stacklevel=2,
        )
        objects = [obj for obj in objects if obj.name not in unsafe]

    entries = []
    for obj in objects:
        checksum = (obj.hash or "").lower()
        if "-" in checksum or obj.bytes > MAX_SINGLE_OBJECT_BYTES:
            checksum = ""
        entries.append(
            FileEntry(
                obj.name[len(prefix) :],
                obj.bytes,
                parse_listing_time(obj.last_modified),
                checksum,
            )
        )

    entries.sort(key=lambda entry: entry.path)
    return entries


def parse_listing_time(text: str | None) -> datetime | None:
    """Read a listing time such as "2024-05-03T10:22:33.123456" as UTC.

    The object store reports times in UTC without a zone, with or without a
    fraction of a second. A "Z" or an offset such as "+02:00" is read too,
    in case a listing carries one. The fraction is dropped, which the
    tolerance of the time comparison covers. Text in any other form gives
    None, which leaves the time out of the comparison.
    """
    if not text:
        return None
    text = re.sub(r"\.\d+", "", text)
    text = re.sub(r"Z$", "+00:00", text)
    text = re.sub(r"([-+]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
