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
import re
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .model import FileEntry, PlanItem
from .paths import is_safe_relative_path, normalize_prefix
from .plan import TIME_TOLERANCE, Refusal

METADATA_FOLDER = ".ebrains"
"""Folder at the root of a virtual bucket that describes it."""

FORMAT_VERSION = 1
"""Version of the layout of the .ebrains folder that this module writes."""

BUCKET_FILE = "bucket.json"
MANIFEST_FILE = "manifest.csv"
MANIFEST_FIELDS = ("path", "bytes", "modified_time", "hash")

TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
"""Form of the times in the .ebrains files, such as "2024-01-02T03:04:05Z"."""

_TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_WHOLE_NUMBER_PATTERN = re.compile(r"[0-9]+")
_MD5_PATTERN = re.compile(r"[0-9a-f]{32}")

STATES = ("online_only", "downloaded", "differs", "local_only")
"""States of the files of a virtual bucket. See file_states."""


class VirtualBucketError(Exception):
    """A virtual bucket that this module cannot use."""


class ManifestError(VirtualBucketError):
    """A manifest.csv that does not follow the layout of spec/README.md.

    Attributes:
        line_number: The line of the text where the problem is, counted
            from 1. A row that spans lines is at the line where it starts.
        problem: What is wrong with the line.
        path: The file, or None where only the text was read.
    """

    def __init__(self, line_number: int, problem: str, path: Path | None = None) -> None:
        self.line_number = line_number
        self.problem = problem
        self.path = path
        where = f"{path}, line {line_number}" if path else f"Line {line_number} of a manifest"
        super().__init__(
            f"{where}: {problem}. The virtual bucket cannot be used until the line is corrected."
        )


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

    Such paths are left out of both sides of every sync. Case is ignored,
    since on a file system that ignores it an object named ".EBRAINS/..."
    would be written into the .ebrains folder.
    """
    folded = path.casefold()
    return folded == METADATA_FOLDER or folded.startswith(METADATA_FOLDER + "/")


def read_bucket_info(root: Path) -> BucketInfo | None:
    """The bucket.json of a virtual bucket, or None if the folder has none.

    Raises VirtualBucketError when the file does not follow
    virtual-bucket.schema.json, or is of a newer format.
    """
    path = Path(root) / METADATA_FOLDER / BUCKET_FILE
    if not path.is_file():
        return None

    def invalid(problem: str) -> VirtualBucketError:
        return VirtualBucketError(
            f"{path} cannot be read: {problem}. "
            f"The virtual bucket cannot be used until the file is corrected."
        )

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise invalid(f"it is not JSON text in UTF-8 ({error})") from error
    if not isinstance(data, dict):
        raise invalid("it does not hold a JSON object")

    version = data.get("format_version")
    # bool is a subclass of int, but true is no version
    if type(version) is not int or version < 1:
        raise invalid(f"format_version {version!r} is not a whole number from 1")
    if version > FORMAT_VERSION:
        raise VirtualBucketError(
            f"{path} has format_version {version!r}, which this version of "
            f"ebrains-bucket-sync does not know. Update the package to use this folder."
        )
    bucket = data.get("bucket")
    if not isinstance(bucket, str) or not bucket:
        raise invalid(f"bucket {bucket!r} is not the name of a bucket")
    prefix = data.get("prefix")
    if not isinstance(prefix, str) or not is_normalized_prefix(prefix):
        raise invalid(
            f'prefix {prefix!r} is neither "" nor a folder name with a trailing "/" '
            f"that stays below the bucket"
        )
    if "listed_at" not in data:
        raise invalid("it has no listed_at")
    listed_at = data["listed_at"]
    if listed_at is not None:
        listed_at = parse_time(listed_at) if isinstance(listed_at, str) else None
        if listed_at is None:
            raise invalid(f"listed_at {data['listed_at']!r} is neither null nor a time in UTC")
    return BucketInfo(bucket, prefix, listed_at)


def is_normalized_prefix(prefix: str) -> bool:
    """Whether a prefix is "" or a safe folder name with a trailing "/", as bucket.json holds it."""
    return prefix == "" or (prefix.endswith("/") and is_safe_relative_path(prefix[:-1]))


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
    """The manifest of a virtual bucket, or None if it was never listed.

    Raises ManifestError when the file does not follow the layout of a
    manifest, and VirtualBucketError when it is not UTF-8 text.
    """
    path = Path(root) / METADATA_FOLDER / MANIFEST_FILE
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        raise VirtualBucketError(
            f"{path} is not UTF-8 text. The virtual bucket cannot be used until it is corrected."
        ) from error
    try:
        return parse_manifest(text)
    except ManifestError as error:
        raise ManifestError(error.line_number, error.problem, path) from error


def write_manifest(root: Path, entries: Iterable[FileEntry]) -> None:
    """Write the manifest of a virtual bucket, creating .ebrains if needed."""
    write_atomically(Path(root) / METADATA_FOLDER / MANIFEST_FILE, format_manifest(entries))


def parse_manifest(text: str) -> list[FileEntry]:
    """The file entries of the text of a manifest.csv.

    Raises ManifestError at the first line that does not follow the layout
    of spec/README.md: a row is refused rather than skipped, since a
    missing entry could let a placeholder replace its object.
    """
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    entries = []
    paths = set()
    line_number = 1
    try:
        header = next(reader, None)
        if tuple(header or ()) != MANIFEST_FIELDS:
            raise ManifestError(
                1,
                f"the header is {','.join(header or ())!r}, not {','.join(MANIFEST_FIELDS)!r}",
            )
        line_number = reader.line_num + 1
        for row in reader:
            entry = _manifest_entry(row, line_number)
            if entry.path in paths:
                raise ManifestError(line_number, f"the path {entry.path!r} is on an earlier line")
            paths.add(entry.path)
            entries.append(entry)
            line_number = reader.line_num + 1
    except csv.Error as error:
        raise ManifestError(line_number, f"it is not a row of CSV text ({error})") from error
    return entries


def _manifest_entry(row: list[str], line_number: int) -> FileEntry:
    """The file entry of a row of a manifest, or a ManifestError."""
    if len(row) != len(MANIFEST_FIELDS):
        raise ManifestError(line_number, f"it has {len(row)} fields, not {len(MANIFEST_FIELDS)}")
    path, size, time, checksum = row
    if not is_safe_relative_path(path):
        raise ManifestError(line_number, f"the path {path!r} does not stay below the folder")
    if not _WHOLE_NUMBER_PATTERN.fullmatch(size):
        raise ManifestError(line_number, f"bytes {size!r} is not a whole number")
    modified_time = None
    if time:
        modified_time = parse_time(time)
        if modified_time is None:
            raise ManifestError(
                line_number, f"modified_time {time!r} is neither empty nor a time in UTC"
            )
    if checksum and not _MD5_PATTERN.fullmatch(checksum):
        raise ManifestError(
            line_number, f"hash {checksum!r} is neither empty nor a lowercase MD5 checksum"
        )
    return FileEntry(path, int(size), modified_time, checksum)


def parse_time(text: str) -> datetime | None:
    """A time as written in the .ebrains files, or None for text in any other form."""
    if not _TIME_PATTERN.fullmatch(text):
        return None
    try:
        return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


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
    local: Iterable[FileEntry],
    manifest: Iterable[FileEntry] | None,
    *,
    local_hash: Callable[[FileEntry], str] | None = None,
) -> list[tuple[str, str]]:
    """(path, state) of each path of a virtual bucket, sorted by path.

    Without a manifest, an empty file is "online_only" and any other file
    "downloaded". With one, a path only the manifest has and a placeholder
    are "online_only", a path only the local folder has is "local_only",
    and any other file is "downloaded" or "differs" by is_downloaded.

    local_hash gives the MD5 checksum of a local file, or "" where it is
    unknown. It is called only for the files whose checksum decides, and
    by default gives the hash of the entry.
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
            is_same = is_downloaded(local_entry, manifest_entry, local_hash)
            state = "downloaded" if is_same else "differs"
        states.append((path, state))
    return states


