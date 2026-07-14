"""Shared flow-meter reading helpers.

Used by both the controller (delivery loops) and the zone sensors
(expected-duration estimation) so rate detection, unit normalization
and unavailable-state handling stay identical everywhere.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant

GALLON_TO_LITER = 3.785411784  # US liquid gallon → liters

_RATE_UNITS = ("l/h", "l/min", "m³/h", "gal/min", "gal/h")


def get_flow_meter_unit(hass: HomeAssistant, entity_id: str) -> str | None:
    """Get the unit of measurement of a flow meter sensor."""
    state = hass.states.get(entity_id)
    if state is None:
        return None
    return state.attributes.get("unit_of_measurement")


def is_flow_rate_sensor(hass: HomeAssistant, entity_id: str) -> bool:
    """Check if the sensor reports a flow rate (not cumulative volume).

    Recognizes both metric (L/h, L/min, m³/h) and imperial (gal/min,
    gal/h) units. When Home Assistant runs in US-customary mode, ZHA
    flow sensors are exposed in gallons, so these must be detected too.
    """
    unit = (get_flow_meter_unit(hass, entity_id) or "").lower()
    return unit in _RATE_UNITS


def read_flow_meter(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read the current value of a flow meter sensor."""
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def rate_to_lpm(rate: float, unit: str | None) -> float:
    """Normalize a flow-rate reading to liters per minute.

    Handles metric (L/min, L/h, m³/h) and imperial (gal/min, gal/h)
    units. When HA runs in US-customary mode the underlying ZHA sensor
    is exposed in gallons, so the raw value must be converted before it
    is integrated into a delivered volume.
    """
    u = (unit or "").lower()
    if u == "l/min":
        return rate
    if u == "l/h":
        return rate / 60.0
    if u == "m³/h":
        return rate * 1000.0 / 60.0
    if u == "gal/min":
        return rate * GALLON_TO_LITER
    if u == "gal/h":
        return rate * GALLON_TO_LITER / 60.0
    # Unknown unit: assume already L/h (legacy default).
    return rate / 60.0


def volume_to_liters(value: float, unit: str | None) -> float:
    """Normalize a cumulative-volume reading to liters.

    Converts gallons → liters when the sensor is exposed in US-customary
    units; passes metric volumes through unchanged.
    """
    u = (unit or "").lower()
    if u in ("gal", "gallon", "gallons"):
        return value * GALLON_TO_LITER
    if u == "m³":
        return value * 1000.0
    return value


def read_volume_liters(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a cumulative-volume sensor and normalize to liters in one fetch.

    Reads value and unit from a single ``states.get`` so the result is
    consistent and imperial (gallons) readings are converted to liters.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        value = float(state.state)
    except (ValueError, TypeError):
        return None
    return volume_to_liters(value, state.attributes.get("unit_of_measurement"))


def read_flow_rate_lpm(hass: HomeAssistant, entity_id: str) -> float | None:
    """Read a live flow rate normalized to L/min, or ``None``.

    Returns ``None`` when the entity is not a rate sensor (cumulative
    volume meters have no instantaneous rate), is unavailable, or reads
    zero/negative — callers use ``None`` to fall back to the configured
    guard flow rate.
    """
    if not is_flow_rate_sensor(hass, entity_id):
        return None
    raw = read_flow_meter(hass, entity_id)
    if raw is None or raw <= 0:
        return None
    return rate_to_lpm(raw, get_flow_meter_unit(hass, entity_id))
