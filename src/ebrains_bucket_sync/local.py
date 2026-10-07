"""List and checksum the files of a local folder."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

from .model import FileEntry

MD5_BLOCK_SIZE = 16 * 1024 * 1024


def list_local_files(root: str | os.PathLike[str]) -> list[FileEntry]:
    """One FileEntry per file in root and its subfolders, sorted by path.

    Paths are relative to root and use "/" as separator on every platform,
    as object names do. Folders are not listed: an object store has no
    empty folders to sync them to. Checksums are left unknown, since
    computing them means reading every file. Symbolic links to folders are
    not followed.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"{str(root)!r} is not a folder.")

    entries = []
    for folder, _subfolders, names in os.walk(root):
        for name in names:
            full_path = Path(folder) / name
            try:
                info = full_path.stat()
            except OSError:
                continue  # vanished since the listing
            if not stat.S_ISREG(info.st_mode):
                continue
            modified_time = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
            entries.append(
                FileEntry(full_path.relative_to(root).as_posix(), info.st_size, modified_time)
            )

    entries.sort(key=lambda entry: entry.path)
    return entries


def compute_md5(path: str | os.PathLike[str]) -> str:
    """MD5 checksum of a file as lowercase hexadecimal text, read in blocks."""
    digest = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as file:
        while True:
            block = file.read(MD5_BLOCK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()
