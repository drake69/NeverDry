"""Tests for ETSensor — evapotranspiration estimate."""


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
        from never_dry.const import CONF_ALPHA, CONF_TEMP_SENSOR
        from never_dry.sensor import ETSensor

        config = {CONF_TEMP_SENSOR: "sensor.t", CONF_ALPHA: 0.30}
        sensor = ETSensor(hass_mock, config)
        sensor._on_temp_change(make_event(20.0))
        # T_base defaults to 9.0
        expected = round(0.30 * (20.0 - 9.0) / 24, 4)
        assert sensor.native_value == expected

    def test_custom_t_base(self, hass_mock, make_event):
        from never_dry.const import CONF_T_BASE, CONF_TEMP_SENSOR
        from never_dry.sensor import ETSensor

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


class TestETFahrenheit:
    """Test that ETSensor correctly converts °F input to °C before the formula."""

    def test_et_fahrenheit_equivalent(self, et_sensor, make_event):
        """77°F == 25°C → same ET as 25°C test."""
        et_sensor._on_temp_change(make_event(77.0, unit="°F"))
        expected = round(0.22 * (25.0 - 9.0) / 24, 4)
        assert et_sensor.native_value == expected

    def test_et_fahrenheit_below_base(self, et_sensor, make_event):
        """48.2°F == 9°C (T_base) → ET should be zero."""
        et_sensor._on_temp_change(make_event(48.2, unit="°F"))
        assert et_sensor.native_value == 0.0

    def test_et_fahrenheit_cold(self, et_sensor, make_event):
        """41°F == 5°C < T_base → ET should be zero."""
        et_sensor._on_temp_change(make_event(41.0, unit="°F"))
        assert et_sensor.native_value == 0.0

    def test_et_no_unit_treated_as_celsius(self, et_sensor, make_event):
        """Sensor without unit_of_measurement is treated as °C (backward compat)."""
        et_sensor._on_temp_change(make_event(25.0))
        expected = round(0.22 * (25.0 - 9.0) / 24, 4)
        assert et_sensor.native_value == expected

    def test_et_unavailable_fahrenheit_preserves_previous(self, et_sensor, make_event):
        """Unavailable state with °F unit is gracefully ignored."""
        et_sensor._on_temp_change(make_event(77.0, unit="°F"))
        previous = et_sensor.native_value
        assert previous > 0
        et_sensor._on_temp_change(make_event("unavailable", unit="°F"))
        assert et_sensor.native_value == previous


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
