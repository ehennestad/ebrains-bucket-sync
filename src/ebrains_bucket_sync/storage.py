"""Access to Data Proxy buckets through the ebrains_drive package.

This is the only module that imports ebrains_drive. The engine talks to a
model.BucketStorage, so tests use an in-memory one, and a change in the
library's interface stays contained here.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import quote

import requests
from ebrains_drive import BucketApiClient
from ebrains_drive.exceptions import ClientHttpError, TokenExpired, Unauthorized
from ebrains_drive.utils import EBRAINS_DRIVE_MULTIPART_THRESHOLD

from .model import DownloadProgress, RemoteObject
from .paths import MULTIPART_MANIFEST_SUFFIX

_T = TypeVar("_T")

LISTING_PROGRESS_STEP = 1000
"""Objects between two progress reports of a listing. The time of a listing
grows with the objects it returns (about 30 ms each, as measured on the
Data Proxy), so this reports about every half minute."""

DOWNLOAD_CHUNK_SIZE = 1024 * 1024
"""Bytes read from a download at a time, and between two progress reports."""


class _SessionWithTimeout(requests.Session):
    """A session whose requests wait at most `timeout` seconds unless told otherwise.

    ebrains_drive sends the listing of a bucket, the lookup of a bucket
    and the requests for signed URLs through the session of its client
    without a timeout, so a stalled connection would hang the sync
    forever. The client's session is replaced with this one right after
    the client is made, before any request.
    """

    def __init__(self, timeout: float) -> None:
        super().__init__()
        self.timeout = timeout

    def request(  # type: ignore[override]  # requests names every keyword; all pass through
        self, method: str, url: str | bytes, *args: Any, **kwargs: Any
    ) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        return super().request(method, url, *args, **kwargs)


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


def can_write_in(folder: Path) -> bool:
    """Whether a file can be created in folder.

    Found by creating one, since the permissions can say yes where writing
    fails, such as on a network file system with its own access rules.
    """
    try:
        with tempfile.TemporaryFile(dir=folder):
            pass
    except OSError:
        return False
    return True


def object_url_path(object_name: str) -> str:
    """Object name for the path of a Data Proxy URL: percent-encoded, "/" kept.

    The Data Proxy decodes the path, so an encoded name reaches it intact.
    Unencoded, "#" ends the path and "?" starts a query string, so the
    request names a different object, and "%" followed by two hex digits
    is decoded into another character.
    """
    return quote(object_name, safe="/")


class EbrainsDriveStorage:
    """BucketStorage over the Data Proxy, through ebrains_drive.

    A request refused for its token is sent once more with a fresh token
    from the TokenSource, so a token that expires during a long sync is
    renewed without the sync noticing. Requests may run on several threads.
    When their token expires, they are all refused at about the same time,
    and the token is renewed once: the other threads retry with the client
    the first one made.

    Args:
        tokens: Where the access token comes from.
        timeout: Seconds to wait for each request.
        client_factory: Makes the ebrains_drive client, for tests.
        session: HTTP session that fetches the objects from their download
            URLs, which need no token. For tests.
    """

    def __init__(
        self,
        tokens: TokenSource,
        *,
        timeout: float = 120.0,
        client_factory: Callable[..., Any] = BucketApiClient,
        session: Any = None,
    ) -> None:
        self._tokens = tokens
        self._timeout = timeout
        self._client_factory = client_factory
        self._session = session or requests.Session()
        self._client: Any = None
        self._buckets: dict[str, Any] = {}
        """Buckets of self._client, by name."""
        self._lock = threading.Lock()

    def list_objects(
        self, bucket: str, prefix: str, *, on_progress: Callable[[int], None] | None = None
    ) -> list[RemoteObject]:
        """Every object whose name starts with prefix.

        on_progress, if given, is called with the number of objects listed
        so far every LISTING_PROGRESS_STEP objects and once at the end, as
        model.ListingProgressStorage describes.
        """

        def operation(client: Any) -> list[RemoteObject]:
            listing = self._bucket(client, bucket).ls(prefix=prefix or None)
            objects = []
            for obj in listing:
                objects.append(
                    RemoteObject(
                        name=obj.name,
                        bytes=int(obj.bytes),
                        last_modified=obj.last_modified,
                        hash=obj.hash,
                        content_type=obj.content_type,
                    )
                )
                if on_progress is not None and len(objects) % LISTING_PROGRESS_STEP == 0:
                    on_progress(len(objects))
            if on_progress is not None:
                on_progress(len(objects))
            return objects

        return self._with_fresh_token_on_refusal(operation)

    def object_count(self, bucket: str) -> int:
        """Number of objects of the bucket, from its stat record in one request."""

        def operation(client: Any) -> int:
            response = client.get(f"/v1/buckets/{bucket}/stat", timeout=self._timeout)
            return int(response.json()["objects_count"])

        return self._with_fresh_token_on_refusal(operation)

    def get_download_url(self, bucket: str, object_name: str) -> str:
        """A URL from which the object can be fetched without a token.

        The URL expires within seconds of being issued, so fetch right
        after asking for it. Without redirect=false the endpoint redirects
        to the object itself instead of answering with the URL.
        """

        def operation(client: Any) -> str:
            response = client.get(
                f"/v1/buckets/{bucket}/{object_url_path(object_name)}",
                params={"redirect": "false"},
                timeout=self._timeout,
            )
            return response.json()["url"]

        return self._with_fresh_token_on_refusal(operation)

    def download(
        self,
        bucket: str,
        object_name: str,
        local_path: Path,
        *,
        on_progress: DownloadProgress | None = None,
    ) -> None:
        """Fetch an object into local_path, which is created or replaced.

        The file is written as the object arrives, so a download that fails
        leaves a partial file: engine.download_object fetches into a
        temporary file for that reason. on_progress is called after each
        chunk with the bytes received and the size the response declares.
        """
        url = self.get_download_url(bucket, object_name)
        with self._session.get(url, stream=True, timeout=self._timeout) as response:
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            total = int(length) if length else None
            received = 0
            with open(local_path, "wb") as file:
                for chunk in response.iter_content(DOWNLOAD_CHUNK_SIZE):
                    file.write(chunk)
                    received += len(chunk)
                    if on_progress is not None:
                        on_progress(received, total)

    def upload(self, bucket: str, object_name: str, local_path: Path) -> None:
        local_path = Path(local_path)

        def operation(client: Any) -> None:
            target = self._bucket(client, bucket)
            # ebrains_drive puts the name into the request URL as it is given
            url_path = object_url_path(object_name)
            # A large file goes by path: the library then keeps a manifest
            # next to it, so an interrupted multipart upload can resume. Other
            # files go as a handle that is closed here, and the library keeps
            # no manifest: a small file, and a large one in a folder that
            # cannot be written to, where writing the manifest would fail.
            is_large = local_path.stat().st_size > EBRAINS_DRIVE_MULTIPART_THRESHOLD
            if is_large and can_write_in(local_path.parent):
                discard_stale_manifest(local_path)
                target.upload(str(local_path), url_path, timeout=self._timeout)
            else:
                with open(local_path, "rb") as file:
                    target.upload(file, url_path, timeout=self._timeout)

        self._with_fresh_token_on_refusal(operation)

    def delete_object(self, bucket: str, object_name: str) -> None:
        def operation(client: Any) -> None:
            client.delete(
                f"/v1/buckets/{bucket}/{object_url_path(object_name)}", timeout=self._timeout
            )

        self._with_fresh_token_on_refusal(operation)

    def _current_client(self) -> Any:
        with self._lock:
            if self._client is None:
                self._install_client(self._tokens.access_token())
            return self._client

    def _renewed_client(self, refused: Any) -> Any:
        """A client with a fresh token, in place of the client that was refused.

        Only the first thread refused with a client renews it. A thread that
        gets the lock after that finds the client already replaced, and uses
        the new one without asking for another token.
        """
        with self._lock:
            if self._client is refused:
                self._install_client(self._tokens.access_token(force_refresh=True))
            return self._client

    def _install_client(self, token: str) -> None:
        self._client = self._client_factory(token=token)
        if isinstance(getattr(self._client, "session", None), requests.Session):
            # ebrains_drive keeps no state in its session: no headers, no
            # cookies, so a fresh one with a timeout can take its place
            self._client.session = _SessionWithTimeout(self._timeout)
        self._buckets = {}

    def _bucket(self, client: Any, name: str) -> Any:
        with self._lock:
            if client is self._client and name in self._buckets:
                return self._buckets[name]
        bucket = client.buckets.get_bucket(name)
        with self._lock:
            if client is self._client:
                self._buckets[name] = bucket
        return bucket

    def _with_fresh_token_on_refusal(self, operation: Callable[[Any], _T]) -> _T:
        client = self._current_client()
        try:
            return operation(client)
        except Exception as error:
            if not is_auth_error(error):
                raise
        return operation(self._renewed_client(client))
