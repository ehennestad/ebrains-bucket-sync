"""The .ebrains folder of a virtual bucket, and the rules that use it.

A virtual bucket is a local folder that stands for a bucket, or for a folder
of one, with a file for each object: its data once downloaded, or an empty
placeholder until then. These functions are the reference implementation of
the rules in spec/README.md#virtual-buckets, which the MATLAB toolbox
implements too.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .model import FileEntry, PlanItem
from .paths import normalize_prefix
from .plan import TIME_TOLERANCE, Refusal, change_reason
from .remote import parse_listing_time

METADATA_FOLDER = ".ebrains"
"""Folder at the root of a virtual bucket that describes it."""

FORMAT_VERSION = 1
"""Version of the layout of the .ebrains folder that this module writes."""

BUCKET_FILE = "bucket.json"
MANIFEST_FILE = "manifest.csv"
MANIFEST_FIELDS = ("path", "bytes", "modified_time", "hash")

STATES = ("online_only", "downloaded", "differs", "local_only")
"""States of the files of a virtual bucket. See file_states."""


class VirtualBucketError(Exception):
    """A virtual bucket that this module cannot use."""


@dataclass(frozen=True)
class BucketInfo:
    """The content of .ebrains/bucket.json.

    Attributes:
        bucket: Name of the bucket.
        prefix: Folder of the bucket that the virtual bucket stands for, as
            paths.normalize_prefix gives it. "" is the whole bucket.
        listed_at: When the manifest was last made from a listing, in UTC,
            or None if it never was.
    """

    bucket: str
    prefix: str = ""
    listed_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "prefix", normalize_prefix(self.prefix))


def is_metadata_path(path: str) -> bool:
    """Whether a path relative to a synced folder is the .ebrains folder or below it.

    Such paths are left out of both sides of every sync.
    """
    return path == METADATA_FOLDER or path.startswith(METADATA_FOLDER + "/")


def read_bucket_info(root: Path) -> BucketInfo | None:
    """The bucket.json of a virtual bucket, or None if the folder has none."""
    path = Path(root) / METADATA_FOLDER / BUCKET_FILE
    if not path.is_file():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("format_version")
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise VirtualBucketError(
            f"{path} has format_version {version!r}, which this version of "
            f"ebrains-bucket-sync does not know. Update the package to use this folder."
        )
    return BucketInfo(data["bucket"], data["prefix"], parse_listing_time(data["listed_at"]))


def write_bucket_info(root: Path, info: BucketInfo) -> None:
    """Write the bucket.json of a virtual bucket, creating .ebrains if needed.

    Fields of an existing file that BucketInfo does not hold are kept.
    """
    path = Path(root) / METADATA_FOLDER / BUCKET_FILE
    data = {}
    if path.is_file():
        data = json.loads(path.read_text(encoding="utf-8"))
    data.update(
        format_version=FORMAT_VERSION,
        bucket=info.bucket,
        prefix=info.prefix,
        listed_at=format_time(info.listed_at),
    )
    write_atomically(path, json.dumps(data, indent=2) + "\n")


def read_manifest(root: Path) -> list[FileEntry] | None:
    """The manifest of a virtual bucket, or None if it was never listed."""
    path = Path(root) / METADATA_FOLDER / MANIFEST_FILE
    if not path.is_file():
        return None
    return parse_manifest(path.read_text(encoding="utf-8"))


def write_manifest(root: Path, entries: Iterable[FileEntry]) -> None:
    """Write the manifest of a virtual bucket, creating .ebrains if needed."""
    write_atomically(Path(root) / METADATA_FOLDER / MANIFEST_FILE, format_manifest(entries))


def parse_manifest(text: str) -> list[FileEntry]:
    """The file entries of the text of a manifest.csv."""
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if tuple(reader.fieldnames or ()) != MANIFEST_FIELDS:
        raise VirtualBucketError(
            f"A manifest must start with the header {','.join(MANIFEST_FIELDS)}, "
            f"not {','.join(reader.fieldnames or ())}."
        )
    return [
        FileEntry(
            row["path"],
            int(row["bytes"]),
            parse_listing_time(row["modified_time"]),
            row["hash"],
        )
        for row in reader
    ]


def format_manifest(entries: Iterable[FileEntry]) -> str:
    """The text of a manifest.csv for file entries, sorted by path."""
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(MANIFEST_FIELDS)
    for entry in sorted(entries, key=lambda entry: entry.path):
        writer.writerow([entry.path, entry.bytes, format_time(entry.modified_time), entry.hash])
    return output.getvalue()


def is_placeholder(local: FileEntry, *object_entries: FileEntry | None) -> bool:
    """Whether a local file is the empty placeholder of an object that is not.

    object_entries are what is known of the object: its entry in the
    listing the sync made and in the manifest, each None where absent.
    """
    return local.bytes == 0 and any(
        entry is not None and entry.bytes > 0 for entry in object_entries
    )


def file_states(
    local: Iterable[FileEntry], manifest: Iterable[FileEntry] | None
) -> list[tuple[str, str]]:
    """(path, state) of each path of a virtual bucket, sorted by path.

    Without a manifest, an empty file is "online_only" and any other file
    "downloaded". With one, a path only the manifest has and a placeholder
    are "online_only", a path only the local folder has is "local_only",
    and any other file is "downloaded" or "differs", by the SizeAndTime
    comparison with its manifest entry in both directions. A download gets
    the time of its object, so a local file later than its object was
    changed after it was downloaded.
    """
    local_by_path = {entry.path: entry for entry in local}
    if manifest is None:
        return sorted(
            (path, "online_only" if entry.bytes == 0 else "downloaded")
            for path, entry in local_by_path.items()
        )

    manifest_by_path = {entry.path: entry for entry in manifest}
    states = []
    for path in sorted(local_by_path.keys() | manifest_by_path.keys()):
        local_entry = local_by_path.get(path)
        manifest_entry = manifest_by_path.get(path)
        if local_entry is None:
            state = "online_only"
        elif manifest_entry is None:
            state = "local_only"
        elif is_placeholder(local_entry, manifest_entry):
            state = "online_only"
        else:
            is_unchanged = all(
                change_reason(source, target, "SizeAndTime", TIME_TOLERANCE) == "unchanged"
                for source, target in ((manifest_entry, local_entry), (local_entry, manifest_entry))
            )
            state = "downloaded" if is_unchanged else "differs"
        states.append((path, state))
    return states


def protect_placeholders(
    plan: Iterable[PlanItem],
    local: Iterable[FileEntry],
    listing: Iterable[FileEntry],
    manifest: Iterable[FileEntry] | None,
) -> list[PlanItem]:
    """The plan of a sync to the bucket, with no action for placeholders.

    An empty placeholder would otherwise replace its object, or be
    recreated as an empty object where the object was removed since the
    manifest was made. The listing the sync made says which objects are
    not empty now, the manifest which were not empty when the placeholders
    were made; without a manifest the listing alone decides.
    """
    local_by_path = {entry.path: entry for entry in local}
    listing_by_path = {entry.path: entry for entry in listing}
    manifest_by_path = {entry.path: entry for entry in manifest or ()}
    protected = []
    for item in plan:
        local_entry = local_by_path.get(item.path)
        if local_entry is not None and is_placeholder(
            local_entry, listing_by_path.get(item.path), manifest_by_path.get(item.path)
        ):
            item = replace(item, action="none", reason="placeholder")
        protected.append(item)
    return protected


def placeholders_of_removed_objects(
    local: Iterable[FileEntry],
    listing: Iterable[FileEntry],
    manifest: Iterable[FileEntry] | None,
) -> list[FileEntry]:
    """The manifest entries of the placeholders whose object is gone, sorted by path.

    Such a placeholder is an empty local file that the listing does not
    have, whose manifest entry says its object was not empty.
    """
    listed_paths = {entry.path for entry in listing}
    manifest_by_path = {entry.path: entry for entry in manifest or ()}
    return sorted(
        (
            manifest_by_path[entry.path]
            for entry in local
            if entry.path not in listed_paths
            and entry.path in manifest_by_path
            and is_placeholder(entry, manifest_by_path[entry.path])
        ),
        key=lambda entry: entry.path,
    )


def delete_removed_placeholders(
    plan: Iterable[PlanItem],
    local: Iterable[FileEntry],
    listing: Iterable[FileEntry],
    manifest: Iterable[FileEntry] | None,
) -> list[PlanItem]:
    """The plan of a sync from the bucket, deleting the placeholders whose object is gone.

    The folder stands for the bucket, and such a placeholder holds no
    data, so it is deleted whether or not the plan deletes extraneous
    files.
    """
    paths = {entry.path for entry in placeholders_of_removed_objects(local, listing, manifest)}
    return [
        replace(item, action="delete", reason="placeholder") if item.path in paths else item
        for item in plan
    ]


def manifest_after_push(
    listing: Iterable[FileEntry],
    uploaded: Iterable[FileEntry],
    deleted: Iterable[str],
    *,
    local: Iterable[FileEntry],
    manifest: Iterable[FileEntry] | None,
) -> list[FileEntry]:
    """The manifest after a sync to the bucket, sorted by path.

    An uploaded file gets the local size and checksum and an unknown time,
    since the time of the new object is only known from a new listing. A
    deleted object loses its entry. A placeholder whose object is gone
    keeps its entry from the manifest before the sync, so that the next
    sync to the bucket still protects it. local is the local folder at the
    sync, and manifest the manifest before it.
    """
    listing = list(listing)
    entries = {entry.path: entry for entry in listing}
    for entry in placeholders_of_removed_objects(local, listing, manifest):
        entries[entry.path] = entry
    for entry in uploaded:
        entries[entry.path] = FileEntry(entry.path, entry.bytes, None, entry.hash)
    for path in deleted:
        entries.pop(path, None)
    return sorted(entries.values(), key=lambda entry: entry.path)


def virtual_bucket_refusal(info: BucketInfo | None, bucket: str, prefix: str) -> Refusal | None:
    """Why a sync of a virtual bucket must stop before changing anything, or None.

    The sync must be of the bucket and the prefix that the folder stands for.
    """
    if info is None:
        return None
    prefix = normalize_prefix(prefix)
    if info.bucket == bucket and info.prefix == prefix:
        return None
    return Refusal(
        "OtherBucket",
        f"The folder is a virtual bucket of {describe(info.bucket, info.prefix)}, "
        f"not of {describe(bucket, prefix)}.",
    )


def describe(bucket: str, prefix: str) -> str:
    return f"bucket {bucket!r}" + (f", folder {prefix!r}" if prefix else "")


def format_time(time: datetime | None) -> str | None:
    """A time as written in the .ebrains files, such as "2024-01-02T03:04:05Z"."""
    if time is None:
        return None
    return time.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_atomically(path: Path, text: str) -> None:
    """Write text to a file so that an interrupted write leaves the old file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as file:
            file.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
