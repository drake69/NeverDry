"""Tests for DrynessIndexSensor — cumulative soil water deficit."""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from never_dry.sensor import DrynessIndexSensor


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
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)

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
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)

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
        assert di_sensor._attr_name == "Dryness Index"

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
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),  # T=T_base → ET=0
            "sensor.rain": make_state(2.0),
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(8.0, abs=0.01)

    def test_event_mode_same_value_no_double_count(self, di_sensor, hass_mock, make_state):
        """A recompute triggered by another sensor (e.g. temperature) must not
        re-apply rain: the rain sensor's state object is unchanged, so its
        ``last_updated`` timestamp is identical across the two reads."""
        di_sensor._deficit = 10.0
        rain_state = make_state(2.0)
        rain_state.last_updated = datetime(2026, 6, 1, 12, 0, 0)

        def states_get(eid):
            return rain_state if eid == "sensor.rain" else make_state(9.0)

        hass_mock.states.get.side_effect = states_get

        # First event
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())
        after_first = di_sensor._deficit

        # Second call: temperature changed but the rain sensor state is the same
        # object (same last_updated) — no new rain event.
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())

        # Deficit should NOT decrease again (rain_delta = 0 on repeat)
        assert di_sensor._deficit == pytest.approx(after_first, abs=0.01)

    def test_event_mode_repeated_identical_events_counted(self, di_sensor, hass_mock, make_state):
        """Two genuine rain events of the same magnitude must both be counted.

        Detection of a new event is by the sensor's ``last_updated`` timestamp,
        not by value, so a ``force_update`` sensor emitting 2 mm twice reduces
        the deficit twice (regression for the value-equality dedup bug).
        """
        di_sensor._deficit = 10.0
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)

        def make_rain(ts):
            s = make_state(2.0)
            s.last_updated = ts
            return s

        # First event at t1
        rain1 = make_rain(datetime(2026, 6, 1, 12, 0, 0))
        hass_mock.states.get.side_effect = lambda eid: rain1 if eid == "sensor.rain" else make_state(9.0)
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(8.0, abs=0.01)

        # Second identical event at t2 (advanced timestamp) — counted again.
        rain2 = make_rain(datetime(2026, 6, 1, 12, 5, 0))
        hass_mock.states.get.side_effect = lambda eid: rain2 if eid == "sensor.rain" else make_state(9.0)
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(6.0, abs=0.01)

    def test_event_mode_new_event(self, di_sensor, hass_mock, make_state):
        """New rain event with different value should subtract."""
        di_sensor._deficit = 10.0
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)

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
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        sensor._last_rain = 0.0
        sensor._last_rain_event_ts = datetime(2020, 1, 1)

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
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(20.0),
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(seconds=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == 0.0


class TestRainUnits:
    """Rain sensor reporting in inches is converted to mm before deficit update."""

    def test_rain_inches_converted_to_mm(self, di_sensor, hass_mock, make_state):
        """1 inch of rain must reduce the deficit by 25.4 mm."""
        di_sensor._deficit = 30.0
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),  # ET = 0
            "sensor.rain": make_state(1.0, unit="in"),
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(30.0 - 25.4, abs=0.1)

    def test_rain_mm_not_converted(self, di_sensor, hass_mock, make_state):
        """Rain in mm must not be multiplied by the inches factor."""
        di_sensor._deficit = 10.0
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(5.0, unit="mm"),
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(5.0, abs=0.1)

    def test_rain_no_unit_treated_as_mm(self, di_sensor, hass_mock, make_state):
        """Rain sensor without unit_of_measurement is treated as mm (backward compat)."""
        di_sensor._deficit = 8.0
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(3.0),  # no unit
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(5.0, abs=0.1)

    def test_half_inch_rain_converts_correctly(self, di_sensor, hass_mock, make_state):
        """0.5 in rain == 12.7 mm."""
        di_sensor._deficit = 20.0
        # Known rain baseline (post-restore steady state): the first tick
        # after boot fixes the baseline without crediting (2026-07-17 fix).
        di_sensor._last_rain = 0.0
        di_sensor._last_rain_event_ts = datetime(2020, 1, 1)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(0.5, unit="in"),
        }[eid]
        di_sensor._last_update = datetime.now() - timedelta(hours=1)
        di_sensor._on_sensor_change(MagicMock())
        assert di_sensor._deficit == pytest.approx(20.0 - 12.7, abs=0.1)


