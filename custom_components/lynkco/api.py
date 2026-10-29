"""API client for Lynk & Co vehicles."""

import asyncio
import hashlib
import json
import logging
import re
import urllib.parse
import uuid

import aiohttp
import pkce

from .const import (
    CLIENT_ID,
    COMMAND_BASE,
    IAM_BASE,
    LOGIN_B2C_URL,
    LOVE_BASE,
    REDIRECT_URI,
    SCOPE,
    SIGNATURE_BASE_URLS,
    SIGNATURE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Lower-cased fragments of JSON keys whose values never reach the debug log.
REDACT_KEY_PARTS = (
    "token",
    "authorization",
    "password",
    "secret",
    "vin",
    "email",
    "address",
    "latitude",
    "longitude",
    "coordinate",
    "customer",
    "snowflake",
    "device",
    "temid",
    "registrationnumber",
    "vehiclename",
)


# The msauth:// redirect anywhere in pasted text (e.g. a block of response headers).
_REDIRECT_RE = re.compile(r"msauth://[^\s\"'<>]+")
# A bare authorization code: base64url segments joined by dots, no whitespace.
_BARE_CODE_RE = re.compile(r"[A-Za-z0-9_.\-]{20,}")
# A VIN: 17 characters, digits and capitals except I, O and Q.
_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")


class LynkCoAuthError(Exception):
    """The session is no longer valid and the refresh token was rejected.

    Raised instead of a plain HTTP error so the integration can ask Home
    Assistant to start the re-authentication flow (Settings > Repairs).
    """


def redact(value: object, key: str = "") -> object:
    """Return a copy of an API payload with sensitive values replaced."""
    if any(part in key.lower() for part in REDACT_KEY_PARTS):
        return "<redacted>"
    if isinstance(value, dict):
        return {name: redact(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return value


def mask_vin(vin: str) -> str:
    """Shorten a VIN for logs: the last 4 characters still tell cars apart."""
    return f"***{vin[-4:]}" if vin else vin


def mask_vins(text: object) -> str:
    """Mask every VIN in a text, e.g. an error message that contains a URL."""
    return _VIN_RE.sub(lambda match: mask_vin(match.group()), str(text))


def _duration(seconds: object) -> str:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return "unknown (not in response)"
    if value >= 86400:
        return f"{value} s ({value / 86400:g} days)"
    return f"{value} s ({value / 60:g} min)"


def _log_token_lifetimes(event: str, data: dict) -> None:
    """Log how long B2C says new tokens stay valid. Never logs token values.

    The refresh token is opaque, so B2C's refresh_token_expires_in is the only
    way to learn how long a session survives without a refresh.
    """
    if not _LOGGER.isEnabledFor(logging.DEBUG):
        return
    _LOGGER.debug(
        "%s: access token valid for %s, refresh token valid for %s (response fields: %s)",
        event,
        _duration(data.get("expires_in")),
        _duration(data.get("refresh_token_expires_in")),
        ", ".join(sorted(data)),
    )


def _compute_signature(secret: str, nonce: str, path: str) -> str:
    return hashlib.sha256((secret + nonce + path).encode("utf-8")).hexdigest()


def _extract_path(url: str) -> str:
    for base in SIGNATURE_BASE_URLS:
        if url.startswith(base):
            relative = url[len(base):]
            return "/" + relative.lstrip("/")
    parsed = urllib.parse.urlparse(url)
    return parsed.path


def _decode_jwt_claims(token: str) -> dict:
    import base64
    payload = token.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


class LynkCoAPI:
    """Handles communication with the Lynk & Co API."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str,
        refresh_token: str,
        device_id: str,
        on_token_refresh: callable | None = None,
    ) -> None:
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._device_id = device_id
        self._on_token_refresh = on_token_refresh
        # Fast polls, targeted refreshes and the regular poll share this client;
        # the lock keeps their 401 handling to a single refresh at a time.
        self._refresh_lock = asyncio.Lock()
        self._claims: dict = {}
        self._update_claims()

    def _update_claims(self) -> None:
        try:
            self._claims = _decode_jwt_claims(self._access_token)
        except Exception:
            _LOGGER.exception("Failed to decode JWT claims")
            self._claims = {}

    @property
    def customer_number(self) -> str:
        return self._claims.get("customerNumber", "")

    @property
    def snowflake_id(self) -> str:
        return self._claims.get("snowflakeId", "")

    @property
    def user_email(self) -> str:
        return self._claims.get("email", "")

    @property
    def access_token(self) -> str:
        return self._access_token

    @property
    def refresh_token(self) -> str:
        return self._refresh_token

    @property
    def device_id(self) -> str:
        return self._device_id

    def _build_headers(self, url: str) -> dict:
        nonce = str(uuid.uuid4())
        path = _extract_path(url)
        sig = _compute_signature(self.snowflake_id, nonce, path)

        return {
            "Authorization": f"Bearer {self._access_token}",
            "X-Auth-Token": self._access_token,
            "X-DeviceId": self._device_id,
            "X-CustomerNumber": self.customer_number,
            "X-CustomerId": self.snowflake_id,
            "User-Agent": "Android",
            "api-version": "1",
            "X-NONCE": nonce,
            "X-SIGNATURE-VERSION": SIGNATURE_VERSION,
            "X-SIGNATURE": sig,
            "X-App-Name": "Lynk&Co",
            "X-App-Version": "2.67.0",
            "X-App-Build-Number": "8455",
            "X-Device-OS-Version": "14",
            "X-Device-Model": "Pixel 8",
            "X-Device-Language": "en",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        headers = self._build_headers(url)
        token_used = self._access_token
        async with self._session.request(method, url, headers=headers, **kwargs) as resp:
            if resp.status == 401:
                _LOGGER.debug("Got 401, attempting token refresh")
                if not await self.refresh_tokens(stale_token=token_used):
                    raise LynkCoAuthError("Session expired and the refresh token was rejected")
                headers = self._build_headers(url)
                async with self._session.request(
                    method, url, headers=headers, **kwargs
                ) as retry:
                    if retry.status == 401:
                        raise LynkCoAuthError("Still unauthorized after refreshing the token")
                    retry.raise_for_status()
                    retry_data = await retry.json()
                    self._log_response(url, retry_data)
                    return retry_data if retry_data is not None else {}
            resp.raise_for_status()
            if resp.content_length == 0:
                return {}
            data = await resp.json()
            self._log_response(url, data)
            return data if data is not None else {}

    @staticmethod
    def _log_response(url: str, data: object) -> None:
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("API response %s: %s", mask_vins(_extract_path(url)), redact(data))

    async def refresh_tokens(self, stale_token: str | None = None) -> bool:
        """Get a new access token with the refresh token.

        Pass the access token that was rejected as ``stale_token``: if another
        caller already replaced it while this one waited for the lock, the new
        token is used as is instead of refreshing again. Without it, the
        refresh always runs.
        """
        async with self._refresh_lock:
            if stale_token is not None and self._access_token != stale_token:
                _LOGGER.debug("Token was already refreshed by another request")
                return True
            return await self._refresh_tokens_locked()

    async def _refresh_tokens_locked(self) -> bool:
        url = f"{LOGIN_B2C_URL}oauth2/v2.0/token"
        async with self._session.post(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "scope": SCOPE,
                "redirect_uri": REDIRECT_URI,
            },
        ) as resp:
            if resp.status != 200:
                _LOGGER.error("Token refresh failed: %d", resp.status)
                return False
            data = await resp.json()
            self._access_token = data["access_token"]
            self._refresh_token = data.get("refresh_token", self._refresh_token)
            self._update_claims()
            _log_token_lifetimes("Tokens refreshed", data)
            if self._on_token_refresh:
                self._on_token_refresh(self._access_token, self._refresh_token)
            return True

    async def validate_session(self) -> bool:
        url = f"{IAM_BASE}/validate-session"
        headers = self._build_headers(url)
        async with self._session.post(
            url,
            headers=headers,
            json={"deviceUuid": self._device_id, "isLogin": True},
        ) as resp:
            return resp.status == 200

    async def get_vehicles(self) -> list[dict]:
        url = f"{LOVE_BASE}/list/vehicles"
        data = await self._request("GET", url)
        return data.get("listOfVehicles", [])

    async def get_vehicle_data(self, vin: str) -> dict:
        return await self._request("GET", f"{LOVE_BASE}/vehicle/{vin}/vehicle_data")

    async def get_location(self, vin: str) -> dict:
        return await self._request("GET", f"{LOVE_BASE}/vehicle/{vin}/location_state")

    async def get_charge_state(self, vin: str) -> dict:
        return await self._request("GET", f"{LOVE_BASE}/vehicle/{vin}/charge_state")

    async def get_climate_state(self, vin: str) -> dict:
        return await self._request("GET", f"{LOVE_BASE}/vehicle/{vin}/climate_state")

    async def get_doors_windows(self, vin: str) -> dict:
        return await self._request(
            "GET", f"{LOVE_BASE}/vehicle/{vin}/doors_windows_state"
        )

    async def get_vehicle_metadata(self, vin: str) -> dict:
        return await self._request("GET", f"{LOVE_BASE}/vehicle/{vin}/vehicle_metadata")

    async def get_fuel_state(self, vin: str) -> dict:
        return await self._request("GET", f"{LOVE_BASE}/vehicle/{vin}/fuel_state")

    async def get_vehicle_guide(self, vin: str) -> str | None:
        """Return the car guide type, the most precise identity the API offers.

        Pre-2025 01s report their model year (CX11_2021_PHEV, CX11_2022,
        CX11_2023); newer cars report their equipment level (CX11_MORE,
        E335_CORE, DX11_MORE, ...).
        """
        data = await self._request("GET", f"{LOVE_BASE}/vehicle/{vin}/vehicle_guide")
        guide_type = data.get("type")
        return str(guide_type) if guide_type else None

    async def lock_door(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/door_lock"
        )

    async def unlock_door(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/door_unlock"
        )

    async def request_location(self, vin: str) -> dict:
        """Ask the vehicle to report its current position.

        Declared but never called by the official app (v2.63.0); verified working
        against the live API. The car answers COMMAND_RECEIVED immediately and
        pushes a fresh position a few seconds later.
        """
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/request_location"
        )

    async def flash_lights(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/flash_lights"
        )

    async def honk_horn(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/honk_horn"
        )

    async def open_sunroof(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/sun_roof_open"
        )

    async def close_sunroof(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/sun_roof_close"
        )

    async def set_charge_limit(self, vin: str, percent: int) -> dict:
        url = f"{COMMAND_BASE}/vehicle/{vin}/command/set_charge_limit"
        return await self._request("POST", url, params={"percent": percent})

    async def start_charging(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/charge_start"
        )

    async def stop_charging(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/charge_stop"
        )

    async def start_conditioning(self, vin: str, temp: int, level: int | None = None) -> dict:
        params = {"temp": temp}
        if level is not None:
            params["level"] = level
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/auto_conditioning_start",
            params=params,
        )

    async def stop_conditioning(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/auto_conditioning_stop"
        )

    async def start_ventilate(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/ventilate_start"
        )

    async def stop_ventilate(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/ventilate_stop"
        )

    async def start_heaters(self, vin: str, heaters: list[str]) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/start_heaters",
            json=heaters,
        )

    async def stop_heaters(self, vin: str, heaters: list[str]) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/stop_heaters",
            json=heaters,
        )

    async def lock_glovebox(self, vin: str, password: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/glovebox_set_password",
            json={"password": password},
        )

    async def unlock_glovebox(self, vin: str) -> dict:
        return await self._request(
            "POST", f"{COMMAND_BASE}/vehicle/{vin}/command/glovebox_unlock"
        )

    # --- Static helpers for the config flow auth ---

    @staticmethod
    def generate_auth_url() -> tuple[str, str, str]:
        """Generate B2C authorize URL. Returns (url, code_verifier, code_challenge)."""
        code_verifier, code_challenge = pkce.generate_pkce_pair()
        scope_encoded = urllib.parse.quote(SCOPE, safe="")
        redirect_encoded = urllib.parse.quote(REDIRECT_URI, safe="")
        url = (
            f"{LOGIN_B2C_URL}oauth2/v2.0/authorize"
            f"?response_type=code"
            f"&client_id={CLIENT_ID}"
            f"&scope={scope_encoded}"
            f"&code_challenge={code_challenge}"
            f"&code_challenge_method=S256"
            f"&redirect_uri={redirect_encoded}"
        )
        return url, code_verifier, code_challenge

    @staticmethod
    def parse_redirect(text: str) -> tuple[str | None, str | None]:
        """Find the authorization code in whatever the user pasted.

        Accepts the msauth:// redirect URL, a copied block of response headers
        that contains it, a bare query string, or just the code.
        Returns ``(code, error)``. ``error`` is the first line of B2C's error
        description when the login itself failed (e.g. it was cancelled).
        """
        text = text.strip()
        if match := _REDIRECT_RE.search(text):
            query = urllib.parse.urlsplit(match.group(0)).query
        elif token := next(
            (part for part in text.split() if "code=" in part or "error=" in part), None
        ):
            query = token.split("?", 1)[-1]
        else:
            return (text if _BARE_CODE_RE.fullmatch(text) else None), None

        params = urllib.parse.parse_qs(query)
        if code := params.get("code", [None])[0]:
            return code, None
        if error := params.get("error", [None])[0]:
            description = params.get("error_description", [error])[0]
            return None, description.strip().splitlines()[0]
        return None, None

    @staticmethod
    async def exchange_code(
        session: aiohttp.ClientSession, code: str, code_verifier: str
    ) -> dict | None:
        """Exchange authorization code for tokens."""
        url = f"{LOGIN_B2C_URL}oauth2/v2.0/token"
        async with session.post(
            url,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "client_info": "1",
                "scope": SCOPE,
                "code": code,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
                "redirect_uri": REDIRECT_URI,
                "client_id": CLIENT_ID,
            },
        ) as resp:
            if resp.status != 200:
                _LOGGER.error("Token exchange failed: %d", resp.status)
                return None
            data = await resp.json()
            _log_token_lifetimes("Logged in", data)
            return data
