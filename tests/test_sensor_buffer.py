"""Tests for SensorBuffer (AI-058) — rolling median for ET input robustness."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

# ── SensorBuffer unit tests ───────────────────────────────────────────


class TestSensorBufferRejection:
    """Invalid inputs must be rejected (not push 0 or raise)."""

    def test_rejects_unavailable_string(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        assert buf.push("unavailable") is False
        assert len(buf) == 0

    def test_rejects_unknown_string(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        assert buf.push("unknown") is False

    def test_rejects_none(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        assert buf.push(None) is False

    def test_rejects_nan(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        assert buf.push(float("nan")) is False

    def test_rejects_inf(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        assert buf.push(float("inf")) is False
        assert buf.push(float("-inf")) is False

    def test_rejects_non_numeric_string(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        assert buf.push("hello") is False

    def test_rejects_below_valid_range(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10, valid_range=(-50.0, 70.0))
        assert buf.push(-51.0) is False

    def test_rejects_above_valid_range(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10, valid_range=(-50.0, 70.0))
        assert buf.push(71.0) is False

    def test_accepts_boundary_values(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10, valid_range=(-50.0, 70.0))
        assert buf.push(-50.0) is True
        assert buf.push(70.0) is True
        assert len(buf) == 2


class TestSensorBufferMedian:
    """Median calculation correctness."""

    def test_median_single_value(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        buf.push(25.0)
        assert buf.median() == pytest.approx(25.0)

    def test_median_odd_count(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        for v in [10.0, 20.0, 30.0]:
            buf.push(v)
        assert buf.median() == pytest.approx(20.0)

    def test_median_even_count(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        for v in [10.0, 20.0, 30.0, 40.0]:
            buf.push(v)
        assert buf.median() == pytest.approx(25.0)

    def test_median_with_outlier_clamped_by_range(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10, valid_range=(-50.0, 70.0))
        for _ in range(9):
            buf.push(25.0)
        buf.push(71.0)  # rejected — stays at 9 readings of 25.0
        assert buf.median() == pytest.approx(25.0)
        assert len(buf) == 9

    def test_median_returns_none_when_empty(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        assert buf.median() is None

    def test_median_respects_min_readings(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        buf.push(25.0)
        buf.push(26.0)
        assert buf.median(min_readings=3) is None
        buf.push(27.0)
        assert buf.median(min_readings=3) == pytest.approx(26.0)

    def test_rolling_window_evicts_oldest(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(3)
        buf.push(10.0)
        buf.push(20.0)
        buf.push(30.0)
        buf.push(40.0)  # evicts 10.0
        assert 10.0 not in sorted(buf._buf)
        assert buf.median() == pytest.approx(30.0)

    def test_accepts_numeric_string(self):
        from never_dry.sensor import SensorBuffer

        buf = SensorBuffer(10)
        assert buf.push("25.5") is True
        assert buf.median() == pytest.approx(25.5)


# ── ET model robustness (AI-058) ─────────────────────────────────────


class TestETRobustness:
    """ET / deficit must not collapse to 0 on a single invalid reading."""

    def _make_sensor_with_temp(self, hass_mock, base_config, temp_value):
        from never_dry.sensor import DrynessIndexSensor

        sensor = DrynessIndexSensor(hass_mock, base_config)
        hass_mock.states.get.side_effect = lambda eid: MagicMock(state=str(temp_value))
        sensor._last_update = datetime.now() - timedelta(hours=1)
        sensor._on_sensor_change(MagicMock())
        return sensor

    def test_single_unavailable_does_not_zero_deficit(self, hass_mock, base_config, make_state):
        """One unavailable reading must not reset accumulated deficit to 0."""
        from never_dry.sensor import DrynessIndexSensor

        sensor = DrynessIndexSensor(hass_mock, base_config)

        # Build up valid readings
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(25.0),
            "sensor.rain": make_state(0.0),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(hours=2)
        sensor._on_sensor_change(MagicMock())
        deficit_before = sensor._deficit
        assert deficit_before > 0

        # Now temp becomes unavailable
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state("unavailable"),
            "sensor.rain": make_state(0.0),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(hours=1)
        sensor._on_sensor_change(MagicMock())

        # Deficit must have grown or stayed — NOT zeroed
        assert sensor._deficit >= deficit_before

    def test_alternating_valid_unavailable_keeps_deficit_growing(self, hass_mock, base_config, make_state):
        """Alternating valid/unavailable readings must still accumulate deficit."""
        from never_dry.sensor import DrynessIndexSensor

        sensor = DrynessIndexSensor(hass_mock, base_config)
        for i in range(10):
            temp = "25.0" if i % 2 == 0 else "unavailable"
            hass_mock.states.get.side_effect = lambda eid, t=temp: {
                "sensor.temperature": make_state(t),
                "sensor.rain": make_state(0.0),
            }[eid]
            sensor._last_update = datetime.now() - timedelta(hours=1)
            sensor._on_sensor_change(MagicMock())

        assert sensor._deficit > 0

    def test_spike_rejection_keeps_median_stable(self, hass_mock, base_config, make_state):
        """A temperature spike outside valid range does not affect the ET median."""
        from never_dry.const import ET_TEMP_VALID_RANGE
        from never_dry.sensor import DrynessIndexSensor

        sensor = DrynessIndexSensor(hass_mock, base_config)
        normal_temp = 25.0

        for _ in range(5):
            hass_mock.states.get.side_effect = lambda eid: {
                "sensor.temperature": make_state(normal_temp),
                "sensor.rain": make_state(0.0),
            }[eid]
            sensor._last_update = datetime.now() - timedelta(hours=1)
            sensor._on_sensor_change(MagicMock())

        deficit_before_spike = sensor._deficit

        spike = ET_TEMP_VALID_RANGE[1] + 10  # above valid max
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(spike),
            "sensor.rain": make_state(0.0),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(hours=1)
        sensor._on_sensor_change(MagicMock())

        # Spike rejected → median stays at normal_temp → deficit increases normally
        assert sensor._deficit > deficit_before_spike

    def test_no_update_when_buffer_empty(self, hass_mock, base_config, make_state):
        """Deficit stays frozen when buffer has no valid readings (all unavailable)."""
        from never_dry.sensor import DrynessIndexSensor

        sensor = DrynessIndexSensor(hass_mock, base_config)
        assert sensor._deficit == 0.0

        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state("unavailable"),
            "sensor.rain": make_state(0.0),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(hours=1)
        sensor._on_sensor_change(MagicMock())

        assert sensor._deficit == 0.0  # frozen, not negative or erroneous


# ── _discover_hw_max_duration ─────────────────────────────────────────


class TestDiscoverHwMaxDuration:
    """Unit tests for the entity-registry based hw-timer discovery."""

    def _patch_er(self, switch_entry, entries):
        """Context manager that stubs entity registry via sys.modules patches."""
        import sys
        from unittest.mock import MagicMock

        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        ent_reg = MagicMock()
        ent_reg.async_get.return_value = switch_entry
        er_mod.async_get = MagicMock(return_value=ent_reg)
        er_mod.async_entries_for_device = MagicMock(return_value=entries)

    def test_returns_none_when_switch_entry_missing(self):
        """No switch entry → (None, 1.0)."""
        import sys
        from unittest.mock import MagicMock

        from never_dry.sensor import _discover_hw_max_duration

        hass = MagicMock()
        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        ent_reg = MagicMock()
        ent_reg.async_get.return_value = None
        er_mod.async_get = MagicMock(return_value=ent_reg)
        er_mod.async_entries_for_device = MagicMock(return_value=[])

        result = _discover_hw_max_duration(hass, "switch.valve")
        assert result == (None, 1.0)

    def test_returns_none_when_no_device_id(self):
        """Switch entry with device_id=None → (None, 1.0)."""
        import sys
        from unittest.mock import MagicMock

        from never_dry.sensor import _discover_hw_max_duration

        hass = MagicMock()
        switch_entry = MagicMock()
        switch_entry.device_id = None
        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        ent_reg = MagicMock()
        ent_reg.async_get.return_value = switch_entry
        er_mod.async_get = MagicMock(return_value=ent_reg)
        er_mod.async_entries_for_device = MagicMock(return_value=[])

        result = _discover_hw_max_duration(hass, "switch.valve")
        assert result == (None, 1.0)

    def test_returns_none_when_no_number_candidates(self):
        """Device has no matching number entity → (None, 1.0)."""
        import sys
        from unittest.mock import MagicMock

        from never_dry.sensor import _discover_hw_max_duration

        hass = MagicMock()
        switch_entry = MagicMock()
        switch_entry.device_id = "dev123"
        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        ent_reg = MagicMock()
        ent_reg.async_get.return_value = switch_entry
        er_mod.async_get = MagicMock(return_value=ent_reg)
        er_mod.async_entries_for_device = MagicMock(return_value=[])

        result = _discover_hw_max_duration(hass, "switch.valve")
        assert result == (None, 1.0)

    def test_discovers_seconds_entity(self):
        """Finds a matching number entity with second unit → multiplier=1.0."""
        import sys
        from unittest.mock import MagicMock

        from never_dry.sensor import _discover_hw_max_duration

        hass = MagicMock()
        switch_entry = MagicMock()
        switch_entry.device_id = "dev123"

        num_entry = MagicMock()
        num_entry.domain = "number"
        num_entry.entity_id = "number.valve_max_duration"
        num_entry.original_name = "Max Duration"

        state = MagicMock()
        state.attributes = {"unit_of_measurement": "s"}
        hass.states.get.return_value = state

        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        ent_reg = MagicMock()
        ent_reg.async_get.return_value = switch_entry
        er_mod.async_get = MagicMock(return_value=ent_reg)
        er_mod.async_entries_for_device = MagicMock(return_value=[num_entry])

        entity_id, multiplier = _discover_hw_max_duration(hass, "switch.valve")

        assert entity_id == "number.valve_max_duration"
        assert multiplier == pytest.approx(1.0)

    def test_discovers_minutes_entity(self):
        """Finds a matching number entity with minutes unit → multiplier=1/60."""
        import sys
        from unittest.mock import MagicMock

        from never_dry.sensor import _discover_hw_max_duration

        hass = MagicMock()
        switch_entry = MagicMock()
        switch_entry.device_id = "dev456"

        num_entry = MagicMock()
        num_entry.domain = "number"
        num_entry.entity_id = "number.valve_irrigation_time"
        num_entry.original_name = "Irrigation Time"

        state = MagicMock()
        state.attributes = {"unit_of_measurement": "min"}
        hass.states.get.return_value = state

        er_mod = sys.modules["homeassistant.helpers.entity_registry"]
        ent_reg = MagicMock()
        ent_reg.async_get.return_value = switch_entry
        er_mod.async_get = MagicMock(return_value=ent_reg)
        er_mod.async_entries_for_device = MagicMock(return_value=[num_entry])

        entity_id, multiplier = _discover_hw_max_duration(hass, "switch.valve")

        assert entity_id == "number.valve_irrigation_time"
        assert multiplier == pytest.approx(1.0 / 60.0)
