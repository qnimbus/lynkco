"""Config flow tests. The Lynk & Co API is mocked; nothing goes over the network."""

from collections.abc import Iterator
from unittest.mock import AsyncMock, patch

import aiohttp
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.lynkco.api import LynkCoAPI
from custom_components.lynkco.const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    DOMAIN,
)

from .test_api import CODE, REDIRECT, make_jwt

EMAIL = "owner@example.com"
TOKENS = {"access_token": make_jwt({"email": EMAIL}), "refresh_token": "new-refresh"}


@pytest.fixture
def api() -> Iterator[dict[str, AsyncMock]]:
    """Mock the API calls the flow makes, and the setup that follows."""
    mocks = {
        "exchange_code": AsyncMock(return_value=TOKENS),
        "validate_session": AsyncMock(return_value=True),
        "get_vehicles": AsyncMock(return_value=[{"vehicle": {"vin": "VIN1"}}]),
    }
    with (
        patch.object(LynkCoAPI, "exchange_code", mocks["exchange_code"]),
        patch.object(LynkCoAPI, "validate_session", mocks["validate_session"]),
        patch.object(LynkCoAPI, "get_vehicles", mocks["get_vehicles"]),
        patch("custom_components.lynkco.async_setup_entry", return_value=True),
    ):
        yield mocks


async def start_flow(hass: HomeAssistant) -> dict:
    """Open the flow and click Next on the login-link step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


async def paste(hass: HomeAssistant, result: dict, text: str) -> dict:
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"redirect_url": text}
    )


async def test_user_step_shows_login_url(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "auth_url"
    assert "login.lynkco.com" in result["description_placeholders"]["auth_url"]
    assert "code_challenge=" in result["description_placeholders"]["auth_url"]


async def test_paste_step_repeats_the_login_url(hass: HomeAssistant) -> None:
    first = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(first["flow_id"], {})
    assert result["step_id"] == "paste_redirect"
    assert result["description_placeholders"]["auth_url"] == (
        first["description_placeholders"]["auth_url"]
    )


async def test_paste_without_code_shows_error(hass: HomeAssistant) -> None:
    result = await paste(hass, await start_flow(hass), "not a redirect")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"redirect_url": "no_code"}


@pytest.mark.parametrize(
    "pasted",
    [
        f"{REDIRECT}?code={CODE}&state=s",
        f"HTTP/2 302\r\nlocation: {REDIRECT}?code={CODE}\r\nx-ms-request-id: 1\r\n",
        CODE,
    ],
    ids=["url", "headers", "bare-code"],
)
async def test_valid_paste_creates_entry(
    hass: HomeAssistant, api: dict[str, AsyncMock], pasted: str
) -> None:
    result = await paste(hass, await start_flow(hass), pasted)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == f"Lynk & Co ({EMAIL})"
    assert result["result"].unique_id == EMAIL
    assert result["data"][CONF_ACCESS_TOKEN] == TOKENS["access_token"]
    assert result["data"][CONF_REFRESH_TOKEN] == "new-refresh"
    assert result["data"][CONF_DEVICE_ID]
    assert api["exchange_code"].call_args.args[1] == CODE


async def test_rejected_code_gives_a_new_login_link(
    hass: HomeAssistant, api: dict[str, AsyncMock]
) -> None:
    api["exchange_code"].return_value = None
    result = await start_flow(hass)
    old_link = result["description_placeholders"]["auth_url"]

    result = await paste(hass, result, CODE)
    assert result["errors"] == {"redirect_url": "exchange_failed"}
    new_link = result["description_placeholders"]["auth_url"]
    assert new_link != old_link

    # The next attempt uses the verifier that belongs to the new link.
    first_verifier = api["exchange_code"].call_args.args[2]
    api["exchange_code"].return_value = TOKENS
    result = await paste(hass, result, CODE)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert api["exchange_code"].call_args.args[2] != first_verifier


async def test_network_error_keeps_the_login_link(
    hass: HomeAssistant, api: dict[str, AsyncMock]
) -> None:
    api["exchange_code"].side_effect = aiohttp.ClientConnectionError("down")
    result = await start_flow(hass)
    link = result["description_placeholders"]["auth_url"]

    result = await paste(hass, result, CODE)
    assert result["errors"] == {"base": "cannot_connect"}
    assert result["description_placeholders"]["auth_url"] == link


async def test_failed_login_shows_the_b2c_message(
    hass: HomeAssistant, api: dict[str, AsyncMock]
) -> None:
    result = await paste(
        hass,
        await start_flow(hass),
        f"{REDIRECT}?error=access_denied&error_description=AADB2C90091%3a+Cancelled.%0d%0aCorrelation+ID%3a+1",
    )
    assert result["errors"] == {"redirect_url": "login_error"}
    assert result["description_placeholders"]["login_error"] == "AADB2C90091: Cancelled."
    api["exchange_code"].assert_not_called()


async def test_reauth_updates_the_existing_entry(
    hass: HomeAssistant, api: dict[str, AsyncMock]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data={CONF_ACCESS_TOKEN: "old", CONF_REFRESH_TOKEN: "old", CONF_DEVICE_ID: "old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "auth_url"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await paste(hass, result, CODE)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == TOKENS["access_token"]
    assert entry.data[CONF_REFRESH_TOKEN] == "new-refresh"
    assert entry.data[CONF_DEVICE_ID] != "old"


async def test_reauth_with_another_account_is_refused(
    hass: HomeAssistant, api: dict[str, AsyncMock]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="someone.else@example.com",
        data={CONF_ACCESS_TOKEN: "old", CONF_REFRESH_TOKEN: "old", CONF_DEVICE_ID: "old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await paste(hass, result, CODE)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ACCESS_TOKEN] == "old"


async def test_reconfigure_logs_in_again(
    hass: HomeAssistant, api: dict[str, AsyncMock]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data={CONF_ACCESS_TOKEN: "old", CONF_REFRESH_TOKEN: "old", CONF_DEVICE_ID: "old"},
        options={"scan_interval": 30},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "auth_url"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await paste(hass, result, CODE)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ACCESS_TOKEN] == TOKENS["access_token"]
    assert entry.data[CONF_REFRESH_TOKEN] == "new-refresh"
    assert entry.options == {"scan_interval": 30}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_reconfigure_with_another_account_is_refused(
    hass: HomeAssistant, api: dict[str, AsyncMock]
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="someone.else@example.com",
        data={CONF_ACCESS_TOKEN: "old", CONF_REFRESH_TOKEN: "old", CONF_DEVICE_ID: "old"},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await paste(hass, result, CODE)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert entry.data[CONF_ACCESS_TOKEN] == "old"
