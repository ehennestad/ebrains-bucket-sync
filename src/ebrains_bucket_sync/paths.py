"""Rules for object names and folder prefixes."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PureWindowsPath

from .model import RemoteObject


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


def normalize_prefix(prefix: str) -> str:
    """Folder name in the bucket: without a leading "/" and with a trailing one, or ""."""
    prefix = prefix.lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix += "/"
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
