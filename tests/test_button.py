"""Tests for MarkIrrigatedButton entities."""

from unittest.mock import AsyncMock

import pytest
from never_dry.button import MarkIrrigatedButton, _create_buttons
from never_dry.const import (
    ATTR_ZONE_NAME,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DOMAIN,
    SERVICE_MARK_IRRIGATED,
)


class TestButtonCreation:
    """Test button entity creation."""

    def test_creates_one_button_per_zone(self, hass_mock):
        config = {
            CONF_ZONES: [
                {CONF_ZONE_NAME: "Orto"},
                {CONF_ZONE_NAME: "Prato"},
            ]
        }
        buttons = _create_buttons(hass_mock, config)
        assert len(buttons) == 2

    def test_no_buttons_without_zones(self, hass_mock):
        buttons = _create_buttons(hass_mock, {})
        assert len(buttons) == 0

    def test_no_buttons_empty_zones(self, hass_mock):
        buttons = _create_buttons(hass_mock, {CONF_ZONES: []})
        assert len(buttons) == 0


class TestButtonProperties:
    """Test button entity attributes."""

    def test_name(self, hass_mock):
        btn = MarkIrrigatedButton(hass_mock, "Orto")
        assert btn._attr_name == "Mark Orto irrigated"

    def test_unique_id(self, hass_mock):
        btn = MarkIrrigatedButton(hass_mock, "Orto")
        assert btn._attr_unique_id == "mark_irrigated_orto"

    def test_unique_id_with_spaces(self, hass_mock):
        btn = MarkIrrigatedButton(hass_mock, "Vegetable Garden")
        assert btn._attr_unique_id == "mark_irrigated_vegetable_garden"

    def test_icon(self, hass_mock):
        btn = MarkIrrigatedButton(hass_mock, "Orto")
        assert btn._attr_icon == "mdi:water-check"


class TestButtonPress:
    """Test button press behavior."""

    @pytest.mark.asyncio
    async def test_press_calls_mark_irrigated_service(self, hass_mock):
        hass_mock.services.async_call = AsyncMock()
        btn = MarkIrrigatedButton(hass_mock, "Orto")

        await btn.async_press()

        hass_mock.services.async_call.assert_called_once_with(
            DOMAIN,
            SERVICE_MARK_IRRIGATED,
            {ATTR_ZONE_NAME: "Orto"},
        )

    @pytest.mark.asyncio
    async def test_press_passes_correct_zone_name(self, hass_mock):
        hass_mock.services.async_call = AsyncMock()
        btn = MarkIrrigatedButton(hass_mock, "Vegetable Garden")

        await btn.async_press()

        call_args = hass_mock.services.async_call.call_args
        assert call_args.args[2][ATTR_ZONE_NAME] == "Vegetable Garden"
