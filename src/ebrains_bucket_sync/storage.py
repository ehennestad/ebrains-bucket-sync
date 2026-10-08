"""Access to Data Proxy buckets through the ebrains_drive package.

This is the only module that imports ebrains_drive. The engine talks to a
model.BucketStorage, so tests use an in-memory one, and a change in the
library's interface stays contained here.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar

import requests
from ebrains_drive import BucketApiClient
from ebrains_drive.exceptions import ClientHttpError, TokenExpired, Unauthorized
from ebrains_drive.utils import EBRAINS_DRIVE_MULTIPART_THRESHOLD

from .model import RemoteObject

_T = TypeVar("_T")

MULTIPART_MANIFEST_SUFFIX = ".multipart_manifest.json"
"""ebrains_drive keeps the progress of a multipart upload of a file in a
file of this name next to it, and resumes from it."""


class TokenSource(Protocol):
    """Where the storage gets an access token, and a fresh one when asked."""

    def access_token(self, *, force_refresh: bool = False) -> str: ...


class StaticToken:
    """A TokenSource that holds one token, for scripts and tests."""

    def __init__(self, token: str) -> None:
        self._token = token

    def access_token(self, *, force_refresh: bool = False) -> str:
        return self._token


def is_auth_error(error: BaseException) -> bool:
    """Whether a request was refused for its token."""
    if isinstance(error, (Unauthorized, TokenExpired)):
        return True
    return isinstance(error, ClientHttpError) and getattr(error, "code", None) == 401


def is_transient_error(error: BaseException) -> bool:
    """Whether a failed request may well succeed when sent again."""
    if isinstance(error, (requests.RequestException, ConnectionError, TimeoutError)):
        return True
    code = getattr(error, "code", None)
    return isinstance(error, ClientHttpError) and isinstance(code, int) and code >= 500


def discard_stale_manifest(local_path: Path) -> None:
    """Delete the multipart manifest of a file that changed after it was written.

    ebrains_drive resumes an upload from the manifest without checking the
    file, so a resume after a change would join the parts of the old file
    to the rest of the new one. The manifest is rewritten after every part,
    so a file changed after it was last written must be uploaded anew.

    The change time (st_ctime) is used where it is the time of the last
    change: every write or replacement of the file sets it, and copy tools
    that keep the modification time of the original cannot set it back. On
    Windows st_ctime is the creation time, so the modification time is used.
    """
    manifest = Path(f"{local_path}{MULTIPART_MANIFEST_SUFFIX}")
    try:
        manifest_written = manifest.stat().st_mtime_ns
    except FileNotFoundError:
        return
    info = local_path.stat()
    file_changed = info.st_mtime_ns if os.name == "nt" else info.st_ctime_ns
    if file_changed > manifest_written:
        manifest.unlink(missing_ok=True)


class EbrainsDriveStorage:
    """BucketStorage over the Data Proxy, through ebrains_drive.

    A request refused for its token is sent once more with a fresh token
    from the TokenSource, so a token that expires during a long sync is
    renewed without the sync noticing.

    Args:
        tokens: Where the access token comes from.
        timeout: Seconds to wait for each request.
        client_factory: Makes the ebrains_drive client, for tests.
    """

    def __init__(
        self,
        tokens: TokenSource,
        *,
        timeout: float = 120.0,
        client_factory: Callable[..., Any] = BucketApiClient,
    ) -> None:
        self._tokens = tokens
        self._timeout = timeout
        self._client_factory = client_factory
        self._client: Any = None
        self._buckets: dict[str, Any] = {}

    def list_objects(self, bucket: str, prefix: str) -> list[RemoteObject]:
        def operation(renew: bool) -> list[RemoteObject]:
            listing = self._bucket(bucket, renew).ls(prefix=prefix or None)
            return [
                RemoteObject(
                    name=obj.name,
                    bytes=int(obj.bytes),
                    last_modified=obj.last_modified,
                    hash=obj.hash,
                    content_type=obj.content_type,
                )
                for obj in listing
            ]

        return self._with_fresh_token_on_refusal(operation)

    def upload(self, bucket: str, object_name: str, local_path: Path) -> None:
        local_path = Path(local_path)

        def operation(renew: bool) -> None:
            target = self._bucket(bucket, renew)
            # A large file goes by path: the library then keeps a manifest
            # next to it, so an interrupted multipart upload can resume. A
            # small one goes as a handle that is closed here.
            if local_path.stat().st_size > EBRAINS_DRIVE_MULTIPART_THRESHOLD:
                discard_stale_manifest(local_path)
                target.upload(str(local_path), object_name, timeout=self._timeout)
            else:
                with open(local_path, "rb") as file:
                    target.upload(file, object_name, timeout=self._timeout)

        self._with_fresh_token_on_refusal(operation)

    def delete_object(self, bucket: str, object_name: str) -> None:
        def operation(renew: bool) -> None:
            self._client_for(renew).delete(
                f"/v1/buckets/{bucket}/{object_name}", timeout=self._timeout
            )

        self._with_fresh_token_on_refusal(operation)

    def _client_for(self, renew: bool) -> Any:
        if self._client is None or renew:
            token = self._tokens.access_token(force_refresh=renew)
            self._client = self._client_factory(token=token)
            self._buckets.clear()
        return self._client

    def _bucket(self, name: str, renew: bool) -> Any:
        client = self._client_for(renew)
        if name not in self._buckets:
            self._buckets[name] = client.buckets.get_bucket(name)
        return self._buckets[name]

    def _with_fresh_token_on_refusal(self, operation: Callable[[bool], _T]) -> _T:
        try:
            return operation(False)
        except Exception as error:
            if not is_auth_error(error):
                raise
        return operation(True)
