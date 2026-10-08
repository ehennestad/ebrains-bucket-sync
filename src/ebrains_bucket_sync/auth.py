"""Log in to EBRAINS with the OAuth device flow, and keep the token fresh.

The flow and the client id are those of the MATLAB toolbox
(ebrains.iam.DeviceFlowTokenClient), so both tools appear as one
application in the user's EBRAINS account. The tokens are kept in a file
that only the user can read, and the access token is renewed from the
refresh token without a new login for as long as the refresh token lasts.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import webbrowser
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import requests
from platformdirs import user_config_dir

IAM_BASE_URL = "https://iam.ebrains.eu/auth/realms/hbp/"
CLIENT_ID = "ebrains-services-toolbox-matlab"
DEFAULT_SCOPES = ("openid", "profile", "email", "team", "offline_access")
"""team lets the Data Proxy see the user's collabs; offline_access gives a
refresh token that outlives the session."""
ENV_TOKEN = "EBRAINS_BUCKET_SYNC_TOKEN"
"""An access token in this variable is used as it is, for CI and scripts."""
DEVICE_CODE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
ACCESS_TOKEN_MARGIN_SECONDS = 60
"""An access token this close to expiry is renewed before use, so that it
does not expire during the request."""


class AuthError(Exception):
    """Logging in or renewing the token failed."""


@dataclass
class TokenSet:
    """The tokens of one login, with their expiry as epoch seconds."""

    access_token: str
    access_expires_at: float
    refresh_token: str = ""
    refresh_expires_at: float | None = None
    """None for a refresh token that does not expire."""

    @classmethod
    def from_response(cls, body: dict[str, Any], now: float) -> TokenSet:
        refresh_token = body.get("refresh_token") or ""
        refresh_expires_in = float(body.get("refresh_expires_in") or 0)
        # Keycloak reports 0 for a refresh token with no expiry
        refresh_expires_at = now + refresh_expires_in if refresh_expires_in > 0 else None
        return cls(
            access_token=body["access_token"],
            access_expires_at=now + float(body.get("expires_in") or 0),
            refresh_token=refresh_token,
            refresh_expires_at=refresh_expires_at if refresh_token else None,
        )

    def access_is_valid(self, now: float) -> bool:
        return now + ACCESS_TOKEN_MARGIN_SECONDS < self.access_expires_at

    def refresh_is_valid(self, now: float) -> bool:
        if not self.refresh_token:
            return False
        return self.refresh_expires_at is None or now < self.refresh_expires_at


class TokenStore:
    """The tokens of the user, in a file only the user can read."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(user_config_dir("ebrains-bucket-sync")) / "tokens.json"

    def load(self) -> TokenSet | None:
        try:
            return TokenSet(**json.loads(self.path.read_text()))
        except (OSError, ValueError, TypeError):
            return None

    def save(self, tokens: TokenSet) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # mkstemp creates a new file that only the user can read, so the
        # tokens are never written to a file that others can read
        descriptor, temporary = tempfile.mkstemp(
            dir=self.path.parent, prefix=self.path.name, suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "w") as file:
                json.dump(asdict(tokens), file)
            os.replace(temporary, self.path)
        except BaseException:
            # a failed write must not leave a copy of the tokens behind
            Path(temporary).unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def _print_to_stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _open_in_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except webbrowser.Error:
        pass  # the printed link still lets the user open the page


