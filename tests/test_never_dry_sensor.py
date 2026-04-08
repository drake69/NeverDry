"""Tests for DrynessIndexSensor — cumulative soil water deficit."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest


class TestDeficitAccumulation:
    """Test deficit grows with ET and shrinks with rain."""

    def test_deficit_increases_with_temperature(self, di_sensor, hass_mock, make_state):
        """Deficit should increase when T > T_base and no rain."""
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(25.0),
            "sensor.rain": make_state(0.0),
        }[eid]

        # Simulate 1 hour passing
        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        event = MagicMock()
        di_sensor._on_sensor_change(event)

        # ET_h = 0.22 * (25-9) / 24 ≈ 0.1467 mm/h → deficit ≈ 0.15 after 1h
        assert di_sensor._deficit > 0
        expected_et = 0.22 * (25.0 - 9.0) / 24 * 1.0
        assert abs(di_sensor._deficit - expected_et) < 0.01

    def test_rain_reduces_deficit(self, di_sensor, hass_mock, make_state):
        """Rain should reduce accumulated deficit."""
        di_sensor._deficit = 5.0

        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),  # T=T_base → ET=0
            "sensor.rain": make_state(3.0),
        }[eid]

        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())

        assert di_sensor._deficit == pytest.approx(2.0, abs=0.01)

    def test_deficit_never_negative(self, di_sensor, hass_mock, make_state):
        """Deficit is clipped to zero (no negative values)."""
        di_sensor._deficit = 1.0

        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(10.0),  # heavy rain
        }[eid]

        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())

        assert di_sensor._deficit == 0.0

    def test_deficit_clipped_at_d_max(self, di_sensor, hass_mock, make_state):
        """Deficit is clipped at D_max (default 100 mm)."""
        di_sensor._deficit = 99.5

        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(40.0),  # high ET
            "sensor.rain": make_state(0.0),
        }[eid]

        di_sensor._last_update = datetime.now() - timedelta(hours=10)
        di_sensor._on_sensor_change(MagicMock())

        assert di_sensor._deficit == 100.0

    def test_custom_d_max(self, hass_mock, make_state):
        """Custom D_max should be respected."""
        from never_dry.const import (
            CONF_D_MAX,
            CONF_RAIN_SENSOR,
            CONF_TEMP_SENSOR,
        )
        from never_dry.sensor import DrynessIndexSensor

        config = {
            CONF_TEMP_SENSOR: "sensor.temperature",
            CONF_RAIN_SENSOR: "sensor.rain",
            CONF_D_MAX: 50.0,
        }
        sensor = DrynessIndexSensor(hass_mock, config)
        sensor._deficit = 49.0

        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(40.0),
            "sensor.rain": make_state(0.0),
        }[eid]

        sensor._last_update = datetime.now() - timedelta(hours=10)
        sensor._on_sensor_change(MagicMock())

        assert sensor._deficit == 50.0

    def test_no_et_below_t_base(self, di_sensor, hass_mock, make_state):
        """No ET accumulation when temperature is below T_base."""
        di_sensor._deficit = 5.0

        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(5.0),  # below T_base=9
            "sensor.rain": make_state(0.0),
        }[eid]

        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())

        assert di_sensor._deficit == pytest.approx(5.0, abs=0.01)


class TestReset:
    """Test irrigation reset functionality."""

    def test_reset_zeroes_deficit(self, di_sensor):
        di_sensor._deficit = 25.0
        di_sensor.reset()
        assert di_sensor._deficit == 0.0

    def test_reset_updates_timestamp(self, di_sensor):
        old_time = di_sensor._last_update
        di_sensor.reset()
        assert di_sensor._last_update >= old_time

    def test_native_value_after_reset(self, di_sensor):
        di_sensor._deficit = 15.0
        di_sensor.reset()
        assert di_sensor.native_value == 0.0


class TestVWCMode:
    """Test VWC-based deficit calculation."""

    def test_vwc_below_field_capacity(self, hass_mock, make_state):
        """Deficit = (FC - VWC) * root_depth * 1000."""
        from never_dry.const import (
            CONF_FIELD_CAPACITY,
            CONF_RAIN_SENSOR,
            CONF_ROOT_DEPTH,
            CONF_TEMP_SENSOR,
            CONF_VWC_SENSOR,
        )
        from never_dry.sensor import DrynessIndexSensor

        config = {
            CONF_TEMP_SENSOR: "sensor.temperature",
            CONF_RAIN_SENSOR: "sensor.rain",
            CONF_VWC_SENSOR: "sensor.vwc",
            CONF_FIELD_CAPACITY: 0.30,
            CONF_ROOT_DEPTH: 0.30,
        }
        sensor = DrynessIndexSensor(hass_mock, config)

        hass_mock.states.get.return_value = make_state(0.20)  # VWC = 20%

        sensor._on_sensor_change(MagicMock())

        # (0.30 - 0.20) * 0.30 * 1000 = 30 mm
        assert sensor._deficit == pytest.approx(30.0, abs=0.1)

    def test_vwc_at_field_capacity(self, hass_mock, make_state):
        """Deficit = 0 when VWC == field capacity."""
        from never_dry.const import (
            CONF_RAIN_SENSOR,
            CONF_TEMP_SENSOR,
            CONF_VWC_SENSOR,
        )
        from never_dry.sensor import DrynessIndexSensor

        config = {
            CONF_TEMP_SENSOR: "sensor.temperature",
            CONF_RAIN_SENSOR: "sensor.rain",
            CONF_VWC_SENSOR: "sensor.vwc",
        }
        sensor = DrynessIndexSensor(hass_mock, config)

        hass_mock.states.get.return_value = make_state(0.30)

        sensor._on_sensor_change(MagicMock())
        assert sensor._deficit == 0.0

    def test_vwc_above_field_capacity(self, hass_mock, make_state):
        """Deficit = 0 when VWC > field capacity (saturated soil)."""
        from never_dry.const import (
            CONF_RAIN_SENSOR,
            CONF_TEMP_SENSOR,
            CONF_VWC_SENSOR,
        )
        from never_dry.sensor import DrynessIndexSensor

        config = {
            CONF_TEMP_SENSOR: "sensor.temperature",
            CONF_RAIN_SENSOR: "sensor.rain",
            CONF_VWC_SENSOR: "sensor.vwc",
        }
        sensor = DrynessIndexSensor(hass_mock, config)

        hass_mock.states.get.return_value = make_state(0.40)

        sensor._on_sensor_change(MagicMock())
        assert sensor._deficit == 0.0


class TestInvalidInputs:
    """Test handling of invalid or missing sensor data."""

    def test_invalid_temperature(self, di_sensor, hass_mock, make_state):
        """Invalid temperature should not change deficit."""
        di_sensor._deficit = 5.0

        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state("unavailable"),
            "sensor.rain": make_state(0.0),
        }[eid]

        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())

        assert di_sensor._deficit == 5.0

    def test_invalid_rain(self, di_sensor, hass_mock, make_state):
        """Invalid rain should still accumulate ET (rain delta = 0)."""
        di_sensor._deficit = 5.0

        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(25.0),
            "sensor.rain": make_state("unknown"),
        }[eid]

        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())

        # ET still accumulates; rain delta is 0 (invalid → ignored)
        assert di_sensor._deficit > 5.0

    def test_none_state(self, di_sensor, hass_mock):
        """None state object should not crash."""
        di_sensor._deficit = 5.0
        hass_mock.states.get.return_value = None

        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())

        assert di_sensor._deficit == 5.0


class TestSensorAttributes:
    """Test sensor metadata."""

    def test_unit(self, di_sensor):
        assert di_sensor._attr_native_unit_of_measurement == "mm"

    def test_name(self, di_sensor):
        assert di_sensor._attr_name == "NeverDry"

    def test_icon(self, di_sensor):
        assert di_sensor._attr_icon == "mdi:water-percent-alert"

    def test_native_value_rounded(self, di_sensor):
        di_sensor._deficit = 12.3456
        assert di_sensor.native_value == 12.35

    def test_initial_value(self, di_sensor):
        assert di_sensor.native_value == 0.0


class TestRainDelta:
    """Test rain delta computation for event and daily_total modes."""

    def test_event_mode_first_rain(self, di_sensor, hass_mock, make_state):
        """First rain event should reduce deficit by the event amount."""
        di_sensor._deficit = 10.0
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),  # T=T_base → ET=0
            "sensor.rain": make_state(2.0),
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(8.0, abs=0.01)

    def test_event_mode_same_value_no_double_count(self, di_sensor, hass_mock, make_state):
        """Same rain value on consecutive reads should not subtract twice."""
        di_sensor._deficit = 10.0
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(2.0),
        }[eid]

        # First event
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())
        after_first = di_sensor._deficit

        # Second call with same rain value (e.g., temp changed)
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())

        # Deficit should NOT decrease again (rain_delta = 0 on repeat)
        assert di_sensor._deficit == pytest.approx(after_first, abs=0.01)

    def test_event_mode_new_event(self, di_sensor, hass_mock, make_state):
        """New rain event with different value should subtract."""
        di_sensor._deficit = 10.0

        # First event: 2mm
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(2.0),
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(8.0, abs=0.01)

        # Second event: 3mm (different value)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(3.0),
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(5.0, abs=0.01)

    def test_daily_total_mode_accumulation(self, hass_mock, make_state):
        """Daily total mode should compute delta from previous reading."""
        from never_dry.const import (
            CONF_RAIN_SENSOR,
            CONF_RAIN_SENSOR_TYPE,
            CONF_TEMP_SENSOR,
            RAIN_TYPE_DAILY_TOTAL,
        )
        from never_dry.sensor import DrynessIndexSensor

        config = {
            CONF_TEMP_SENSOR: "sensor.temperature",
            CONF_RAIN_SENSOR: "sensor.rain",
            CONF_RAIN_SENSOR_TYPE: RAIN_TYPE_DAILY_TOTAL,
        }
        sensor = DrynessIndexSensor(hass_mock, config)
        sensor._deficit = 10.0

        # Rain total goes from 0 to 3mm
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(3.0),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(seconds=1)
        sensor._on_sensor_change(MagicMock())
        assert sensor._deficit == pytest.approx(7.0, abs=0.01)

        # Rain total goes from 3 to 5mm (delta = 2mm)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(5.0),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(seconds=1)
        sensor._on_sensor_change(MagicMock())
        assert sensor._deficit == pytest.approx(5.0, abs=0.01)

    def test_daily_total_mode_midnight_reset(self, hass_mock, make_state):
        """Daily total sensor resets at midnight — handle gracefully."""
        from never_dry.const import (
            CONF_RAIN_SENSOR,
            CONF_RAIN_SENSOR_TYPE,
            CONF_TEMP_SENSOR,
            RAIN_TYPE_DAILY_TOTAL,
        )
        from never_dry.sensor import DrynessIndexSensor

        config = {
            CONF_TEMP_SENSOR: "sensor.temperature",
            CONF_RAIN_SENSOR: "sensor.rain",
            CONF_RAIN_SENSOR_TYPE: RAIN_TYPE_DAILY_TOTAL,
        }
        sensor = DrynessIndexSensor(hass_mock, config)
        sensor._deficit = 10.0
        sensor._last_rain = 8.0  # accumulated 8mm yesterday

        # Midnight reset: sensor drops to 1.0 (new day, 1mm rain)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(1.0),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(seconds=1)
        sensor._on_sensor_change(MagicMock())

        # Should treat 1.0 as new rain (not -7.0 delta)
        assert sensor._deficit == pytest.approx(9.0, abs=0.01)

    def test_rain_zeroes_deficit(self, di_sensor, hass_mock, make_state):
        """Heavy rain should zero out the deficit (never goes negative)."""
        di_sensor._deficit = 3.0
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(20.0),
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == 0.0
