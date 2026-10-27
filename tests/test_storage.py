"""EbrainsDriveStorage against a stand-in for the ebrains_drive client."""

import os
import threading
import time
from types import SimpleNamespace

import pytest
import requests
from ebrains_drive.exceptions import ClientHttpError, TokenExpired, Unauthorized

import ebrains_bucket_sync.storage as storage_module
from ebrains_bucket_sync import SyncOptions, sync_to_bucket
from ebrains_bucket_sync.storage import EbrainsDriveStorage, is_transient_error

from .conftest import write


class RecordingTokens:
    def __init__(self):
        self.calls = []

    def access_token(self, *, force_refresh=False):
        self.calls.append(force_refresh)
        return f"token-{len(self.calls)}"


class FakeBucket:
    def __init__(self, client, name):
        self.client = client
        self.name = name
        self.uploads = []

    def ls(self, prefix=None):
        self.client.ls_calls += 1
        if self.client.refuse_first and self.client.ls_calls == 1:
            raise Unauthorized("expired")
        yield SimpleNamespace(
            name="a.txt", bytes=1, last_modified="2024-01-01T00:00:00", hash="h", content_type="t"
        )

    def upload(self, filelike, filename, **kwargs):
        self.uploads.append(
            (filelike if isinstance(filelike, str) else "<handle>", filename, kwargs)
        )


class FakeClient:
    instances = []

    def __init__(self, token):
        self.token = token
        self.ls_calls = 0
        self.refuse_first = False
        self.deleted = []
        self.requests = []
        self.buckets = SimpleNamespace(get_bucket=lambda name: FakeBucket(self, name))
        FakeClient.instances.append(self)

    def delete(self, url, **kwargs):
        self.deleted.append(url)

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if url.endswith("/stat"):
            return SimpleNamespace(json=lambda: {"objects_count": 42})
        return SimpleNamespace(json=lambda: {"url": "https://store.example/signed?sig=1"})


class FakeResponse:
    """What a session answers a streamed download with."""

    def __init__(self, content, length=True):
        self.content = content
        self.headers = {"Content-Length": str(len(content))} if length else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for start in range(0, len(self.content), chunk_size):
            yield self.content[start : start + chunk_size]


class FakeSession:
    def __init__(self, content, length=True):
        self.content = content
        self.length = length
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.content, self.length)


@pytest.fixture(autouse=True)
def reset_clients():
    FakeClient.instances = []


def test_lists_objects_with_one_client_per_token():
    tokens = RecordingTokens()
    storage = EbrainsDriveStorage(tokens, client_factory=FakeClient)

    objects = storage.list_objects("b", "")
    storage.list_objects("b", "")

    assert [(o.name, o.bytes, o.hash, o.content_type) for o in objects] == [("a.txt", 1, "h", "t")]
    assert tokens.calls == [False]
    assert len(FakeClient.instances) == 1


def test_renews_the_token_once_when_a_request_is_refused():
    tokens = RecordingTokens()
    storage = EbrainsDriveStorage(tokens, client_factory=FakeClient)
    FakeClient.instances.clear()
    storage.list_objects("b", "")  # makes the first client
    FakeClient.instances[0].refuse_first = True
    FakeClient.instances[0].ls_calls = 0

    objects = storage.list_objects("b", "")

    assert len(objects) == 1
    assert tokens.calls == [False, True]
    assert FakeClient.instances[-1].token == "token-2"


def test_token_is_renewed_once_when_parallel_uploads_are_refused(folder):
    workers = 4
    for k in range(workers):
        write(folder, f"f{k}.txt", b"x")
    # Every upload with the first token waits here until all workers hold
    # one, so they are all refused together, as when a token expires.
    refused_together = threading.Barrier(workers, timeout=5)
    renewals = []
    uploads = []

    class Tokens:
        def access_token(self, *, force_refresh=False):
            if force_refresh:
                renewals.append(threading.current_thread().name)
            return f"token-{len(renewals)}"

    class Bucket:
        def __init__(self, token):
            self.token = token

        def ls(self, prefix=None):
            return []

        def upload(self, filelike, filename, **kwargs):
            if self.token == "token-0":
                refused_together.wait()
                raise TokenExpired()
            uploads.append((self.token, filename))

    class Client:
        def __init__(self, token):
            self.buckets = SimpleNamespace(get_bucket=lambda name: Bucket(token))

    storage = EbrainsDriveStorage(Tokens(), client_factory=Client)

    results = sync_to_bucket(folder, "b", storage, SyncOptions(workers=workers))

    assert len(renewals) == 1
    assert {(r.path, r.status) for r in results} == {(f"f{k}.txt", "done") for k in range(workers)}
    assert sorted(uploads) == [("token-1", f"f{k}.txt") for k in range(workers)]