class DeviceFlowAuthenticator:
    """A TokenSource that logs the user in with the device flow when needed.

    Args:
        store: Where the tokens are kept between runs.
        client_id, scopes, iam_base_url: The OIDC client and what it asks for.
        session: HTTP session with get and post, for tests.
        notify: Shows the login link to the user. Prints to stderr by default.
        open_browser: Opens the login page. Uses the default web browser by
            default; the printed link is the fallback where none opens.
        sleep, now: For tests.
        timeout: Seconds to wait for each request.
    """

    def __init__(
        self,
        store: TokenStore | None = None,
        *,
        client_id: str = CLIENT_ID,
        scopes: Sequence[str] = DEFAULT_SCOPES,
        iam_base_url: str = IAM_BASE_URL,
        session: Any = None,
        notify: Callable[[str], None] = _print_to_stderr,
        open_browser: Callable[[str], None] = _open_in_browser,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
        timeout: float = 30.0,
    ) -> None:
        self.store = store or TokenStore()
        self.client_id = client_id
        self.scopes = tuple(scopes)
        self.iam_base_url = iam_base_url.rstrip("/") + "/"
        self._session = session or requests.Session()
        self._notify = notify
        self._open_browser = open_browser
        self._sleep = sleep
        self._now = now
        self._timeout = timeout
        self._tokens: TokenSet | None = None
        self._endpoints: dict[str, str] | None = None

    def access_token(self, *, force_refresh: bool = False) -> str:
        """A valid access token: from the environment, the store, a refresh or a login."""
        from_environment = os.environ.get(ENV_TOKEN)
        if from_environment:
            return from_environment

        tokens = self._tokens or self.store.load()
        now = self._now()
        if tokens is not None and not force_refresh and tokens.access_is_valid(now):
            self._tokens = tokens
            return tokens.access_token

        if tokens is not None and tokens.refresh_is_valid(now):
            try:
                return self._remember(self.refresh(tokens)).access_token
            except AuthError:
                pass  # the refresh token was revoked or is stale, so log in again

        return self._remember(self.login()).access_token

    def login(self) -> TokenSet:
        """Run the device flow: open the link, wait for the user, return the tokens."""
        endpoints = self._get_endpoints()
        response = self._session.post(
            endpoints["device_authorization_endpoint"],
            data={"client_id": self.client_id, "scope": " ".join(self.scopes)},
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise AuthError(f"Could not start the login: {_describe(response)}")
        device = response.json()

        link = device.get("verification_uri_complete") or (
            f"{device.get('verification_uri')} and enter the code {device.get('user_code')}"
        )
        self._notify(f"To log in to EBRAINS, open {link}")
        self._open_browser(device.get("verification_uri_complete") or device["verification_uri"])

        interval = float(device.get("interval") or 5)
        deadline = self._now() + float(device.get("expires_in") or 600)
        while True:
            self._sleep(interval)
            if self._now() > deadline:
                raise AuthError("The login link expired before it was used. Log in again.")
            response = self._session.post(
                endpoints["token_endpoint"],
                data={
                    "grant_type": DEVICE_CODE_GRANT,
                    "client_id": self.client_id,
                    "device_code": device["device_code"],
                },
                timeout=self._timeout,
            )
            if response.status_code == 200:
                return TokenSet.from_response(response.json(), self._now())
            error = _error_code(response)
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5  # RFC 8628, section 3.5
                continue
            raise AuthError(f"The login was not completed: {_describe(response)}")

    def refresh(self, tokens: TokenSet) -> TokenSet:
        """A new TokenSet from the refresh token.

        A reply without a new refresh token leaves the old one in use, as
        RFC 6749, section 6, has it.
        """
        response = self._session.post(
            self._get_endpoints()["token_endpoint"],
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "refresh_token": tokens.refresh_token,
            },
            timeout=self._timeout,
        )
        if response.status_code != 200:
            raise AuthError(f"Could not renew the login: {_describe(response)}")
        renewed = TokenSet.from_response(response.json(), self._now())
        if not renewed.refresh_token:
            renewed = replace(
                renewed,
                refresh_token=tokens.refresh_token,
                refresh_expires_at=tokens.refresh_expires_at,
            )
        return renewed

    def logout(self) -> None:
        self._tokens = None
        self.store.clear()

    def _remember(self, tokens: TokenSet) -> TokenSet:
        self._tokens = tokens
        self.store.save(tokens)
        return tokens

    def _get_endpoints(self) -> dict[str, str]:
        if self._endpoints is None:
            response = self._session.get(
                self.iam_base_url + ".well-known/openid-configuration", timeout=self._timeout
            )
            if response.status_code != 200:
                raise AuthError(
                    "Could not read the configuration of the identity provider: "
                    + _describe(response)
                )
            config = response.json()
            self._endpoints = {
                "device_authorization_endpoint": config["device_authorization_endpoint"],
                "token_endpoint": config["token_endpoint"],
            }
        return self._endpoints


def _error_code(response: Any) -> str:
    try:
        return str(response.json().get("error", ""))
    except ValueError:
        return ""


def _describe(response: Any) -> str:
    try:
        body = response.json()
        detail = body.get("error_description") or body.get("error") or ""
    except ValueError:
        detail = ""
    return f"HTTP {response.status_code} {detail}".strip()
