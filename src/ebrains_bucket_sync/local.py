"""List and checksum the files of a local folder."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .model import FileEntry

MD5_BLOCK_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True)
class LocalListing:
    """What a listing of a local folder found.

    Attributes:
        files: One FileEntry per file, as list_local_files gives them.
        linked_folders: Subfolders that are symbolic links (or, on Windows,
            junctions), whose content is stored elsewhere and not listed.
        unreadable_folders: Folders whose content could not be read, so
            their files are missing from files. "" stands for the root.
    All paths are relative to the root, with "/" separators, and sorted.
    """

    files: list[FileEntry] = field(default_factory=list)
    linked_folders: list[str] = field(default_factory=list)
    unreadable_folders: list[str] = field(default_factory=list)


def list_local_files(root: str | os.PathLike[str]) -> list[FileEntry]:
    """One FileEntry per file in root and its subfolders, sorted by path.

    Paths are relative to root and use "/" as separator on every platform,
    as object names do. Folders are not listed: an object store has no
    empty folders to sync them to. Checksums are left unknown, since
    computing them means reading every file. Symbolic links to folders are
    not followed.
    """
    return scan_local_folder(root).files


def scan_local_folder(root: str | os.PathLike[str]) -> LocalListing:
    """The files of root and its subfolders, and the folders whose files are not among them.

    The files are those of list_local_files. A caller that deletes or
    replaces what the files lack must check the other two lists first:
    the files of a linked folder are stored outside root, and those of an
    unreadable folder are unknown.
    """
    root = Path(root)
    if not root.is_dir():
        raise NotADirectoryError(f"{str(root)!r} is not a folder.")

    def relative(path: str | os.PathLike[str]) -> str:
        relative_path = Path(path).relative_to(root).as_posix()
        return "" if relative_path == "." else relative_path

    unreadable_folders = []

    def on_error(error: OSError) -> None:
        unreadable_folders.append(relative(error.filename))

    entries = []
    linked_folders = []
    for folder, subfolders, names in os.walk(root, onerror=on_error):
        for name in subfolders:
            if is_link(Path(folder) / name):
                linked_folders.append(relative(Path(folder) / name))
        for name in names:
            entry = local_file_entry(root, relative(Path(folder) / name))
            if entry is not None:
                entries.append(entry)

    entries.sort(key=lambda entry: entry.path)
    return LocalListing(entries, sorted(linked_folders), sorted(unreadable_folders))


def local_file_entry(root: str | os.PathLike[str], relative_path: str) -> FileEntry | None:
    """The FileEntry of one file below root, as list_local_files gives it.

    None for a path that is not a regular file or cannot be read: a
    folder, a link to one, or a file that vanished since it was listed.
    """
    try:
        info = (Path(root) / relative_path).stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    modified_time = datetime.fromtimestamp(info.st_mtime, tz=timezone.utc)
    return FileEntry(relative_path, info.st_size, modified_time)


def is_case_insensitive(root: str | os.PathLike[str], local_paths: Sequence[str]) -> bool:
    """Whether the file system of a local folder ignores the case of names.

    The file system is probed with a listed file whose path has letters:
    it is looked up with their case changed, which a file system that
    ignores case finds and the listing does not have. Without such a file
    there is nothing to look up, and the default file systems of macOS and
    Windows are taken to ignore case and those of other platforms not to.
    local_paths are the paths of a listing of root, as list_local_files
    gives them.
    """
    probed = next((path for path in local_paths if path.upper() != path.lower()), None)
    if probed is None:
        return sys.platform in ("darwin", "win32")

    probe = probed.upper() if probed.upper() != probed else probed.lower()
    return probe not in set(local_paths) and (Path(root) / probe).is_file()


def is_link(path: Path) -> bool:
    """Whether a folder is a symbolic link, or a junction on Windows."""
    is_junction = getattr(os.path, "isjunction", None)  # Python 3.12 and later
    return path.is_symlink() or (is_junction is not None and is_junction(path))


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