def test_requests_of_the_client_get_the_timeout_unless_given_one(monkeypatch):
    # ebrains_drive lists a bucket and asks for signed URLs through its
    # client's session without a timeout, which would hang on a stalled
    # connection
    class Client:
        def __init__(self, token):
            self.session = requests.Session()

    sent = []
    monkeypatch.setattr(
        requests.Session, "request", lambda self, method, url, **kwargs: sent.append(kwargs)
    )
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=Client, timeout=7)

    client = storage._current_client()
    client.session.request("GET", "https://data-proxy.example.org/v1/buckets/b")
    client.session.request("GET", "https://data-proxy.example.org/v1/buckets/b", timeout=1)

    assert [kwargs["timeout"] for kwargs in sent] == [7, 1]


def test_other_errors_are_not_retried_with_a_new_token():
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient)

    def failing_ls(prefix=None):
        raise ClientHttpError(500, "boom")

    client = FakeClient("t")
    bucket = FakeBucket(client, "b")
    bucket.ls = failing_ls
    client.buckets.get_bucket = lambda name: bucket
    storage._client_factory = lambda token: client

    with pytest.raises(ClientHttpError):
        storage.list_objects("b", "")


def test_small_files_go_as_a_handle_and_large_files_by_path(folder, monkeypatch):
    monkeypatch.setattr(storage_module, "EBRAINS_DRIVE_MULTIPART_THRESHOLD", 2)
    small = write(folder, "small.bin", b"x")
    large = write(folder, "large.bin", b"xyz")
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient, timeout=7)

    storage.upload("b", "small.bin", small)
    storage.upload("b", "large.bin", large)

    bucket = storage._buckets["b"]
    assert bucket.uploads == [
        ("<handle>", "small.bin", {"timeout": 7}),
        (str(large), "large.bin", {"timeout": 7}),
    ]


def set_mtime(path, seconds_from_now):
    moment = time.time_ns() + int(seconds_from_now * 1e9)
    os.utime(path, ns=(moment, moment))


def upload_large(folder, monkeypatch, name="large.bin"):
    monkeypatch.setattr(storage_module, "EBRAINS_DRIVE_MULTIPART_THRESHOLD", 2)
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient)
    storage.upload("b", name, folder / name)
    return storage._buckets["b"]


def test_manifest_of_an_unchanged_file_is_kept_for_the_resume(folder, monkeypatch):
    large = write(folder, "large.bin", b"xyz")
    manifest = write(folder, "large.bin.multipart_manifest.json", b"{}")
    set_mtime(manifest, 60)  # written after the file last changed

    bucket = upload_large(folder, monkeypatch)

    assert manifest.exists()
    assert bucket.uploads[0][0] == str(large)


def test_manifest_of_a_file_changed_after_it_is_discarded(folder, monkeypatch):
    write(folder, "large.bin", b"xyz")
    manifest = write(folder, "large.bin.multipart_manifest.json", b"{}")
    set_mtime(manifest, -60)  # the file changed after the last part was recorded

    upload_large(folder, monkeypatch)

    assert not manifest.exists()


