"""Shared test doubles: an in-memory bucket and helpers to build entries."""

from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ebrains_sync.model import FileEntry, RemoteObject


def md5_of(content: bytes) -> str:
    return hashlib.md5(content, usedforsecurity=False).hexdigest()


@dataclass
class FakeObject:
    content: bytes
    last_modified: str | None
    content_type: str | None
    hash: str | None


class FakeStorage:
    """A BucketStorage held in memory.

    An object put with the default last_modified counts as uploaded after
    any local file is changed, so a file of the same size on both sides is
    unchanged unless a test says otherwise.
    """

    UPLOADED_AFTER_LOCAL = "2999-01-01T00:00:00"
    UPLOADED_BEFORE_LOCAL = "2000-01-01T00:00:00"

    def __init__(self) -> None:
        self.buckets: dict[str, dict[str, FakeObject]] = defaultdict(dict)
        self.errors: dict[str, Exception] = {}
        self.upload_attempts: Counter[str] = Counter()
        self.uploaded: list[tuple[str, str]] = []
        self.deleted: list[tuple[str, str]] = []

    def put(
        self,
        bucket: str,
        name: str,
        content: bytes = b"",
        *,
        last_modified: str | None = UPLOADED_AFTER_LOCAL,
        content_type: str | None = "application/octet-stream",
        hash: str | None = "",
    ) -> None:
        checksum = md5_of(content) if hash == "" else hash
        self.buckets[bucket][name] = FakeObject(content, last_modified, content_type, checksum)

    def content(self, bucket: str, name: str) -> bytes:
        return self.buckets[bucket][name].content

    def list_objects(self, bucket: str, prefix: str) -> list[RemoteObject]:
        return [
            RemoteObject(name, len(obj.content), obj.last_modified, obj.hash, obj.content_type)
            for name, obj in sorted(self.buckets[bucket].items())
            if name.startswith(prefix)
        ]

    def upload(self, bucket: str, object_name: str, local_path: Path) -> None:
        self.upload_attempts[object_name] += 1
        if object_name in self.errors:
            raise self.errors[object_name]
        content = Path(local_path).read_bytes()
        self.buckets[bucket][object_name] = FakeObject(
            content, "2024-06-01T12:00:00", "application/octet-stream", md5_of(content)
        )
        self.uploaded.append((bucket, object_name))

    def delete_object(self, bucket: str, object_name: str) -> None:
        del self.buckets[bucket][object_name]
        self.deleted.append((bucket, object_name))


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    root = tmp_path / "local"
    root.mkdir()
    return root


def write(folder: Path, relative_path: str, content: bytes = b"") -> Path:
    path = folder / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def entry(path: str, size: int = 1, modified_time=None, hash: str = "") -> FileEntry:
    return FileEntry(path, size, modified_time, hash)
