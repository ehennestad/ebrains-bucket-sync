"""Decide what to copy and delete to make a target match a source.

The planner only compares: it reads no file and sends no request. It is
the reference implementation of the rules in spec/README.md, which the
MATLAB toolbox implements too, and spec/fixtures holds the cases both
must pass.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta

from .model import COMPARISONS, FileEntry, PlanItem

TIME_TOLERANCE = timedelta(seconds=2)
"""Difference in modification time that still counts as the same time.

It covers the time resolution of FAT file systems and the fraction of a
second the listing times lose.
"""


def plan_sync(
    source: Iterable[FileEntry],
    target: Iterable[FileEntry],
    *,
    comparison: str = "SizeAndTime",
    delete: bool = False,
    time_tolerance: timedelta = TIME_TOLERANCE,
    ignore_case: bool = False,
) -> list[PlanItem]:
    """One PlanItem per path found on either side, sorted by path.

    Args:
        source: Files to make the target match.
        target: Files the target has.
        comparison: How a file on both sides is judged changed:
            "SizeAndTime": the sizes differ, or the source was changed
                after the target.
            "Size": the sizes differ.
            "Checksum": the sizes or the checksums differ. Where a checksum
                is unknown on either side, the file is judged as for
                "SizeAndTime".
        delete: Whether to delete the target files that the source does not
            have. Otherwise they are kept, with action "none".
        time_tolerance: Difference in modification time that still counts
            as the same time.
        ignore_case: Whether paths that differ only in case name the same
            file, as on a file system that ignores case. A file matched
            this way is planned under its source path.
    """
    if comparison not in COMPARISONS:
        raise ValueError(f"comparison must be one of {COMPARISONS}, not {comparison!r}.")

    source = list(source)
    target = list(target)
    key = match_key(ignore_case)
    target_by_key = {key(entry.path): entry for entry in target}

    items = []
    for entry in source:
        counterpart = target_by_key.get(key(entry.path))
        if counterpart is None:
            reason = "new"
        else:
            reason = change_reason(entry, counterpart, comparison, time_tolerance)
        action = "none" if reason == "unchanged" else "copy"
        items.append(PlanItem(entry.path, action, reason, entry.bytes))

    source_keys = {key(entry.path) for entry in source}
    extraneous_action = "delete" if delete else "none"
    for entry in target:
        if key(entry.path) not in source_keys:
            items.append(PlanItem(entry.path, extraneous_action, "extraneous", entry.bytes))

    items.sort(key=lambda item: item.path)
    return items


def match_key(ignore_case: bool) -> Callable[[str], str]:
    """What the paths of the two sides of a sync are matched by.

    Where case is ignored, paths are matched in lowercase, as the MATLAB
    toolbox matches them.
    """
    return str.lower if ignore_case else str


def case_colliding_deletions(
    plan: Iterable[PlanItem], local_paths: Iterable[str]
) -> dict[str, str]:
    """The deletions of a sync to the bucket of an object named like a local file in another case.

    Returns the path of the local file by the path of each such plan row.
    An upload matches names exactly, since the bucket tells names apart,
    so such an object is extraneous to the plan. On a file system that
    ignores case, the local folder cannot hold the object next to the
    file, and a sync from the bucket keeps only one of the two, so the
    object may be the only copy of its data. Whether the folder ignores
    case is for the caller to decide (see local.is_case_insensitive).
    """
    local_by_key = {path.lower(): path for path in local_paths}
    return {
        item.path: local_by_key[item.path.lower()]
        for item in plan
        if item.action == "delete" and item.path.lower() in local_by_key
    }


def change_reason(
    source: FileEntry, target: FileEntry, comparison: str, time_tolerance: timedelta
) -> str:
    """Reason to copy a file the target has, or "unchanged".

    The first reason that applies is given: a size difference before a
    checksum or time difference.
    """
    if source.bytes != target.bytes:
        return "size"

    if comparison == "Checksum" and source.hash and target.hash:
        return "checksum" if source.hash != target.hash else "unchanged"

    if comparison == "Size":
        return "unchanged"

    # An unknown time on either side leaves the file as it is.
    if source.modified_time is None or target.modified_time is None:
        return "unchanged"
    if source.modified_time - target.modified_time > time_tolerance:
        return "newer"
    return "unchanged"


@dataclass(frozen=True)
class Refusal:
    """Why the planned deletions look like a mistake."""

    code: str
    reason: str


def deletion_refusal(
    plan: Sequence[PlanItem], n_source_files: int, max_delete: float
) -> Refusal | None:
    """Why a run must stop before changing anything, or None.

    An empty source with deletions would empty the target. That is more
    often a mistyped folder or prefix than what is wanted, so it is
    refused. More deletions than max_delete are refused too.
    """
    deletions = [item for item in plan if item.action == "delete"]
    if not deletions:
        return None

    if n_source_files == 0:
        return Refusal(
            "EmptySource",
            f"The source has no files, so the sync would delete all {len(deletions)} "
            "file(s) of the target. Check the folder and the prefix.",
        )

    if len(deletions) > max_delete:
        return Refusal(
            "TooManyDeletions",
            f"The sync would delete {len(deletions)} file(s), more than max_delete "
            f"({max_delete:g}), for example {deletions[0].path!r}.",
        )

    return None