def is_downloaded(
    local: FileEntry,
    manifest_entry: FileEntry,
    local_hash: Callable[[FileEntry], str] | None = None,
) -> bool:
    """Whether a local file holds what its manifest entry says its object holds.

    A download gets the time of its object, so a file of the same size and
    time is taken to be the download. Where the times differ, or one is
    unknown, the MD5 checksums decide where both are known: a pushed file
    has an unknown time in the manifest until the next listing, and then
    the time of its upload, which is later than the local file. Without
    both checksums, differing times mean the file was changed, and an
    unknown time leaves it as downloaded, as in a plan.
    """
    if local.bytes != manifest_entry.bytes:
        return False
    times_known = local.modified_time is not None and manifest_entry.modified_time is not None
    if times_known and abs(local.modified_time - manifest_entry.modified_time) <= TIME_TOLERANCE:
        return True
    if manifest_entry.hash:
        checksum = local_hash(local) if local_hash is not None else local.hash
        if checksum:
            return checksum == manifest_entry.hash
    return not times_known


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

    An uploaded file gets the local size, the MD5 checksum of what was
    uploaded and an unknown time, since the time of the new object is only
    known from a new listing. uploaded holds the entries of the uploaded
    files with their checksums. A
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
    return time.astimezone(timezone.utc).strftime(TIME_FORMAT)


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
