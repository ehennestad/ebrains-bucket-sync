"""Data types shared by the planner, the engine and the command line."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

COMPARISONS = ("SizeAndTime", "Size", "Checksum")
"""How a file on both sides is judged changed. See plan.plan_sync."""


class SyncWarning(UserWarning):
    """A condition of a sync that is worth knowing but does not stop it."""


@dataclass(frozen=True)
class FileEntry:
    """A file on one side of a sync, as the planner compares it.

    Attributes:
        path: Path relative to the synced folder, with "/" separators.
        bytes: Size of the file.
        modified_time: Time of the last change, in UTC, or None where unknown.
        hash: Lowercase MD5 checksum, or "" where unknown.
    """

    path: str
    bytes: int
    modified_time: datetime | None = None
    hash: str = ""

    def __post_init__(self) -> None:
        # Times without a zone cannot be compared with times that have one,
        # so every time is held in UTC; a naive time is taken to be UTC.
        if self.modified_time is not None:
            if self.modified_time.tzinfo is None:
                in_utc = self.modified_time.replace(tzinfo=timezone.utc)
            else:
                in_utc = self.modified_time.astimezone(timezone.utc)
            object.__setattr__(self, "modified_time", in_utc)
        object.__setattr__(self, "hash", (self.hash or "").lower())


@dataclass(frozen=True)
class RemoteObject:
    """An object of a bucket listing, as the Data Proxy reports it."""

    name: str
    bytes: int
    last_modified: str | None = None
    hash: str | None = None
    content_type: str | None = None


class BucketStorage(Protocol):
    """What the engine needs from a bucket. storage.EbrainsDriveStorage is the real one."""

    def list_objects(self, bucket: str, prefix: str) -> list[RemoteObject]:
        """Every object whose name starts with prefix."""
        ...

    def upload(self, bucket: str, object_name: str, local_path: Path) -> None:
        """Store a local file as an object, replacing one of that name."""
        ...

    def delete_object(self, bucket: str, object_name: str) -> None: ...


@dataclass(frozen=True)
class PlanItem:
    """One row of a sync plan.

    Attributes:
        path: Path relative to the synced folder.
        action: "copy", "delete" or "none".
        reason: For a path of the source: "new", "size", "newer", "checksum"
            or "unchanged". For a path only the target has: "extraneous".
        bytes: Size of the source file, or of the target file for a path
            only the target has.
    """

    path: str
    action: str
    reason: str
    bytes: int


@dataclass
class SyncOptions:
    """Options of a sync. The defaults are those of the MATLAB toolbox.

    Attributes:
        prefix: Folder of the bucket to sync, such as "results/". "" is the
            root of the bucket. Objects outside the folder are never changed.
        delete: Delete the files of the target that the source does not
            have, which makes the target an exact mirror of the source.
        comparison: How a file on both sides is judged changed, one of
            COMPARISONS. See plan.plan_sync.
        exclude: Wildcard patterns of paths to leave out. Excluded files are
            neither transferred nor deleted. See exclude.exclude_files.
        dry_run: Only plan, and change nothing.
        max_delete: Most files the sync may delete. If the plan deletes
            more, the sync stops before it changes anything.
        workers: Number of files transferred at the same time.
    """

    prefix: str = ""
    delete: bool = False
    comparison: str = "SizeAndTime"
    exclude: tuple[str, ...] = ()
    dry_run: bool = False
    max_delete: float = math.inf
    workers: int = 4

    def __post_init__(self) -> None:
        if self.comparison not in COMPARISONS:
            raise ValueError(f"comparison must be one of {COMPARISONS}, not {self.comparison!r}.")
        if self.max_delete < 0:
            raise ValueError("max_delete must not be negative.")
        if self.workers < 1:
            raise ValueError("workers must be at least 1.")
        self.exclude = tuple(self.exclude)


@dataclass
class ActionResult:
    """One row of what a sync returns: a planned action and its outcome.

    Attributes:
        path: Path relative to the synced folder.
        action: "upload", "delete" or "none".
        reason: Why, as for PlanItem.reason.
        bytes: Size of the file.
        status: "done", "failed", "skipped", "planned" (dry run) or "" for
            no action.
        message: Why an action failed or was skipped.
    """

    path: str
    action: str
    reason: str
    bytes: int
    status: str = ""
    message: str = ""
