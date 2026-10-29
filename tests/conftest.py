"""Shared fixtures for tests that run inside a Home Assistant test instance."""

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/lynkco in every test."""
    yield
