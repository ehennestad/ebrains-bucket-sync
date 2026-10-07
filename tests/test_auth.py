"""The device flow and token renewal against a scripted identity provider."""

import json
import os
import stat
from collections import deque

import pytest

from ebrains_bucket_sync.auth import (
    DEVICE_CODE_GRANT,
    ENV_TOKEN,
    AuthError,
    DeviceFlowAuthenticator,
    TokenSet,
    TokenStore,
)

WELL_KNOWN = "https://iam.example/realms/hbp/.well-known/openid-configuration"
DEVICE_URL = "https://iam.example/device"
TOKEN_URL = "https://iam.example/token"


class Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class ScriptedSession:
    """Answers GET and POST from queues per URL, and records the posts."""

    def __init__(self):
        self.responses = {
            WELL_KNOWN: deque(
                [
                    Response(
                        200,
                        {"device_authorization_endpoint": DEVICE_URL, "token_endpoint": TOKEN_URL},
                    )
                ]
            )
        }
        self.posts = []

    def queue(self, url, *responses):
        self.responses.setdefault(url, deque()).extend(responses)

    def get(self, url, timeout=None):
        return self.responses[url].popleft()

    def post(self, url, data=None, timeout=None):
        self.posts.append((url, data))
        return self.responses[url].popleft()


def tokens_response(
    access="access-1", refresh="refresh-1", expires_in=300, refresh_expires_in=1800
):
    return Response(
        200,
        {
            "access_token": access,
            "expires_in": expires_in,
            "refresh_token": refresh,
            "refresh_expires_in": refresh_expires_in,
        },
    )


@pytest.fixture
def clock():
    return {"now": 1_000_000.0}


@pytest.fixture
def authenticator(tmp_path, clock, monkeypatch):
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    session = ScriptedSession()
    notices = []
    auth = DeviceFlowAuthenticator(
        TokenStore(tmp_path / "tokens.json"),
        iam_base_url="https://iam.example/realms/hbp",
        session=session,
        notify=notices.append,
        sleep=lambda seconds: None,
        now=lambda: clock["now"],
    )
    auth.session = session
    auth.notices = notices
    return auth


def test_login_shows_the_link_polls_until_granted_and_stores_the_tokens(authenticator, clock):
    session = authenticator.session
    session.queue(
        DEVICE_URL,
        Response(
            200,
            {
                "device_code": "dc",
                "verification_uri_complete": "https://iam.example/verify?code=X",
                "interval": 5,
                "expires_in": 600,
            },
        ),
    )
    session.queue(
        TOKEN_URL,
        Response(400, {"error": "authorization_pending"}),
        Response(400, {"error": "slow_down"}),
        Response(400, {"error": "authorization_pending"}),
        tokens_response(),
    )

    token = authenticator.access_token()

    assert token == "access-1"
    assert authenticator.notices == ["To log in to EBRAINS, open https://iam.example/verify?code=X"]
    device_post = session.posts[0][1]
    assert device_post["client_id"] == "ebrains-services-toolbox-matlab"
    assert "team" in device_post["scope"] and "offline_access" in device_post["scope"]
    assert all(p[1]["grant_type"] == DEVICE_CODE_GRANT for p in session.posts[1:])

    stored = authenticator.store.load()
    assert stored.access_token == "access-1" and stored.refresh_token == "refresh-1"
    assert stored.access_expires_at == clock["now"] + 300
    assert stat.S_IMODE(os.stat(authenticator.store.path).st_mode) == 0o600


def test_stored_token_is_reused_while_valid(authenticator, clock):
    authenticator.store.save(TokenSet("stored", clock["now"] + 3600, "r", None))

    assert authenticator.access_token() == "stored"
    assert authenticator.session.posts == []


def test_expiring_token_is_renewed_from_the_refresh_token(authenticator, clock):
    authenticator.store.save(TokenSet("old", clock["now"] + 30, "refresh-0", clock["now"] + 3600))
    authenticator.session.queue(TOKEN_URL, tokens_response(access="renewed", refresh="refresh-2"))

    assert authenticator.access_token() == "renewed"
    url, data = authenticator.session.posts[0]
    assert data == {
        "grant_type": "refresh_token",
        "client_id": "ebrains-services-toolbox-matlab",
        "refresh_token": "refresh-0",
    }
    assert authenticator.store.load().refresh_token == "refresh-2"


def test_force_refresh_renews_a_valid_token(authenticator, clock):
    authenticator.store.save(TokenSet("old", clock["now"] + 3600, "refresh-0", None))
    authenticator.session.queue(TOKEN_URL, tokens_response(access="renewed"))

    assert authenticator.access_token(force_refresh=True) == "renewed"


def test_logs_in_again_when_the_refresh_is_refused(authenticator, clock):
    authenticator.store.save(TokenSet("old", clock["now"], "stale", None))
    session = authenticator.session
    session.queue(
        TOKEN_URL, Response(400, {"error": "invalid_grant"}), tokens_response(access="fresh")
    )
    session.queue(
        DEVICE_URL,
        Response(
            200,
            {
                "device_code": "dc",
                "verification_uri_complete": "u",
                "interval": 1,
                "expires_in": 60,
            },
        ),
    )

    assert authenticator.access_token() == "fresh"
    assert len(authenticator.notices) == 1


def test_refresh_token_without_expiry_is_kept(clock):
    tokens = TokenSet.from_response(
        {"access_token": "a", "expires_in": 10, "refresh_token": "r", "refresh_expires_in": 0},
        clock["now"],
    )

    assert tokens.refresh_expires_at is None
    assert tokens.refresh_is_valid(clock["now"] + 10**9)


def test_login_fails_when_the_link_expires(authenticator, clock):
    session = authenticator.session
    session.queue(
        DEVICE_URL,
        Response(
            200,
            {
                "device_code": "dc",
                "verification_uri_complete": "u",
                "interval": 5,
                "expires_in": 10,
            },
        ),
    )
    session.queue(TOKEN_URL, Response(400, {"error": "authorization_pending"}))
    original_sleep = authenticator._sleep

    def sleep(seconds):
        clock["now"] += 11
        original_sleep(seconds)

    authenticator._sleep = sleep

    with pytest.raises(AuthError, match="expired"):
        authenticator.login()


def test_login_fails_when_the_user_denies(authenticator):
    session = authenticator.session
    session.queue(
        DEVICE_URL, Response(200, {"device_code": "dc", "verification_uri_complete": "u"})
    )
    session.queue(
        TOKEN_URL, Response(400, {"error": "access_denied", "error_description": "denied"})
    )

    with pytest.raises(AuthError, match="denied"):
        authenticator.login()


def test_environment_token_wins(authenticator, monkeypatch):
    monkeypatch.setenv(ENV_TOKEN, "from-env")

    assert authenticator.access_token() == "from-env"


def test_logout_forgets_the_stored_login(authenticator, clock):
    authenticator.store.save(TokenSet("stored", clock["now"] + 3600))

    authenticator.logout()

    assert authenticator.store.load() is None


def test_corrupt_store_counts_as_no_login(tmp_path):
    path = tmp_path / "tokens.json"
    path.write_text("{not json")

    assert TokenStore(path).load() is None
    path.write_text(json.dumps({"unexpected": 1}))
    assert TokenStore(path).load() is None
