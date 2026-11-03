"""The registry of virtual buckets of the user.

The registry records the folders opened as virtual buckets, a label for
each bucket the user has named, and the root folder where new virtual
buckets are made. A browser that opens without a folder lists the virtual
buckets from it. These functions are the reference implementation of
spec/README.md#virtual-bucket-registry, which the MATLAB toolbox
implements too.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from .config import config_dir
from .virtual_bucket import (
    VirtualBucketError,
    format_time,
    is_normalized_prefix,
    parse_time,
    read_bucket_info,
    write_atomically,
)


def normalize_path(path: str | os.PathLike[str]) -> str:
    """A folder path as the registry records it.

    Absolute, with the separator of the system, without a trailing
    separator or "." and ".." segments. Links are not resolved, so a
    folder below a root that is a link is the same folder as the one a
    scan of the root finds. Two paths are the same folder when they are
    equal as text, so the methods of Registry put every path given to
    them through this.
    """
    return os.path.normpath(os.path.abspath(os.fspath(path)))


REGISTRY_FILE = "virtual-buckets.json"
"""Name of the registry file in the configuration folder."""

FORMAT_VERSION = 1
"""Version of the layout of the registry file that this module writes."""


class RegistryError(Exception):
    """The registry file cannot be read."""


@dataclass(frozen=True)
class FolderEntry:
    """A recorded folder: what it stood for when last recorded, and how the user keeps it.

    Attributes:
        path: Absolute path of the folder, as the operating system writes it.
        bucket, prefix: What the folder stood for when it was last recorded,
            in the form of bucket.json.
        pinned: Whether the user pinned the folder to the top of the list.
        last_opened: When the folder was last opened, in UTC, or None when
            it was recorded without being opened.
    """

    path: str
    bucket: str
    prefix: str
    pinned: bool = False
    last_opened: datetime | None = None


@dataclass
class Registry:
    """The content of the registry file.

    Attributes:
        root: The folder where new virtual buckets are made, or None.
        labels: The label of each bucket that has one, by bucket.
        folders: The recorded folders, by path.
    """

    root: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    folders: dict[str, FolderEntry] = field(default_factory=dict)

    def set_root(self, path: str | os.PathLike[str] | None) -> None:
        """Set the folder where new virtual buckets are made, or remove it with None.

        The folder is recorded as normalize_path writes it.
        """
        self.root = None if path is None else normalize_path(path)

    def label_of(self, bucket: str) -> str | None:
        return self.labels.get(bucket)

    def set_label(self, bucket: str, label: str) -> None:
        """Give a bucket a label, or remove its label with empty text.

        A bucket that is not a nonempty text is a ValueError, since the
        file could not be read back.
        """
        _check_entry_fields(bucket, "")
        if label:
            self.labels[bucket] = label
        else:
            self.labels.pop(bucket, None)

    def entry_of(self, path: str | os.PathLike[str]) -> FolderEntry | None:
        return self.folders.get(normalize_path(path))

    def record_open(
        self, path: str | os.PathLike[str], bucket: str, prefix: str, opened_at: datetime
    ) -> None:
        """Record that a folder was opened as a virtual bucket of bucket and prefix.

        The entry keeps its pinned flag; a folder without one is recorded
        unpinned. opened_at is an aware datetime, recorded in UTC to the
        second, as the file holds it. The path is recorded as normalize_path
        writes it. A bucket that is not a nonempty text, or a prefix that
        is not "" or a folder name with a trailing "/", is a ValueError,
        since the file could not be read back.
        """
        key = normalize_path(path)
        _check_entry_fields(bucket, prefix)
        if opened_at.tzinfo is None:
            raise ValueError("opened_at must be an aware datetime")
        opened_at = opened_at.astimezone(timezone.utc).replace(microsecond=0)
        entry = self.folders.get(key)
        pinned = entry.pinned if entry is not None else False
        self.folders[key] = FolderEntry(key, bucket, prefix, pinned, opened_at)

    def set_pinned(
        self,
        path: str | os.PathLike[str],
        pinned: bool,
        *,
        bucket: str | None = None,
        prefix: str | None = None,
    ) -> None:
        """Pin or unpin a folder.

        Pinning a folder that is not recorded records it without a time,
        which needs its bucket and prefix (checked as in record_open);
        unpinning one changes nothing.
        """
        key = normalize_path(path)
        entry = self.folders.get(key)
        if entry is None:
            if not pinned:
                return
            if bucket is None or prefix is None:
                raise ValueError(f"{key} is not recorded: give its bucket and prefix to pin it")
            _check_entry_fields(bucket, prefix)
            entry = FolderEntry(key, bucket, prefix)
        self.folders[key] = replace(entry, pinned=pinned)

    def remove_folder(self, path: str | os.PathLike[str]) -> None:
        """Forget a folder. The folder itself is left as it is."""
        self.folders.pop(normalize_path(path), None)


def _check_entry_fields(bucket: str, prefix: str) -> None:
    """Refuse a bucket or prefix that the registry file could not hold."""
    if not isinstance(bucket, str) or not bucket:
        raise ValueError(f"bucket {bucket!r} is not the name of a bucket")
    if not isinstance(prefix, str) or not is_normalized_prefix(prefix):
        raise ValueError(
            f'prefix {prefix!r} is neither "" nor a folder name with a trailing "/" '
            f"that stays below the bucket"
        )


def registry_path() -> Path:
    """Where the registry file is, in the configuration folder."""
    return config_dir() / REGISTRY_FILE


def read_registry(path: Path | None = None) -> Registry:
    """The registry in the file, or an empty registry when there is no file.

    Raises RegistryError when the file does not follow
    bucket-registry.schema.json, is of a newer format, or cannot be read
    for another reason than being missing.
    """
    path = path or registry_path()
    try:
        text = path.read_bytes()
    except FileNotFoundError:
        return Registry()
    except OSError as error:
        raise RegistryError(f"{path} cannot be read: {error}") from error
    return parse_registry(text, name=str(path))


def write_registry(registry: Registry, path: Path | None = None) -> None:
    """Write the registry so that an interrupted write leaves the old file.

    The folder of the file is created readable to the user alone, as the
    token store creates the same folder, whichever comes first.
    """
    path = path or registry_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_atomically(path, format_registry(registry))


def parse_registry(text: str | bytes, *, name: str = REGISTRY_FILE) -> Registry:
    """The registry in the text of a registry file. name is used in errors."""

    def invalid(problem: str) -> RegistryError:
        return RegistryError(
            f"{name} cannot be read: {problem}. "
            f"The registry cannot be used until the file is corrected or deleted."
        )

    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        data = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise invalid(f"it is not JSON text in UTF-8 ({error})") from error
    if not isinstance(data, dict):
        raise invalid("it does not hold a JSON object")

    version = data.get("format_version")
    # bool is a subclass of int, but true is no version
    if type(version) is not int or version < 1:
        raise invalid(f"format_version {version!r} is not a whole number from 1")
    if version > FORMAT_VERSION:
        raise RegistryError(
            f"{name} has format_version {version!r}, which this version of "
            f"ebrains-bucket-sync does not know. Update the package to use the registry."
        )
    for key in ("root", "buckets", "folders"):
        if key not in data:
            raise invalid(f"it has no {key}")

    root = data["root"]
    if root is not None and (not isinstance(root, str) or not root):
        raise invalid(f"root {root!r} is neither null nor a path")

    if not isinstance(data["buckets"], list):
        raise invalid("buckets is not a list")
    labels: dict[str, str] = {}
    for item in data["buckets"]:
        if not isinstance(item, dict):
            raise invalid("an entry of buckets is not an object")
        bucket = item.get("bucket")
        if not isinstance(bucket, str) or not bucket:
            raise invalid(f"bucket {bucket!r} in buckets is not the name of a bucket")
        if bucket in labels:
            raise invalid(f"bucket {bucket!r} appears twice in buckets")
        label = item.get("label")
        if not isinstance(label, str) or not label:
            raise invalid(
                f"label {label!r} of bucket {bucket!r} is not text of at least one character"
            )
        labels[bucket] = label

    if not isinstance(data["folders"], list):
        raise invalid("folders is not a list")
    folders: dict[str, FolderEntry] = {}
    for item in data["folders"]:
        if not isinstance(item, dict):
            raise invalid("an entry of folders is not an object")
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise invalid(f"path {path!r} in folders is not a path")
        if path in folders:
            raise invalid(f"path {path!r} appears twice in folders")
        bucket = item.get("bucket")
        if not isinstance(bucket, str) or not bucket:
            raise invalid(f"bucket {bucket!r} of folder {path!r} is not the name of a bucket")
        prefix = item.get("prefix")
        if not isinstance(prefix, str) or not is_normalized_prefix(prefix):
            raise invalid(
                f'prefix {prefix!r} of folder {path!r} is neither "" nor a folder name '
                f'with a trailing "/" that stays below the bucket'
            )
        pinned = item.get("pinned")
        if not isinstance(pinned, bool):
            raise invalid(f"pinned {pinned!r} of folder {path!r} is not true or false")
        if "last_opened" not in item:
            raise invalid(f"folder {path!r} has no last_opened")
        last_opened = item["last_opened"]
        if last_opened is not None:
            last_opened = parse_time(last_opened) if isinstance(last_opened, str) else None
            if last_opened is None:
                raise invalid(
                    f"last_opened {item['last_opened']!r} of folder {path!r} is neither null "
                    f"nor a time in UTC"
                )
        folders[path] = FolderEntry(path, bucket, prefix, pinned, last_opened)

    return Registry(root, labels, folders)


def format_registry(registry: Registry) -> str:
    """The text of the registry file, with buckets and folders sorted by name.

    A root of "" is a ValueError: the file holds a path or null, and the
    reader refuses "".
    """
    if registry.root == "":
        raise ValueError('root is "": use None for no root')
    data = {
        "format_version": FORMAT_VERSION,
        "root": registry.root,
        "buckets": [
            {"bucket": bucket, "label": label} for bucket, label in sorted(registry.labels.items())
        ],
        "folders": [
            {
                "path": entry.path,
                "bucket": entry.bucket,
                "prefix": entry.prefix,
                "pinned": entry.pinned,
                "last_opened": format_time(entry.last_opened),
            }
            for entry in sorted(registry.folders.values(), key=lambda entry: entry.path)
        ],
    }
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


@dataclass(frozen=True)
class ListedBucket:
    """A row of the list of virtual buckets.

    Attributes:
        path: Absolute path of the folder.
        bucket, prefix: What the folder stands for: from its bucket.json
            when that can be read, else from its entry in the registry.
        label: The label of the bucket, or None.
        pinned, last_opened: From the entry of the folder; unpinned and None
            for a folder below the root that is not recorded.
        exists: Whether the bucket.json of the folder can be read.
    """

    path: str
    bucket: str
    prefix: str
    label: str | None
    pinned: bool
    last_opened: datetime | None
    exists: bool


def list_virtual_buckets(
    registry: Registry,
    root_children: Iterable[str],
    info_of: Callable[[str], tuple[str, str] | None],
) -> list[ListedBucket]:
    """The rows of the list of virtual buckets, in the order of the spec.

    Args:
        registry: The registry of the user.
        root_children: Paths of the folders directly below the root.
        info_of: Gives the bucket and prefix of the bucket.json of a path,
            or None when the folder is not a virtual bucket whose
            bucket.json can be read. bucket_info_of reads the file.
    """
    rows: dict[str, ListedBucket] = {}

    def row(path: str, bucket: str, prefix: str, exists: bool) -> ListedBucket:
        entry = registry.folders.get(path)
        return ListedBucket(
            path,
            bucket,
            prefix,
            registry.label_of(bucket),
            entry.pinned if entry is not None else False,
            entry.last_opened if entry is not None else None,
            exists,
        )

    for path in root_children:
        info = info_of(path)
        if info is not None:
            rows[path] = row(path, info[0], info[1], True)
    for path, entry in registry.folders.items():
        if path in rows:
            continue
        info = info_of(path)
        if info is None:
            rows[path] = row(path, entry.bucket, entry.prefix, False)
        else:
            rows[path] = row(path, info[0], info[1], True)

    def order(item: ListedBucket) -> tuple[bool, bool, float, str]:
        latest_first = -item.last_opened.timestamp() if item.last_opened is not None else 0.0
        return (not item.pinned, item.last_opened is None, latest_first, item.path)

    return sorted(rows.values(), key=order)


def root_children(root: str | os.PathLike[str] | None) -> list[str]:
    """Paths of the folders directly below the root, sorted by name.

    The root is normalized first (see normalize_path), so that the
    children are written as the registry records folders. No root, or one
    that cannot be read, gives no folders.
    """
    if not root:
        return []
    root = normalize_path(root)
    try:
        with os.scandir(root) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
    except OSError:
        return []
    return [entry.path for entry in children if entry.is_dir()]


def bucket_info_of(path: str | os.PathLike[str]) -> tuple[str, str] | None:
    """The bucket and prefix of the bucket.json of a folder, or None when it cannot be read."""
    try:
        info = read_bucket_info(Path(path))
    except (VirtualBucketError, OSError):
        return None
    return None if info is None else (info.bucket, info.prefix)
