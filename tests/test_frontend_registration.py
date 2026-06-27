"""Tests for the Lovelace card frontend registration in __init__.py."""

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry import (
    _CARD_URL,
    _FRONTEND_REGISTERED,
    _STATIC_URL,
    _async_register_frontend,
)
from never_dry.const import DOMAIN


@pytest.fixture
def frontend_stubs(monkeypatch):
    """Stub homeassistant.components.http and .frontend for the duration of a test.

    Returns the ``add_extra_js_url`` mock so tests can assert on it.
    """
    http_mod = ModuleType("homeassistant.components.http")
    http_mod.StaticPathConfig = lambda url, path, cache_headers=False: (url, path, cache_headers)

    frontend_mod = ModuleType("homeassistant.components.frontend")
    frontend_mod.add_extra_js_url = MagicMock()

    components_mod = sys.modules.get("homeassistant.components") or ModuleType("homeassistant.components")
    monkeypatch.setattr(components_mod, "frontend", frontend_mod, raising=False)

    monkeypatch.setitem(sys.modules, "homeassistant.components", components_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.components.http", http_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.components.frontend", frontend_mod)

    return frontend_mod.add_extra_js_url


def _make_hass():
    hass = MagicMock()
    hass.data = {DOMAIN: {}}
    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    return hass


async def test_registers_static_path_and_card(frontend_stubs):
    """Happy path: registers the static dir and adds the card JS URL once."""
    add_extra_js_url = frontend_stubs
    hass = _make_hass()

    await _async_register_frontend(hass)

    hass.http.async_register_static_paths.assert_awaited_once()
    add_extra_js_url.assert_called_once()
    called_url = add_extra_js_url.call_args.args[1]
    assert called_url.startswith(f"{_CARD_URL}?v=")
    assert hass.data[DOMAIN][_FRONTEND_REGISTERED] is True

    # The static path serves the bundled www/ directory under _STATIC_URL.
    static_arg = hass.http.async_register_static_paths.call_args.args[0][0]
    assert static_arg[0] == _STATIC_URL


async def test_skips_when_already_registered(frontend_stubs):
    """Early return when the frontend was already registered for this instance."""
    add_extra_js_url = frontend_stubs
    hass = _make_hass()
    hass.data[DOMAIN][_FRONTEND_REGISTERED] = True

    await _async_register_frontend(hass)

    hass.http.async_register_static_paths.assert_not_awaited()
    add_extra_js_url.assert_not_called()


async def test_does_not_raise_on_failure(frontend_stubs):
    """A frontend hiccup must never block integration setup."""
    hass = _make_hass()
    hass.http.async_register_static_paths.side_effect = RuntimeError("boom")

    # Should swallow the exception and leave the integration un-flagged.
    await _async_register_frontend(hass)

    assert hass.data[DOMAIN].get(_FRONTEND_REGISTERED) is not True