class TestTemperatureUnitsEndToEnd:
    """Deficit update is identical whether temperature is in °C or °F."""

    def test_celsius_and_fahrenheit_yield_same_deficit(self, hass_mock, base_config, make_state):
        """25 °C and 77 °F must produce the same deficit after one hour."""
        # sensor fed in Celsius
        di_c = DrynessIndexSensor(hass_mock, base_config)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(25.0, unit="°C"),
            "sensor.rain": make_state(0.0),
        }[eid]
        di_c._last_update = datetime.now() - timedelta(hours=1)
        di_c._on_sensor_change(MagicMock())

        # sensor fed in Fahrenheit (same physical temperature)
        di_f = DrynessIndexSensor(hass_mock, base_config)
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(77.0, unit="°F"),  # 77 °F == 25 °C
            "sensor.rain": make_state(0.0),
        }[eid]
        di_f._last_update = datetime.now() - timedelta(hours=1)
        di_f._on_sensor_change(MagicMock())

        assert di_c._deficit == pytest.approx(di_f._deficit, abs=0.001)

    def test_freezing_fahrenheit_yields_zero_deficit(self, hass_mock, base_config, make_state):
        """32 °F == 0 °C < T_base → ET = 0, deficit stays unchanged."""
        di = DrynessIndexSensor(hass_mock, base_config)
        di._deficit = 5.0
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(32.0, unit="°F"),
            "sensor.rain": make_state(0.0),
        }[eid]
        di._last_update = datetime.now() - timedelta(hours=1)
        di._on_sensor_change(MagicMock())
        assert di._deficit == pytest.approx(5.0, abs=0.01)


class TestRainBaselineAcrossRestart:
    """The rain baseline must survive restarts (field bug 2026-07-17).

    _last_rain started at 0.0 on every boot, so a cumulative/24h rain
    sensor reading was re-credited in full at the first tick after
    restart — 14.2 mm of rain wiped every zone deficit on reboot.
    """

    def _daily_sensor(self, hass_mock):
        from never_dry.const import (
            CONF_RAIN_SENSOR,
            CONF_RAIN_SENSOR_TYPE,
            CONF_TEMP_SENSOR,
            RAIN_TYPE_DAILY_TOTAL,
        )
        from never_dry.sensor import DrynessIndexSensor

        return DrynessIndexSensor(
            hass_mock,
            {
                CONF_TEMP_SENSOR: "sensor.temperature",
                CONF_RAIN_SENSOR: "sensor.rain",
                CONF_RAIN_SENSOR_TYPE: RAIN_TYPE_DAILY_TOTAL,
            },
        )

    def test_first_reading_after_boot_fixes_baseline_without_credit(self, hass_mock, make_state):
        """The exact field scenario: 14.2 mm of 24h rain at boot must NOT
        be credited — it predates this boot."""
        sensor = self._daily_sensor(hass_mock)
        sensor._deficit = 10.0
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(14.2),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(seconds=1)
        sensor._on_sensor_change(MagicMock())

        assert sensor._deficit == pytest.approx(10.0, abs=0.01)
        assert sensor._last_rain == pytest.approx(14.2)

        # New rain AFTER the baseline fix is credited normally.
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(17.2),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(seconds=1)
        sensor._on_sensor_change(MagicMock())
        assert sensor._deficit == pytest.approx(7.0, abs=0.01)

    def test_restored_baseline_credits_downtime_rain(self, hass_mock, make_state):
        """With a restored baseline, rain fallen while HA was down IS credited."""
        sensor = self._daily_sensor(hass_mock)
        sensor._deficit = 10.0
        sensor._last_rain = 10.0  # restored from rain_baseline_mm
        hass_mock.states.get.side_effect = lambda eid: {
            "sensor.temperature": make_state(9.0),
            "sensor.rain": make_state(14.2),
        }[eid]
        sensor._last_update = datetime.now() - timedelta(seconds=1)
        sensor._on_sensor_change(MagicMock())
        assert sensor._deficit == pytest.approx(10.0 - 4.2, abs=0.01)

    def test_event_sensor_restored_event_not_recredited(self, hass_mock, make_state):
        """Event mode: the state present at boot is the restore of an event
        already counted before the restart — no re-credit."""
        from never_dry.const import CONF_RAIN_SENSOR, CONF_TEMP_SENSOR
        from never_dry.sensor import DrynessIndexSensor

        sensor = DrynessIndexSensor(
            hass_mock,
            {CONF_TEMP_SENSOR: "sensor.temperature", CONF_RAIN_SENSOR: "sensor.rain"},
        )
        sensor._deficit = 10.0
        rain_state = make_state(2.0)
        rain_state.last_updated = datetime(2026, 6, 1, 12, 0, 0)
        hass_mock.states.get.side_effect = lambda eid: rain_state if eid == "sensor.rain" else make_state(9.0)
        sensor._last_update = datetime.now() - timedelta(seconds=1)
        sensor._on_sensor_change(MagicMock())
        assert sensor._deficit == pytest.approx(10.0, abs=0.01)

        # A genuinely NEW event (fresh timestamp) is credited.
        rain2 = make_state(2.0)
        rain2.last_updated = datetime(2026, 6, 1, 12, 5, 0)
        hass_mock.states.get.side_effect = lambda eid: rain2 if eid == "sensor.rain" else make_state(9.0)
        sensor._last_update = datetime.now() - timedelta(seconds=1)
        sensor._on_sensor_change(MagicMock())
        assert sensor._deficit == pytest.approx(8.0, abs=0.01)

    def test_rain_baseline_exposed_in_attributes(self, hass_mock, make_state):
        """The baseline is persisted through extra_state_attributes."""
        sensor = self._daily_sensor(hass_mock)
        assert "rain_baseline_mm" not in sensor.extra_state_attributes
        sensor._last_rain = 14.2
        assert sensor.extra_state_attributes["rain_baseline_mm"] == pytest.approx(14.2)
