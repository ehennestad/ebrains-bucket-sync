"""Rules for object names and folder prefixes."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PureWindowsPath

from .model import RemoteObject

DOWNLOAD_PART_SUFFIX = ".sync-part"
"""Suffix of the temporary file an object is received in, next to its target.
The MATLAB toolbox receives its downloads under the same suffix, so that each
tool recognises the leftovers of the other in a folder both sync."""

MULTIPART_MANIFEST_SUFFIX = ".multipart_manifest.json"
"""ebrains_drive keeps the progress of a multipart upload of a file in a
file of this name next to it, and resumes from it."""

TRANSFER_LEFTOVER_SUFFIXES = (DOWNLOAD_PART_SUFFIX, MULTIPART_MANIFEST_SUFFIX)
"""Suffixes of the files a transfer keeps next to its target. See is_transfer_leftover."""


class InvalidPrefixError(ValueError):
    """A prefix that does not name a folder of a bucket."""


def is_safe_relative_path(path: str) -> bool:
    """Whether an object name can be used as a path below a folder.

    False for a name that could leave the folder or point elsewhere: a "."
    or ".." segment, an empty segment (a leading, trailing or doubled "/"),
    a backslash, which Windows reads as a separator, or a Windows drive such
    as "C:" as the first segment, which makes the name absolute there.
    Object names come from the bucket, which other members of a collab can
    write to, so a sync must not trust them to be plain paths.
    """
    segments = path.split("/")
    if any(segment in ("", ".", "..") for segment in segments) or "\\" in path:
        return False
    # Joining a folder with a name that has a drive gives a path on that
    # drive, outside the folder, so the name must have no anchor on Windows
    return not PureWindowsPath(path).anchor


def is_transfer_leftover(path: str) -> bool:
    """Whether a path is the file a transfer keeps next to its target.

    Such a file ends in ".sync-part", the file a download is received in,
    or ".multipart_manifest.json", the manifest ebrains_drive keeps next to
    a file during a multipart upload. Neither holds data of its own, and a
    process killed during the transfer leaves it behind, so these paths are
    left out of both sides of every sync, before any exclude pattern, and
    of the files the virtual bucket browser shows. A file given such a
    name on purpose is left out as well. The suffixes are matched as
    written, in lowercase, on the path itself: "a.sync-part/b.txt" is a
    file below a folder, not a leftover. The MATLAB toolbox leaves out the
    same names (ebrains.bucket.sync.internal.isTransferLeftover).
    """
    return path.endswith(TRANSFER_LEFTOVER_SUFFIXES)


def normalize_prefix(prefix: str) -> str:
    """Folder name in the bucket: without a leading "/" and with a trailing one, or "".

    Raises InvalidPrefixError for a prefix that is not a plain relative
    path (see is_safe_relative_path): the objects of such a folder could
    not be placed below a local folder, and the prefix could not be
    recorded in the bucket.json of a virtual bucket, whose schema forbids
    it. The check is here so that every function that takes a prefix
    refuses the same names.
    """
    given = prefix
    prefix = prefix.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    # The folder name is checked without its trailing "/", which would
    # count as an empty last segment
    if prefix and not is_safe_relative_path(prefix[:-1]):
        raise InvalidPrefixError(
            f"The prefix {given!r} does not name a folder of the bucket: a folder name has "
            'no empty, "." or ".." segment, no backslash, and no Windows drive such as "C:" '
            'as its first segment. Give the folder as a name such as "results" or "results/sub".'
        )
    return prefix


def folder_object_flags(objects: Sequence[RemoteObject]) -> list[bool]:
    """Which objects of a listing stand for folders rather than hold files.

    An object store has no folders, so a folder shows up as a placeholder
    object in one of three ways: its name ends with "/", its content type
    is a directory type, or other objects are named below it. Buckets
    migrated from the old object storage mark a folder with an empty object
    that has neither a trailing "/" nor a directory content type.
    """
    parent_paths = set()
    for obj in objects:
        # A trailing "/" names the object itself as a folder, not a parent
        name = obj.name[:-1] if obj.name.endswith("/") else obj.name
        for index, char in enumerate(name):
            if char == "/":
                parent_paths.add(name[:index])

    return [
        obj.name.endswith("/")
        or obj.name in parent_paths
        or bool(obj.content_type and obj.content_type.startswith("application/directory"))
        for obj in objects
    ]
