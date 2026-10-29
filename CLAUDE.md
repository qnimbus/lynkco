# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Home Assistant custom integration (`custom_components/lynkco/`, domain `lynkco`) for Lynk & Co vehicles (2025+ 01, 02, 08; pre-2025 01 partially). The API is reverse-engineered from the Lynk & Co Android app, so there is no official spec. Behaviour is inferred from the app and from owner reports. This repo is `qnimbus/ha_lynkco`, a fork of `b12e/ha_lynkco_2025` (where pull requests are switched off), published as a HACS custom repository. It keeps the domain `lynkco` on purpose, so it replaces the original as a drop-in. `manifest.json`, `const.ISSUE_TRACKER` and the issue templates point to the fork. Links to the wiki and to historical issues (e.g. #33) still point to b12e, because that content only exists there.

## Commands

There is no build step; Home Assistant loads the integration directly.

Dev environment (`.venv/`, Python 3.14; HA 2026.10 needs ≥ 3.14.2). The `uv` on this machine is a mise shim with no global version, so call it through `mise exec`:
```bash
mise exec uv@0.13.6 -- uv venv .venv --python 3.14.10
mise exec uv@0.13.6 -- uv pip install --python .venv/bin/python -r requirements_dev.txt
```
`requirements_test.txt` (used by CI) pins `pytest-homeassistant-custom-component`, which pins the matching `homeassistant` release, plus `ruff`. `requirements_dev.txt` includes it and adds two voice packages that a running HA needs. Those must be built from source, so install a C++ compiler first (`build-essential`).