@pytest.mark.skipif(os.name == "nt", reason="Windows has no change time to detect this with.")
def test_manifest_of_a_file_replaced_with_an_older_time_is_discarded(folder, monkeypatch):
    manifest = write(folder, "large.bin.multipart_manifest.json", b"{}")
    set_mtime(manifest, -60)
    # A copy that keeps the time of its original, as cp -p does: the file
    # looks older than the manifest, but its change time is now.
    large = write(folder, "large.bin", b"abc")
    set_mtime(large, -3600)

    upload_large(folder, monkeypatch)

    assert not manifest.exists()


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="Needs a folder the user cannot write to, which Windows and root do not give.",
)
def test_large_file_in_a_read_only_folder_goes_as_a_handle(folder, monkeypatch):
    large = write(folder, "large.bin", b"xyz")
    folder.chmod(0o555)
    try:
        bucket = upload_large(folder, monkeypatch)
    finally:
        folder.chmod(0o755)

    assert bucket.uploads[0][:2] == ("<handle>", "large.bin")
    assert sorted(path.name for path in folder.iterdir()) == [large.name]


def test_delete_uses_the_bucket_api_path():
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient)

    storage.delete_object("b", "sub/a.txt")

    assert FakeClient.instances[0].deleted == ["/v1/buckets/b/sub/a.txt"]


@pytest.mark.parametrize(
    ("object_name", "url_path"),
    [
        ("sub/run#2.csv", "sub/run%232.csv"),
        ("what?.txt", "what%3F.txt"),
        ("50%25done.txt", "50%2525done.txt"),
        ("a b+c.txt", "a%20b%2Bc.txt"),
        ("ø.txt", "%C3%B8.txt"),
    ],
)
def test_object_names_are_encoded_in_urls(folder, monkeypatch, object_name, url_path):
    monkeypatch.setattr(storage_module, "EBRAINS_DRIVE_MULTIPART_THRESHOLD", 2)
    small = write(folder, "small.bin", b"x")
    large = write(folder, "large.bin", b"xyz")
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient)

    storage.upload("b", object_name, small)
    storage.upload("b", object_name, large)
    storage.delete_object("b", object_name)

    assert [name for _, name, _ in storage._buckets["b"].uploads] == [url_path, url_path]
    assert FakeClient.instances[0].deleted == [f"/v1/buckets/b/{url_path}"]


def test_transient_errors():
    assert is_transient_error(ConnectionError())
    assert is_transient_error(ClientHttpError(503, "busy"))
    assert not is_transient_error(ClientHttpError(404, "gone"))
    assert not is_transient_error(ValueError())


def test_listing_reports_progress_at_the_step_and_at_the_end(monkeypatch):
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient)
    reported = []

    storage.list_objects("b", "", on_progress=reported.append)
    assert reported == [1]

    monkeypatch.setattr(storage_module, "LISTING_PROGRESS_STEP", 1)
    reported.clear()
    storage.list_objects("b", "", on_progress=reported.append)
    assert reported == [1, 1]


def test_object_count_comes_from_the_stat_record():
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient, timeout=7)

    assert storage.object_count("b") == 42
    assert FakeClient.instances[0].requests == [("/v1/buckets/b/stat", {"timeout": 7})]


def test_download_url_is_asked_for_without_a_redirect():
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient, timeout=7)

    url = storage.get_download_url("b", "sub/run#2.csv")

    assert url == "https://store.example/signed?sig=1"
    assert FakeClient.instances[0].requests == [
        ("/v1/buckets/b/sub/run%232.csv", {"params": {"redirect": "false"}, "timeout": 7})
    ]


def test_download_streams_the_object_into_the_file(folder, monkeypatch):
    monkeypatch.setattr(storage_module, "DOWNLOAD_CHUNK_SIZE", 2)
    session = FakeSession(b"abcde")
    storage = EbrainsDriveStorage(
        RecordingTokens(), client_factory=FakeClient, session=session, timeout=7
    )
    progress = []

    storage.download(
        "b", "a.dat", folder / "a.dat", on_progress=lambda *args: progress.append(args)
    )

    assert (folder / "a.dat").read_bytes() == b"abcde"
    assert progress == [(2, 5), (4, 5), (5, 5)]
    assert session.calls == [("https://store.example/signed?sig=1", {"stream": True, "timeout": 7})]


def test_download_without_a_content_length_reports_no_total(folder):
    session = FakeSession(b"abc", length=False)
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient, session=session)
    progress = []

    storage.download(
        "b", "a.dat", folder / "a.dat", on_progress=lambda *args: progress.append(args)
    )

    assert progress == [(3, None)]
