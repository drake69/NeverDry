"""A zone must not be judged by another zone's water meter.

Field case, 2026-09-09: while fixing an unrelated problem the flow meter of
"Giardino Melograno" was set to the counter belonging to "Giardino Melino".
Melino irrigates at 06:00, so at 22:00 its counter sat still, the open guard
saw no flow, and every session of Melograno failed while the valve watered
normally. Nothing in the UI said the two belonged to different devices.

A meter on a different device is not wrong by itself: an in-line flow meter
installed on the pipe is a legitimate setup, and NeverDry consumes whatever
entity the user points at. What is almost certainly a mistake is pointing a
zone at the meter of *another configured zone's valve*, and that is what these
guards catch. A warning, not a rejection: the boundary rule is that NeverDry
consumes HA entities and does not dictate the plumbing.
"""

import pytest
from never_dry import config_flow as cf
from never_dry.const import (
    CONF_ZONE_AREA,
    CONF_ZONE_DELIVERY_MODE,
    CONF_ZONE_FLOW_METER_SENSOR,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_NAME,
    CONF_ZONE_VALVE,
    DELIVERY_MODE_ESTIMATED_FLOW,
    DELIVERY_MODE_FLOW_METER,
)

#: Entity ids with nothing zone-like in them, for the naming-independence tests.
OPAQUE_DEVICES = {
    "switch.0x00124b0022ab": "dev_a",
    "sensor.0x00124b0022ab_volume": "dev_a",
    "switch.0x00124b0099ff": "dev_b",
    "sensor.0x00124b0099ff_volume": "dev_b",
}


