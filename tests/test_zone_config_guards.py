"""Tests for the zone-config plausibility guards.

Unusual values (tiny area, flow rate that smells like a L/min-vs-L/h
mix-up) trigger a soft confirmation step instead of a hard error, and the
options flow offers a read-only "check zones" report for installations
configured before the guards existed.
"""

from unittest.mock import MagicMock

import pytest
from never_dry import config_flow as cf
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONES,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
    DELIVERY_MODE_VOLUME_PRESET,
    UNUSUAL_AREA_MIN_M2,
    UNUSUAL_FLOW_MAX_LPM,
    UNUSUAL_FLOW_MIN_LPM,
)


def _entry(zones):
    entry = MagicMock()
    entry.entry_id = "abc"
    entry.data = {CONF_ZONES: zones}
    return entry


@pytest.fixture(autouse=True)
def _patch_flow_env(monkeypatch):
    """Fill the gaps in the conftest HA stubs for flow-step tests.

    The stubbed ``homeassistant`` is not a package (no ``util.unit_system``),
    ``voluptuous``/``selector`` are empty modules, and the stub flow base
    classes have no ``async_show_form``/``async_create_entry``/``async_show_menu``.
    Patch just enough to drive the steps and inspect their results.
    """
    monkeypatch.setattr(cf, "_is_imperial", lambda hass: False)

    monkeypatch.setattr(cf.vol, "Schema", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(cf.vol, "Required", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(cf.vol, "Optional", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(cf, "_confirm_zone_schema", lambda: None)
    monkeypatch.setattr(cf, "_zone_schema_initial", lambda imperial: None)

    def _show_form(self, *, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {
            "type": "form",
            "step_id": step_id,
            "errors": errors,
            "description_placeholders": description_placeholders,
        }

    def _create_entry(self, *, data=None, title=None):
        return {"type": "create_entry", "title": title, "data": data}

    for klass in (cf.NeverDryConfigFlow, cf.NeverDryOptionsFlow):
        monkeypatch.setattr(klass, "async_show_form", _show_form, raising=False)
        monkeypatch.setattr(klass, "async_create_entry", _create_entry, raising=False)


class TestUnusualZoneValues:
    """The pure helper operating on metric zone dicts."""

    def test_plausible_zone_is_clean(self):
        zone = {CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 3.33}
        assert cf._unusual_zone_values(zone, imperial=False) == []

    def test_small_area_flagged(self):
        warnings = cf._unusual_zone_values({CONF_ZONE_AREA: 2.0}, imperial=False)
        assert len(warnings) == 1
        assert "m²" in warnings[0]

    def test_low_flow_flagged(self):
        zone = {CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 5.0 / 60.0}  # 5 L/h
        warnings = cf._unusual_zone_values(zone, imperial=False)
        assert len(warnings) == 1
        assert "L/h" in warnings[0]

    def test_high_flow_flagged(self):
        # The classic unit mix-up: 200 L/h typed where L/min is stored.
        zone = {CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 200.0}
        warnings = cf._unusual_zone_values(zone, imperial=False)
        assert len(warnings) == 1
        assert "12000" in warnings[0]  # 200 L/min shown as 12000 L/h

    def test_boundary_values_not_flagged(self):
        zone = {
            CONF_ZONE_AREA: UNUSUAL_AREA_MIN_M2,
            CONF_ZONE_FLOW_RATE: UNUSUAL_FLOW_MAX_LPM,
        }
        assert cf._unusual_zone_values(zone, imperial=False) == []
        zone[CONF_ZONE_FLOW_RATE] = UNUSUAL_FLOW_MIN_LPM
        assert cf._unusual_zone_values(zone, imperial=False) == []

    def test_missing_values_ignored(self):
        assert cf._unusual_zone_values({}, imperial=False) == []

    def test_imperial_messages_use_imperial_units(self):
        zone = {CONF_ZONE_AREA: 2.0, CONF_ZONE_FLOW_RATE: 200.0}
        warnings = cf._unusual_zone_values(zone, imperial=True)
        assert len(warnings) == 2
        assert "ft²" in warnings[0]
        assert "gal/h" in warnings[1]


class TestGuardFlowDeprecationWarning:
    """flow_meter/volume_preset without a guard flow rate get a deprecation notice."""

    def test_flow_meter_without_guard_flow_flagged(self):
        zone = {CONF_ZONE_AREA: 20.0, CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER}
        warnings = cf._unusual_zone_values(zone, imperial=False)
        assert len(warnings) == 1
        assert "guard flow rate" in warnings[0]
        assert "required" in warnings[0]

    def test_volume_preset_without_guard_flow_flagged(self):
        zone = {CONF_ZONE_AREA: 20.0, CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_VOLUME_PRESET}
        warnings = cf._unusual_zone_values(zone, imperial=False)
        assert len(warnings) == 1
        assert "guard flow rate" in warnings[0]

    def test_flow_meter_with_guard_flow_clean(self):
        zone = {
            CONF_ZONE_AREA: 20.0,
            CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
            CONF_ZONE_FLOW_RATE: 3.33,
        }
        assert cf._unusual_zone_values(zone, imperial=False) == []

    def test_estimated_flow_without_flow_rate_not_flagged(self):
        """estimated_flow missing flow_rate is a blocking form error, not a warning."""
        zone = {CONF_ZONE_AREA: 20.0, CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_ESTIMATED_FLOW}
        assert cf._unusual_zone_values(zone, imperial=False) == []

    def test_zero_flow_rate_flagged(self):
        zone = {
            CONF_ZONE_AREA: 20.0,
            CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
            CONF_ZONE_FLOW_RATE: 0.0,
        }
        warnings = cf._unusual_zone_values(zone, imperial=False)
        assert len(warnings) == 1
        assert "guard flow rate" in warnings[0]


class TestInitialFlowSoftConfirm:
    """Initial setup: zone step routes through confirm_zone on unusual input."""

    @pytest.mark.asyncio
    async def test_unusual_zone_asks_confirmation(self, hass_mock):
        flow = cf.NeverDryConfigFlow()
        flow.hass = hass_mock

        # Form units: area m², flow L/h. Both implausible.
        result = await flow.async_step_zone(
            {CONF_ZONE_NAME: "Vaso", CONF_ZONE_AREA: 1.0, CONF_ZONE_FLOW_RATE: 5.0},
        )

        assert result["step_id"] == "confirm_zone"
        assert flow._zones == []

    @pytest.mark.asyncio
    async def test_confirm_saves_zone_metric(self, hass_mock):
        flow = cf.NeverDryConfigFlow()
        flow.hass = hass_mock
        await flow.async_step_zone(
            {CONF_ZONE_NAME: "Vaso", CONF_ZONE_AREA: 1.0, CONF_ZONE_FLOW_RATE: 5.0},
        )

        result = await flow.async_step_confirm_zone({"confirm": True})

        assert result["step_id"] == "add_another"
        assert len(flow._zones) == 1
        assert flow._zones[0][CONF_ZONE_NAME] == "Vaso"
        # Stored metric: 5 L/h → L/min
        assert flow._zones[0][CONF_ZONE_FLOW_RATE] == pytest.approx(5.0 / 60.0)

    @pytest.mark.asyncio
    async def test_decline_returns_to_zone_form(self, hass_mock):
        flow = cf.NeverDryConfigFlow()
        flow.hass = hass_mock
        await flow.async_step_zone(
            {CONF_ZONE_NAME: "Vaso", CONF_ZONE_AREA: 1.0, CONF_ZONE_FLOW_RATE: 5.0},
        )

        result = await flow.async_step_confirm_zone({"confirm": False})

        assert result["step_id"] == "zone"
        assert flow._zones == []

    @pytest.mark.asyncio
    async def test_plausible_zone_skips_confirmation(self, hass_mock):
        flow = cf.NeverDryConfigFlow()
        flow.hass = hass_mock

        result = await flow.async_step_zone(
            {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 200.0},
        )

        assert result["step_id"] == "add_another"
        assert len(flow._zones) == 1


class TestOptionsFlowSoftConfirm:
    """Options flow: add/edit zone route through confirm_zone on unusual input."""

    @pytest.mark.asyncio
    async def test_add_zone_unusual_asks_confirmation(self, hass_mock):
        flow = cf.NeverDryOptionsFlow(_entry([]))
        flow.hass = hass_mock

        result = await flow.async_step_add_zone(
            {CONF_ZONE_NAME: "Vaso", CONF_ZONE_AREA: 1.0, CONF_ZONE_FLOW_RATE: 5.0},
        )

        assert result["step_id"] == "confirm_zone"
        hass_mock.config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_confirm_saves_added_zone(self, hass_mock):
        flow = cf.NeverDryOptionsFlow(_entry([]))
        flow.hass = hass_mock
        await flow.async_step_add_zone(
            {CONF_ZONE_NAME: "Vaso", CONF_ZONE_AREA: 1.0, CONF_ZONE_FLOW_RATE: 5.0},
        )

        result = await flow.async_step_confirm_zone({"confirm": True})

        assert result["type"] == "create_entry"
        saved = hass_mock.config_entries.async_update_entry.call_args.kwargs["data"]
        assert saved[CONF_ZONES][0][CONF_ZONE_NAME] == "Vaso"

    @pytest.mark.asyncio
    async def test_decline_returns_to_add_zone_form(self, hass_mock):
        flow = cf.NeverDryOptionsFlow(_entry([]))
        flow.hass = hass_mock
        await flow.async_step_add_zone(
            {CONF_ZONE_NAME: "Vaso", CONF_ZONE_AREA: 1.0, CONF_ZONE_FLOW_RATE: 5.0},
        )

        result = await flow.async_step_confirm_zone({"confirm": False})

        assert result["step_id"] == "add_zone"
        hass_mock.config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_edit_zone_unusual_asks_confirmation(self, hass_mock):
        existing = {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 3.33}
        flow = cf.NeverDryOptionsFlow(_entry([existing]))
        flow.hass = hass_mock
        flow._edit_zone_name = "Orto"

        # Edit introduces the L/min-vs-L/h mistake: 12000 L/h form value.
        result = await flow.async_step_edit_zone_detail(
            {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 12000.0},
        )

        assert result["step_id"] == "confirm_zone"
        hass_mock.config_entries.async_update_entry.assert_not_called()

        result = await flow.async_step_confirm_zone({"confirm": True})

        assert result["type"] == "create_entry"
        saved = hass_mock.config_entries.async_update_entry.call_args.kwargs["data"]
        assert saved[CONF_ZONES][0][CONF_ZONE_FLOW_RATE] == pytest.approx(200.0)


class TestCheckZonesReport:
    """Read-only audit of all configured zones from the options menu."""

    @pytest.mark.asyncio
    async def test_report_lists_only_unusual_zones(self, hass_mock):
        zones = [
            {CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 3.33},
            {CONF_ZONE_NAME: "Melograno", CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 200.0},
        ]
        flow = cf.NeverDryOptionsFlow(_entry(zones))
        flow.hass = hass_mock

        result = await flow.async_step_check_zones()

        assert result["step_id"] == "check_zones"
        placeholders = result["description_placeholders"]
        assert placeholders["zone_count"] == "2"
        assert placeholders["findings_count"] == "1"
        assert "Melograno" in placeholders["report"]
        assert "Orto" not in placeholders["report"]

    @pytest.mark.asyncio
    async def test_report_clean_when_all_plausible(self, hass_mock):
        zones = [{CONF_ZONE_NAME: "Orto", CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 3.33}]
        flow = cf.NeverDryOptionsFlow(_entry(zones))
        flow.hass = hass_mock

        result = await flow.async_step_check_zones()

        assert result["description_placeholders"]["findings_count"] == "0"

    @pytest.mark.asyncio
    async def test_submit_closes_flow_without_changes(self, hass_mock):
        flow = cf.NeverDryOptionsFlow(_entry([]))
        flow.hass = hass_mock

        result = await flow.async_step_check_zones({})

        assert result["type"] == "create_entry"
        hass_mock.config_entries.async_update_entry.assert_not_called()
