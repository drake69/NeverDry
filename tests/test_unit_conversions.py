"""Unit-system conversion tests for the config flow (UI ↔ metric storage) and
the controller (imperial flow/volume sensor readings → liters).

NeverDry always stores and computes in metric. When Home Assistant runs in
US-customary mode the config flow must show imperial labels and convert user
input back to metric, and the controller must convert imperial sensor readings
(gal/min, gal) into liters before integrating them into delivered volume.
"""

import pytest
from never_dry.const import (
    CONF_D_MAX,
    CONF_T_BASE,
    CONF_ZONE_AREA,
    CONF_ZONE_FLOW_RATE,
    CONF_ZONE_THRESHOLD,
)
from never_dry.controller import IrrigationController
from never_dry.unit_convert import (
    c_to_f as _c_to_f,
)
from never_dry.unit_convert import (
    f_to_c as _f_to_c,
)
from never_dry.unit_convert import (
    sensors_input_to_metric as _sensors_input_to_metric,
)
from never_dry.unit_convert import (
    zone_input_to_metric as _zone_input_to_metric,
)


class TestTemperatureConversion:
    def test_c_to_f_freezing(self):
        assert _c_to_f(0.0) == 32.0

    def test_c_to_f_body(self):
        assert _c_to_f(25.0) == 77.0

    def test_f_to_c_roundtrip(self):
        assert _f_to_c(_c_to_f(9.0)) == pytest.approx(9.0, abs=0.01)

    def test_f_to_c_freezing(self):
        assert _f_to_c(32.0) == pytest.approx(0.0, abs=0.01)


class TestSensorsInputToMetric:
    def test_metric_passthrough(self):
        data = {CONF_T_BASE: 9.0, CONF_D_MAX: 100.0}
        assert _sensors_input_to_metric(data, is_imperial=False) == data

    def test_t_base_fahrenheit_converted(self):
        out = _sensors_input_to_metric({CONF_T_BASE: 77.0}, is_imperial=True)
        assert out[CONF_T_BASE] == pytest.approx(25.0, abs=0.01)

    def test_d_max_inches_converted(self):
        out = _sensors_input_to_metric({CONF_D_MAX: 4.0}, is_imperial=True)
        assert out[CONF_D_MAX] == pytest.approx(101.6, abs=0.01)

    def test_none_values_untouched(self):
        out = _sensors_input_to_metric({CONF_T_BASE: None}, is_imperial=True)
        assert out[CONF_T_BASE] is None

    def test_does_not_mutate_input(self):
        data = {CONF_T_BASE: 77.0}
        _sensors_input_to_metric(data, is_imperial=True)
        assert data[CONF_T_BASE] == 77.0


class TestZoneInputToMetric:
    def test_metric_passthrough(self):
        data = {CONF_ZONE_AREA: 20.0, CONF_ZONE_FLOW_RATE: 8.0, CONF_ZONE_THRESHOLD: 20.0}
        assert _zone_input_to_metric(data, is_imperial=False) == data

    def test_area_ft2_to_m2(self):
        out = _zone_input_to_metric({CONF_ZONE_AREA: 107.639}, is_imperial=True)
        assert out[CONF_ZONE_AREA] == pytest.approx(10.0, abs=0.01)

    def test_flow_rate_gpm_to_lpm(self):
        out = _zone_input_to_metric({CONF_ZONE_FLOW_RATE: 1.0}, is_imperial=True)
        # 1 gal/min ≈ 3.785 L/min
        assert out[CONF_ZONE_FLOW_RATE] == pytest.approx(3.785, abs=0.01)

    def test_threshold_in_to_mm(self):
        out = _zone_input_to_metric({CONF_ZONE_THRESHOLD: 1.0}, is_imperial=True)
        assert out[CONF_ZONE_THRESHOLD] == pytest.approx(25.4, abs=0.01)

    def test_does_not_mutate_input(self):
        data = {CONF_ZONE_AREA: 100.0}
        _zone_input_to_metric(data, is_imperial=True)
        assert data[CONF_ZONE_AREA] == 100.0


class TestControllerRateToLpm:
    def test_lpm_passthrough(self):
        assert IrrigationController._rate_to_lpm(10.0, "L/min") == 10.0

    def test_lph_to_lpm(self):
        assert IrrigationController._rate_to_lpm(60.0, "L/h") == pytest.approx(1.0)

    def test_m3h_to_lpm(self):
        # 1 m³/h = 1000 L / 60 min ≈ 16.667 L/min
        assert IrrigationController._rate_to_lpm(1.0, "m³/h") == pytest.approx(16.667, abs=0.01)

    def test_gpm_to_lpm(self):
        # 1 gal/min ≈ 3.785 L/min
        assert IrrigationController._rate_to_lpm(1.0, "gal/min") == pytest.approx(3.785, abs=0.01)

    def test_gph_to_lpm(self):
        # 60 gal/h = 1 gal/min ≈ 3.785 L/min
        assert IrrigationController._rate_to_lpm(60.0, "gal/h") == pytest.approx(3.785, abs=0.01)

    def test_case_insensitive(self):
        assert IrrigationController._rate_to_lpm(10.0, "GAL/MIN") == pytest.approx(37.854, abs=0.01)

    def test_unknown_unit_defaults_to_lph(self):
        # Legacy default: treat unknown as L/h
        assert IrrigationController._rate_to_lpm(60.0, "weird") == pytest.approx(1.0)


class TestControllerVolumeToLiters:
    def test_liters_passthrough(self):
        assert IrrigationController._volume_to_liters(50.0, "L") == 50.0

    def test_none_unit_passthrough(self):
        assert IrrigationController._volume_to_liters(50.0, None) == 50.0

    def test_gallons_to_liters(self):
        assert IrrigationController._volume_to_liters(1.0, "gal") == pytest.approx(3.785, abs=0.01)

    def test_gallons_plural(self):
        assert IrrigationController._volume_to_liters(2.0, "gallons") == pytest.approx(7.571, abs=0.01)

    def test_cubic_meters_to_liters(self):
        assert IrrigationController._volume_to_liters(1.0, "m³") == 1000.0

    def test_case_insensitive(self):
        assert IrrigationController._volume_to_liters(1.0, "GAL") == pytest.approx(3.785, abs=0.01)


class TestImperialFlowMeterIsRate:
    """The controller must recognize imperial flow-rate units as rate sensors."""

    def _make_ctrl(self, hass_mock, di_sensor, unit):
        from unittest.mock import MagicMock

        state = MagicMock(state="5.0", attributes={"unit_of_measurement": unit})
        hass_mock.states.get = MagicMock(return_value=state)
        return IrrigationController(hass_mock, di_sensor, [], inter_zone_delay=0)

    def test_gal_min_detected_as_rate(self, hass_mock, di_sensor):
        ctrl = self._make_ctrl(hass_mock, di_sensor, "gal/min")
        assert ctrl._is_flow_rate_sensor("sensor.flow") is True

    def test_gal_h_detected_as_rate(self, hass_mock, di_sensor):
        ctrl = self._make_ctrl(hass_mock, di_sensor, "gal/h")
        assert ctrl._is_flow_rate_sensor("sensor.flow") is True

    def test_gallons_cumulative_not_rate(self, hass_mock, di_sensor):
        ctrl = self._make_ctrl(hass_mock, di_sensor, "gal")
        assert ctrl._is_flow_rate_sensor("sensor.flow") is False
