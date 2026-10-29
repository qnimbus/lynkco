"""Tests for the API client's auth helpers (no network access)."""

import asyncio
import base64
import json
import logging

import aiohttp
import pytest

from custom_components.lynkco.api import LynkCoAPI, LynkCoAuthError, mask_vin, mask_vins
from custom_components.lynkco.const import LOVE_BASE

REDIRECT = "msauth://prod.lynkco.app.crisp.prod/2jmj7l5rSw0yVb%2FvlWAYkK%2FYBwk%3D"
CODE = "eyJraWQiOiJjcGltY29yZV8wOTI1MjAxNSIsInZlciI6IjEuMCJ9..Ab-c_D.eF9"


def make_jwt(claims: dict) -> str:
    def part(data: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


# --- parse_redirect -----------------------------------------------------------


@pytest.mark.parametrize(
    "pasted",
    [
        f"{REDIRECT}?state=abc&code={CODE}&client_info=xyz",
        f"  {REDIRECT}?code={CODE}\n",
        # Firefox: "Copy response headers"
        f"HTTP/2 302\r\ncache-control: no-cache\r\nlocation: {REDIRECT}?code={CODE}&state=s\r\nx-ms-request-id: 1\r\n",
        # Chrome / Edge console: "Copy message" on the failed redirect
        f"Failed to launch '{REDIRECT}?code={CODE}&client_info=x' because the "
        "scheme does not have a registered handler.",
        # The whole console copied, with other errors around it
        f"[hCaptcha] render: invalid container 'null'.\nconfirmed?csrf_token=abc:1 "
        f"Failed to launch '{REDIRECT}?code={CODE}' because the scheme does not "
        "have a registered handler.\n",
        # Chrome: the value of the Location header pasted with its label
        f"Location\t{REDIRECT}?code={CODE}",
        f"code={CODE}&state=s",
        CODE,
    ],
)
def test_parse_redirect_finds_the_code(pasted: str) -> None:
    assert LynkCoAPI.parse_redirect(pasted) == (CODE, None)


def test_parse_redirect_decodes_an_encoded_code() -> None:
    assert LynkCoAPI.parse_redirect(f"{REDIRECT}?code=ab%2Bcd%3D") == ("ab+cd=", None)


def test_parse_redirect_reports_a_failed_login() -> None:
    pasted = (
        f"{REDIRECT}?error=access_denied&error_description=AADB2C90091%3a+The+user+"
        "has+cancelled+entering+self-asserted+information.%0d%0aCorrelation+ID%3a+123"
    )
    assert LynkCoAPI.parse_redirect(pasted) == (
        None,
        "AADB2C90091: The user has cancelled entering self-asserted information.",
    )


def test_parse_redirect_error_without_description() -> None:
    assert LynkCoAPI.parse_redirect(f"{REDIRECT}?error=server_error") == (None, "server_error")


@pytest.mark.parametrize(
    "pasted",
    ["", "not a redirect", "abc", f"{REDIRECT}?state=only", "https://login.lynkco.com/"],
)
def test_parse_redirect_without_code(pasted: str) -> None:
    assert LynkCoAPI.parse_redirect(pasted) == (None, None)


# --- token refresh ----------------------------------------------------------


class FakeResponse:
    def __init__(self, status: int, data: dict | None = None) -> None:
        self.status = status
        self._data = data
        self.content_length = None if data is not None else 0

    async def json(self):
        return self._data

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeRefresh(FakeResponse):
    """A token endpoint call that yields to the event loop before answering."""

    def __init__(self, session: FakeSession) -> None:
        super().__init__(200)
        self._session = session

    async def __aenter__(self):
        for _ in range(3):
            await asyncio.sleep(0)  # let other requests run meanwhile
        session = self._session
        session.refresh_calls += 1
        if not session.refresh_accepted:
            self.status = 400
            self._data = {"error": "invalid_grant"}
            return self
        session.valid_token = make_jwt({"email": "a@b.c", "n": session.refresh_calls})
        self._data = {
            "access_token": session.valid_token,
            "refresh_token": f"refresh-{session.refresh_calls + 1}",
            "expires_in": 3600,
            "refresh_token_expires_in": 1209600,
        }
        return self


class FakeSession:
    """Accepts only the newest access token and counts refresh calls."""

    def __init__(self) -> None:
        self.valid_token: str | None = None
        self.refresh_calls = 0
        self.refresh_accepted = True

    def request(self, method, url, headers, **kwargs):
        if headers["Authorization"] == f"Bearer {self.valid_token}":
            return FakeResponse(200, {"ok": True})
        return FakeResponse(401)

    def post(self, url, headers, data):
        return FakeRefresh(self)


def make_api(session: FakeSession, saved: list | None = None) -> LynkCoAPI:
    return LynkCoAPI(
        session,
        make_jwt({"email": "a@b.c", "n": 0}),
        "refresh-1",
        "device",
        on_token_refresh=(lambda *tokens: saved.append(tokens)) if saved is not None else None,
    )


async def test_concurrent_401s_refresh_only_once() -> None:
    session = FakeSession()
    saved: list = []
    api = make_api(session, saved)

    results = await asyncio.gather(
        *(api._request("GET", f"{LOVE_BASE}/vehicle/{n}") for n in range(3))
    )

    assert results == [{"ok": True}] * 3
    assert session.refresh_calls == 1
    assert saved == [(session.valid_token, "refresh-2")]
    assert api.refresh_token == "refresh-2"


async def test_refresh_without_stale_token_always_runs() -> None:
    session = FakeSession()
    api = make_api(session)

    assert await api.refresh_tokens()
    assert await api.refresh_tokens()
    assert session.refresh_calls == 2


async def test_rejected_refresh_raises_auth_error() -> None:
    session = FakeSession()
    session.refresh_accepted = False
    api = make_api(session)

    with pytest.raises(LynkCoAuthError):
        await api._request("GET", f"{LOVE_BASE}/vehicle/x")
    assert session.refresh_calls == 1


# --- VIN masking ------------------------------------------------------------

VIN = "ABCDEFGH123451234"


def test_mask_vin_keeps_the_last_four() -> None:
    assert mask_vin(VIN) == "***1234"
    assert mask_vin("") == ""


def test_mask_vins_in_error_text() -> None:
    error = f"404, message='Not Found', url='{LOVE_BASE}/vehicle/{VIN}/charge_state'"
    masked = mask_vins(error)
    assert VIN not in masked
    assert "/vehicle/***1234/charge_state" in masked
    # Other long uppercase words are not VINs (wrong length)
    assert mask_vins("ACTIVE_HEATING DX11_MORE") == "ACTIVE_HEATING DX11_MORE"


async def test_debug_log_does_not_contain_the_vin(caplog: pytest.LogCaptureFixture) -> None:
    session = FakeSession()
    api = make_api(session)
    session.valid_token = api.access_token
    caplog.set_level(logging.DEBUG, logger="custom_components.lynkco.api")

    await api._request("GET", f"{LOVE_BASE}/vehicle/{VIN}/charge_state")

    assert "/vehicle/***1234/charge_state" in caplog.text
    assert VIN not in caplog.text


async def test_refresh_logs_token_lifetimes_but_not_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = FakeSession()
    api = make_api(session)
    caplog.set_level(logging.DEBUG, logger="custom_components.lynkco.api")

    assert await api.refresh_tokens()

    assert "access token valid for 3600 s (60 min)" in caplog.text
    assert "refresh token valid for 1209600 s (14 days)" in caplog.text
    assert "refresh_token_expires_in" in caplog.text  # field names are listed
    assert api.access_token not in caplog.text
    assert api.refresh_token not in caplog.text