@pytest.fixture
def _flow_env(monkeypatch):
    """Fill the gaps in the conftest HA stubs for driving flow steps.

    Same shim as tests/test_zone_config_guards.py: the stubbed
    ``homeassistant`` is not a package and the stub flow base classes have no
    ``async_show_form``. Patch just enough to run the steps and read back
    where they routed.
    """
    monkeypatch.setattr(cf, "_is_imperial", lambda hass: False)
    monkeypatch.setattr(cf.vol, "Schema", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(cf.vol, "Required", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(cf.vol, "Optional", lambda *a, **k: object(), raising=False)
    monkeypatch.setattr(cf, "_confirm_zone_schema", lambda: None)
    monkeypatch.setattr(cf, "_zone_schema_initial", lambda imperial, current=None: None)

    def _show_form(self, *, step_id, data_schema=None, errors=None, description_placeholders=None):
        return {"type": "form", "step_id": step_id, "errors": errors}

    def _create_entry(self, *, data=None, title=None):
        return {"type": "create_entry", "title": title, "data": data}

    for klass in (cf.NeverDryConfigFlow, cf.NeverDryOptionsFlow):
        monkeypatch.setattr(klass, "async_show_form", _show_form, raising=False)
        monkeypatch.setattr(klass, "async_create_entry", _create_entry, raising=False)


MELOGRANO_VALVE = "switch.giardino_melograno"
MELOGRANO_METER = "sensor.giardino_melograno_real_time_irrigation_volume"
MELINO_VALVE = "switch.giardino_melino"
MELINO_METER = "sensor.giardino_melino_real_time_irrigation_volume"
INLINE_METER = "sensor.main_pipe_flow"

#: entity -> device, as the entity registry would resolve it.
DEVICES = {
    MELOGRANO_VALVE: "dev_melograno",
    MELOGRANO_METER: "dev_melograno",
    MELINO_VALVE: "dev_melino",
    MELINO_METER: "dev_melino",
    INLINE_METER: "dev_pipe_sensor",
}


def _zone(name, valve, meter=None, mode=DELIVERY_MODE_FLOW_METER):
    zone = {CONF_ZONE_NAME: name, CONF_ZONE_VALVE: valve, CONF_ZONE_DELIVERY_MODE: mode}
    if meter is not None:
        zone[CONF_ZONE_FLOW_METER_SENSOR] = meter
    return zone


OTHER_ZONES = [_zone("Giardino Melino", MELINO_VALVE, MELINO_METER)]


class TestAMeterBelongingToAnotherZoneIsFlagged:
    def test_the_field_case_is_caught(self):
        """T13: Melograno pointed at Melino's counter."""
        zone = _zone("Giardino Melograno", MELOGRANO_VALVE, MELINO_METER)
        warnings = cf.meter_ownership_warnings(zone, OTHER_ZONES, DEVICES.get)
        assert warnings, "a meter owned by another zone's valve must be reported"
        assert "Melino" in warnings[0], "the warning must name the zone it belongs to"

    def test_the_zones_own_meter_is_silent(self):
        zone = _zone("Giardino Melograno", MELOGRANO_VALVE, MELOGRANO_METER)
        assert cf.meter_ownership_warnings(zone, OTHER_ZONES, DEVICES.get) == []

    def test_an_inline_meter_on_its_own_device_is_allowed(self):
        """Not every separate device is a mistake: a pipe meter is a real setup.

        Warning on this would train the user to ignore the warning that matters.
        """
        zone = _zone("Giardino Melograno", MELOGRANO_VALVE, INLINE_METER)
        assert cf.meter_ownership_warnings(zone, OTHER_ZONES, DEVICES.get) == []

    def test_an_unresolvable_entity_is_not_an_accusation(self):
        """A registry that cannot answer is not evidence of a wrong meter."""
        zone = _zone("Giardino Melograno", MELOGRANO_VALVE, "sensor.unknown_to_the_registry")
        assert cf.meter_ownership_warnings(zone, OTHER_ZONES, lambda _e: None) == []


class TestAMeteredModeNeedsAMeter:
    def test_flow_meter_without_a_meter_is_reported(self):
        """T14: the mode measures volume, so the measuring device is not optional."""
        zone = _zone("Giardino Melograno", MELOGRANO_VALVE, meter=None)
        warnings = cf.meter_ownership_warnings(zone, [], DEVICES.get)
        assert warnings
        assert "flow meter" in warnings[0].lower()

    def test_estimated_flow_without_a_meter_is_perfectly_normal(self):
        """The mode whose contract is 'I measure nothing' must stay silent."""
        zone = _zone("Giardino Melograno", MELOGRANO_VALVE, meter=None, mode=DELIVERY_MODE_ESTIMATED_FLOW)
        assert cf.meter_ownership_warnings(zone, [], DEVICES.get) == []


class TestTheCheckDoesNotDependOnNaming:
    """Ownership is decided by the device registry, never by string matching.

    Verified against the field registry on 2026-09-09: for all four zones the
    valve entity and its volume counter share one device_id. The clearest case
    is a zone whose device is named "Vavola Irrigazione Ortensia" (the typo is
    the user's) while its entities are named "giardino_ortensia_*": the names
    do not match each other, and the device does. A check that compared names
    would have missed that zone and every zone named after the plant rather
    than the hardware.

    So the entities below are deliberately opaque: nothing in them names a zone.
    """

    def test_another_zones_meter_is_caught_without_any_name_clue(self):
        zone = _zone("Roses", "switch.0x00124b0022ab", "sensor.0x00124b0099ff_volume")
        others = [_zone("Lawn", "switch.0x00124b0099ff", "sensor.0x00124b0099ff_volume")]
        warnings = cf.meter_ownership_warnings(zone, others, OPAQUE_DEVICES.get)
        assert warnings
        assert "Lawn" in warnings[0]

    def test_its_own_meter_is_silent_without_any_name_clue(self):
        zone = _zone("Roses", "switch.0x00124b0022ab", "sensor.0x00124b0022ab_volume")
        others = [_zone("Lawn", "switch.0x00124b0099ff", "sensor.0x00124b0099ff_volume")]
        assert cf.meter_ownership_warnings(zone, others, OPAQUE_DEVICES.get) == []


class TestTheWarningReachesTheUserAndCanBeOverridden:
    """The guard joins the existing soft-confirm, it does not add a new gate.

    Two things are being tested at once, and both matter:

    - the warning actually reaches the form. A pure function nobody calls would
      pass every test above and change nothing for the user;
    - the user can proceed anyway. Ownership is a strong hint, never proof: two
      zones fed by one physical valve, or a meter deliberately shared, are the
      user's business. A guard that cannot be overridden would make a legitimate
      installation impossible to configure, which is worse than the mistake it
      prevents.
    """

    @pytest.fixture
    def _flow(self, hass_mock, monkeypatch, _flow_env):
        monkeypatch.setattr(cf, "_device_resolver", lambda _hass: OPAQUE_DEVICES.get)
        flow = cf.NeverDryConfigFlow()
        flow.hass = hass_mock
        flow._zones = [_zone("Lawn", "switch.0x00124b0099ff", "sensor.0x00124b0099ff_volume")]
        return flow

    @pytest.mark.asyncio
    async def test_it_routes_to_the_confirmation_step(self, _flow):
        result = await _flow.async_step_zone(
            {
                CONF_ZONE_NAME: "Roses",
                CONF_ZONE_VALVE: "switch.0x00124b0022ab",
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.0x00124b0099ff_volume",
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_AREA: 15.0,
                CONF_ZONE_FLOW_RATE: 264.0,
            },
        )
        assert result["step_id"] == "confirm_zone"
        assert any("Lawn" in w for w in _flow._pending_warnings)
        assert len(_flow._zones) == 1, "nothing saved until the user answers"

    @pytest.mark.asyncio
    async def test_confirming_saves_the_zone_anyway(self, _flow):
        await _flow.async_step_zone(
            {
                CONF_ZONE_NAME: "Roses",
                CONF_ZONE_VALVE: "switch.0x00124b0022ab",
                CONF_ZONE_FLOW_METER_SENSOR: "sensor.0x00124b0099ff_volume",
                CONF_ZONE_DELIVERY_MODE: DELIVERY_MODE_FLOW_METER,
                CONF_ZONE_AREA: 15.0,
                CONF_ZONE_FLOW_RATE: 264.0,
            },
        )

        await _flow.async_step_confirm_zone({"confirm": True})

        assert [z[CONF_ZONE_NAME] for z in _flow._zones] == ["Lawn", "Roses"]