- Lint: `scripts/lint` (ruff with `--fix`; rules in `.ruff.toml`, a moderate subset of HA core's). There's no formatter on purpose: the upstream code isn't formatted to a fixed width.
- Run tests: `.venv/bin/python -m pytest`
- Single test: `.venv/bin/python -m pytest tests/test_config_flow.py::test_user_step_shows_login_url`
- Local HA instance: `scripts/develop`, then open http://localhost:8123. On a fresh `config/` it completes onboarding through HA's onboarding API with an owner account `test`/`test` (set `HA_USER`/`HA_PASS` to change it, `CONFIG_DIR` to use another folder). Never delete `config/.storage`: it holds the user's accounts and integration entries. `config/` is gitignored because it holds tokens. Voice/ffmpeg errors in its log come from HA core, not from this integration. `config/custom_components/lynkco` is a symlink to the source, so restarting HA picks up code changes. `config/configuration.yaml` turns on debug logging for `custom_components.lynkco`. API responses are logged after passing through `redact()`.
- CI: `validate.yml` runs HACS validation and `hassfest`; `tests.yml` runs ruff and pytest. `manifest.json` (keys sorted after `domain` and `name`) and `strings.json`/translations must pass hassfest. HACS validation also checks the GitHub repo itself (description, topics, issues enabled). Releases: `release.yml` (manual, calendar versions `YYYY.M.N`) bumps `manifest.json` and creates the GitHub release that HACS installs.

Tests: `tests/conftest.py` enables custom integrations for the HA test harness (`hass` fixture). `pytest.ini` sets `asyncio_mode = auto` and puts the repo root on the import path, so tests import `custom_components.lynkco.*`. `tests/test_features.py` is the exception: it loads `features.py` by file path, so keep `features.py` free of module-level Home Assistant imports. Tests must not call the real Lynk & Co API. Mock `LynkCoAPI` (e.g. `exchange_code`, `validate_session`, `get_vehicles`) instead.

Each login logs out the account's other session (the phone app, or another HA instance). For repeated test logins, use a secondary Lynk & Co account that the car has been shared with.

## Architecture

### Auth flow (Azure AD B2C + PKCE, manual redirect paste)
- `const.py` holds the B2C constants: `CLIENT_ID`, `LOGIN_B2C_URL` (policy `b2c_1a_signin_mfa`), `SCOPE`, and the app's `msauth://` `REDIRECT_URI`. These come from the mobile app.
- `config_flow.py`, steps `user` → `auth_url` → `paste_redirect`:
  1. `LynkCoAPI.generate_auth_url()` builds the authorize URL with a PKCE pair. The `code_verifier` is kept on the flow instance.
  2. The user signs in in their browser (email, password, SMS MFA). The browser cannot follow the `msauth://` redirect, so the user copies the `Location` header from DevTools and pastes it back.
  3. `parse_redirect()` → `exchange_code()` → `validate_session()` → `get_vehicles()`. The entry's unique ID is the `email` claim from the access-token JWT. Each login gets a new random `device_id` (UUID), which is stored in the entry.
- `parse_redirect()` accepts the redirect URL, a pasted block of response headers, a query string, or a bare code. It also returns B2C's `error_description` when the redirect reports a failed login (form error `login_error`).
- The paste step always shows the login link again. A code that B2C rejects (`exchange_failed`) gets a new link with a fresh PKCE pair. A network error (`cannot_connect`) keeps the old link, because the code was never used.
- The login page has an hCaptcha. Don't try to automate the browser login.
- Reauth (`async_step_reauth`, started automatically when the session expires) and Reconfigure (`async_step_reconfigure`, from the entry's ⋮ menu) reuse the same steps. For both sources, `paste_redirect` checks that the account matches (`wrong_account`), then calls `async_update_reload_and_abort` with the new tokens and device ID. The entities and options stay.
- Lynk & Co allows only one logged-in device per account. Logging in through HA logs out the phone app, and the other way round, which invalidates the refresh token.

### Token lifecycle
- `api.LynkCoAPI` holds the access and refresh tokens. Request headers come from claims decoded from the JWT (`customerNumber`, `snowflakeId`). Every request is signed: `X-SIGNATURE = sha256(snowflakeId + nonce + path)`, where `path` is relative to one of `SIGNATURE_BASE_URLS`. The app version and device headers in `_build_headers` imitate the Android app.
- `_request` handles a 401 by calling `refresh_tokens()` once and retrying. If the refresh fails, or the retry also returns 401, it raises `LynkCoAuthError`.
- `refresh_tokens()` calls the `on_token_refresh` callback. `__init__.async_setup_entry` passes `_persist_tokens`, which writes the new tokens back into the config entry.
- Everything that ends in reauth:
  - `async_setup_entry`: `validate_session` fails and refresh fails, or `LynkCoAuthError` → `ConfigEntryAuthFailed`.
  - `coordinator._async_update_data`: when *every* endpoint fails, it tries one explicit refresh. `LynkCoAuthError` → `ConfigEntryAuthFailed`.
  - Fast polls and targeted refreshes run outside the update cycle, so they call `coordinator._start_reauth()` → `entry.async_start_reauth()`.
- One `LynkCoAPI` instance per config entry is shared by all vehicle coordinators, fast polls and targeted refreshes, which can run at the same time. `refresh_tokens()` runs under an `asyncio.Lock`. `_request` passes the token that got the 401 as `stale_token`, and a caller whose token was already replaced while it waited reuses the new one instead of refreshing again. Calling `refresh_tokens()` without `stale_token` always refreshes.

### Data and entities
- `__init__.py` creates one `LynkCoCoordinator` per VIN and stores it under `hass.data[DOMAIN][entry_id] = {"api", "coordinators"}`. It also registers all `lynkco.*` services (`services.yaml`) once, and creates the Repairs issue for unknown models.
- `coordinator.py`: a full snapshot of several endpoints (`vehicle_data`, `location`, `charge`, `climate`, `doors`, `metadata`, and `fuel` for non-BEVs) every 15 min. A failing endpoint is isolated: its last value is kept and the error goes in `endpoint_errors`, which makes the tied entities unavailable. Fast polling (about 60 s) runs only for driving or climate endpoints while they are active. After a command, `async_targeted_refresh` re-fetches only the related endpoint (checks at 3, 5 and 10 s).
- `features.py` and `models/*.json` decide which remote actions are enabled per car. The lookup order is `models/<GUIDE_TYPE>.json`, then `models/<MODEL_CODE>.json`, then `_default.json`. A model file lists only what differs from the default. Capabilities the API reports itself (BEV/PHEV, heater zones, glovebox) are detected at runtime and are not stored in these files. Model and guide-type display names are in `const.py`.
- Platform files (`sensor.py`, `binary_sensor.py`, `lock.py`, `climate.py`, …) subclass the base entity in `entity.py` and read `coordinator.data[<endpoint>]`, usually through `coordinator.dig()`.
- `diagnostics.py` and `api.redact()` redact anything matching `REDACT_KEY_PARTS`. Add new sensitive keys there.
- Never log a raw VIN. Use `coordinator.log_id` (or `api.mask_vin(vin)`, last 4 characters). Pass error texts through `api.mask_vins()`, because aiohttp errors contain the request URL, which includes the VIN.

### Translations
User-facing config-flow strings and errors (`no_code`, `login_error`, `exchange_failed`, `cannot_connect`, `no_token`, `session_failed`, `no_vehicles`, `wrong_account`, …) are defined in `strings.json`. Mirror them in `translations/en.json` and the other languages (de, es, fr, nl).

### Polling limits
The default intervals are deliberately also the minimums (options flow: scan ≥ 15 min, driving ≥ 1 min) to avoid overloading Lynk & Co's backend. Don't lower them.
