"""EbrainsDriveStorage against a stand-in for the ebrains_drive client."""

from types import SimpleNamespace

import pytest
from ebrains_drive.exceptions import ClientHttpError, Unauthorized

import ebrains_bucket_sync.storage as storage_module
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
        self.buckets = SimpleNamespace(get_bucket=lambda name: FakeBucket(self, name))
        FakeClient.instances.append(self)

    def delete(self, url, **kwargs):
        self.deleted.append(url)


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


def test_delete_uses_the_bucket_api_path():
    storage = EbrainsDriveStorage(RecordingTokens(), client_factory=FakeClient)

    storage.delete_object("b", "sub/a.txt")

    assert FakeClient.instances[0].deleted == ["/v1/buckets/b/sub/a.txt"]


def test_transient_errors():
    assert is_transient_error(ConnectionError())
    assert is_transient_error(ClientHttpError(503, "busy"))
    assert not is_transient_error(ClientHttpError(404, "gone"))
    assert not is_transient_error(ValueError())
