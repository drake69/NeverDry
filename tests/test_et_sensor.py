"""Tests for ETSensor — evapotranspiration estimate."""

import pytest


class TestETFormula:
    """Test the linear ET model: ET_h = max(0, alpha * (T - T_base) / 24)."""

    def test_et_above_base(self, et_sensor, make_event):
        """ET should be positive when T > T_base."""
        # T=25°C, alpha=0.22, T_base=9.0
        # Expected: 0.22 * (25 - 9) / 24 = 0.22 * 16 / 24 ≈ 0.1467
        et_sensor._on_temp_change(make_event(25.0))
        assert et_sensor.native_value == round(0.22 * 16 / 24, 4)

    def test_et_at_base(self, et_sensor, make_event):
        """ET should be zero when T == T_base."""
        et_sensor._on_temp_change(make_event(9.0))
        assert et_sensor.native_value == 0.0

    def test_et_below_base(self, et_sensor, make_event):
        """ET should be zero when T < T_base (no negative ET)."""
        et_sensor._on_temp_change(make_event(5.0))
        assert et_sensor.native_value == 0.0

    def test_et_negative_temperature(self, et_sensor, make_event):
        """ET should be zero for sub-zero temperatures."""
        et_sensor._on_temp_change(make_event(-10.0))
        assert et_sensor.native_value == 0.0

    def test_et_high_temperature(self, et_sensor, make_event):
        """ET scales linearly with temperature above base."""
        # T=40°C → 0.22 * (40-9) / 24 = 0.22 * 31 / 24 ≈ 0.2842
        et_sensor._on_temp_change(make_event(40.0))
        assert et_sensor.native_value == round(0.22 * 31 / 24, 4)

    def test_et_fractional_temperature(self, et_sensor, make_event):
        """ET works with fractional temperatures."""
        et_sensor._on_temp_change(make_event(15.5))
        expected = round(0.22 * (15.5 - 9.0) / 24, 4)
        assert et_sensor.native_value == expected


class TestETCustomParameters:
    """Test ET with non-default alpha and T_base."""

    def test_custom_alpha(self, hass_mock, make_event):
        from never_dry.sensor import ETSensor
        from never_dry.const import CONF_TEMP_SENSOR, CONF_ALPHA

        config = {CONF_TEMP_SENSOR: "sensor.t", CONF_ALPHA: 0.30}
        sensor = ETSensor(hass_mock, config)
        sensor._on_temp_change(make_event(20.0))
        # T_base defaults to 9.0
        expected = round(0.30 * (20.0 - 9.0) / 24, 4)
        assert sensor.native_value == expected

    def test_custom_t_base(self, hass_mock, make_event):
        from never_dry.sensor import ETSensor
        from never_dry.const import CONF_TEMP_SENSOR, CONF_T_BASE

        config = {CONF_TEMP_SENSOR: "sensor.t", CONF_T_BASE: 5.0}
        sensor = ETSensor(hass_mock, config)
        sensor._on_temp_change(make_event(20.0))
        expected = round(0.22 * (20.0 - 5.0) / 24, 4)
        assert sensor.native_value == expected


class TestETEdgeCases:
    """Test ET sensor with invalid or edge-case inputs."""

    def test_invalid_state_string(self, et_sensor, make_event):
        """Non-numeric state should be ignored, value stays at 0."""
        et_sensor._on_temp_change(make_event("unavailable"))
        assert et_sensor.native_value == 0.0

    def test_invalid_state_preserves_previous(self, et_sensor, make_event):
        """After a valid reading, invalid state should keep previous value."""
        et_sensor._on_temp_change(make_event(25.0))
        previous = et_sensor.native_value
        assert previous > 0

        et_sensor._on_temp_change(make_event("unknown"))
        assert et_sensor.native_value == previous

    def test_none_new_state(self, et_sensor):
        """Event with None new_state should be safely ignored."""
        from unittest.mock import MagicMock
        event = MagicMock()
        event.data = {"new_state": None}
        et_sensor._on_temp_change(event)
        assert et_sensor.native_value == 0.0


class TestETAttributes:
    """Test sensor metadata."""

    def test_unit(self, et_sensor):
        assert et_sensor._attr_native_unit_of_measurement == "mm/h"

    def test_name(self, et_sensor):
        assert et_sensor._attr_name == "ET Hourly Estimate"

    def test_icon(self, et_sensor):
        assert et_sensor._attr_icon == "mdi:sun-thermometer"

    def test_initial_value(self, et_sensor):
        assert et_sensor.native_value == 0.0
