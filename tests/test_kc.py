"""Tests for crop coefficient (Kc) computation and per-zone deficit tracking."""

import pytest
from unittest.mock import MagicMock

from never_dry.sensor import compute_kc, IrrigationZoneSensor
from never_dry.const import (
    CONF_ZONE_NAME, CONF_ZONE_VALVE, CONF_ZONE_AREA,
    CONF_ZONE_EFFICIENCY, CONF_ZONE_FLOW_RATE,
    CONF_ZONE_PLANT_FAMILY, CONF_ZONE_KC,
)


# ══════════════════════════════════════════════════════════
#  compute_kc pure function
# ══════════════════════════════════════════════════════════


class TestComputeKcOverride:
    """Manual Kc overrides everything."""

    def test_manual_kc_overrides_family(self):
        assert compute_kc(196, "lawn", 0.5, 45.0) == 0.5

    def test_manual_kc_overrides_none_family(self):
        assert compute_kc(196, None, 0.75, 45.0) == 0.75


class TestComputeKcDefaults:
    """No family and no override → DEFAULT_KC (1.0)."""

    def test_no_family_no_override(self):
        assert compute_kc(196, None, None, 45.0) == 1.0

    def test_unknown_family(self):
        assert compute_kc(196, "unknown_plant", None, 45.0) == 1.0


class TestComputeKcAnchors:
    """Kc at exact anchor days should match the tuple values."""

    def test_lawn_mid_winter(self):
        # doy=15, lawn kc_seasonal=(0.45, 0.85, 1.00, 0.70)
        assert compute_kc(15, "lawn", None, 45.0) == pytest.approx(0.45, abs=0.01)

    def test_lawn_mid_spring(self):
        assert compute_kc(105, "lawn", None, 45.0) == pytest.approx(0.85, abs=0.01)

    def test_lawn_mid_summer(self):
        assert compute_kc(196, "lawn", None, 45.0) == pytest.approx(1.0, abs=0.01)

    def test_lawn_mid_autumn(self):
        assert compute_kc(288, "lawn", None, 45.0) == pytest.approx(0.70, abs=0.01)

    def test_succulents_mid_summer(self):
        assert compute_kc(196, "succulents", None, 45.0) == pytest.approx(0.35, abs=0.01)

    def test_vegetables_mid_summer(self):
        assert compute_kc(196, "vegetables", None, 45.0) == pytest.approx(1.10, abs=0.01)


class TestComputeKcInterpolation:
    """Kc between anchors should be linearly interpolated."""

    def test_midpoint_winter_spring(self):
        # Midpoint between 15 and 105 = day 60
        # lawn: 0.45 + (60-15)/(105-15) * (0.85-0.45) = 0.45 + 0.5*0.4 = 0.65
        result = compute_kc(60, "lawn", None, 45.0)
        assert result == pytest.approx(0.65, abs=0.01)

    def test_midpoint_summer_autumn(self):
        # Midpoint between 196 and 288 = day 242
        # lawn: 1.0 + (242-196)/(288-196) * (0.70-1.0) = 1.0 + 0.5*(-0.3) = 0.85
        result = compute_kc(242, "lawn", None, 45.0)
        assert result == pytest.approx(0.85, abs=0.01)


class TestComputeKcWrapAround:
    """Test year boundary (autumn → winter, crossing Dec-Jan)."""

    def test_day_350(self):
        # Between autumn (288) and winter (15), wrapping around.
        # lawn: autumn=0.70, winter=0.45
        result = compute_kc(350, "lawn", None, 45.0)
        assert 0.45 <= result <= 0.70

    def test_day_1(self):
        # Just after New Year, between autumn and winter anchor
        result = compute_kc(1, "lawn", None, 45.0)
        assert 0.45 <= result <= 0.70

    def test_day_365(self):
        result = compute_kc(365, "lawn", None, 45.0)
        assert 0.45 <= result <= 0.70


class TestComputeKcSouthernHemisphere:
    """Southern hemisphere flips seasons by 182 days."""

    def test_southern_mid_summer_is_northern_winter(self):
        # doy=196 (Jul) in southern hemisphere → shifted to ~Jan → winter Kc
        result = compute_kc(196, "lawn", None, -33.0)
        assert result == pytest.approx(0.45, abs=0.05)

    def test_southern_mid_winter_is_northern_summer(self):
        # doy=15 (Jan) in southern hemisphere → shifted to ~Jul → summer Kc
        result = compute_kc(15, "lawn", None, -33.0)
        assert result == pytest.approx(1.0, abs=0.05)

    def test_equator_is_northern(self):
        # latitude=0 → northern (no shift)
        result = compute_kc(196, "lawn", None, 0.0)
        assert result == pytest.approx(1.0, abs=0.01)


# ══════════════════════════════════════════════════════════
#  Per-zone deficit tracking
# ══════════════════════════════════════════════════════════


def _make_hass():
    hass = MagicMock()
    hass.config = MagicMock()
    hass.config.latitude = 45.0
    return hass


def _make_zone_sensor(di_sensor, plant_family=None, kc=None):
    """Helper for zone sensors with Kc config."""
    zone_config = {
        CONF_ZONE_NAME: "Test",
        CONF_ZONE_VALVE: "switch.valve",
        CONF_ZONE_AREA: 20.0,
        CONF_ZONE_EFFICIENCY: 0.85,
        CONF_ZONE_FLOW_RATE: 10.0,
    }
    if plant_family is not None:
        zone_config[CONF_ZONE_PLANT_FAMILY] = plant_family
    if kc is not None:
        zone_config[CONF_ZONE_KC] = kc
    return IrrigationZoneSensor(_make_hass(), zone_config, di_sensor)


class TestPerZoneDeficit:
    """Test zone-level deficit accumulation via _on_et_update."""

    def test_deficit_accumulates_with_kc(self, di_sensor):
        """Zone deficit = ET_h * Kc * dt_h."""
        zone = _make_zone_sensor(di_sensor, kc=0.5)
        zone._on_et_update(dt_h=1.0, et_h=0.15, rain=0.0)
        # 0.15 * 0.5 * 1.0 = 0.075
        assert zone._zone_deficit == pytest.approx(0.075, abs=0.001)

    def test_rain_reduces_zone_deficit(self, di_sensor):
        """Rain reduces zone deficit."""
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        zone._zone_deficit = 5.0
        zone._on_et_update(dt_h=1.0, et_h=0.0, rain=3.0)
        assert zone._zone_deficit == pytest.approx(2.0, abs=0.01)

    def test_deficit_never_negative(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        zone._zone_deficit = 1.0
        zone._on_et_update(dt_h=1.0, et_h=0.0, rain=10.0)
        assert zone._zone_deficit == 0.0

    def test_deficit_clamped_at_d_max(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        zone._zone_deficit = 99.0
        zone._on_et_update(dt_h=10.0, et_h=0.5, rain=0.0)
        assert zone._zone_deficit == di_sensor._d_max

    def test_different_kc_different_deficits(self, di_sensor):
        """Two zones with different Kc accumulate differently."""
        lawn = _make_zone_sensor(di_sensor, kc=1.0)
        succulent = _make_zone_sensor(di_sensor, kc=0.3)

        lawn._on_et_update(dt_h=1.0, et_h=0.15, rain=0.0)
        succulent._on_et_update(dt_h=1.0, et_h=0.15, rain=0.0)

        assert lawn._zone_deficit > succulent._zone_deficit
        assert lawn._zone_deficit == pytest.approx(0.15, abs=0.001)
        assert succulent._zone_deficit == pytest.approx(0.045, abs=0.001)

    def test_volume_uses_zone_deficit(self, di_sensor):
        """Volume should use _zone_deficit, not shared deficit."""
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        di_sensor._deficit = 50.0  # shared deficit is high
        zone._zone_deficit = 5.0  # zone deficit is low
        # Volume = 5 * 20 / 0.85 = 117.6 (not 50 * 20 / 0.85)
        assert zone.volume_liters == pytest.approx(117.6, abs=0.1)

    def test_reset_deficit(self, di_sensor):
        """reset_deficit zeroes only this zone."""
        zone = _make_zone_sensor(di_sensor, kc=1.0)
        zone._zone_deficit = 15.0
        zone.reset_deficit()
        assert zone._zone_deficit == 0.0
        assert zone.volume_liters == 0.0


class TestKcInAttributes:
    """Test Kc-related fields in extra_state_attributes."""

    def test_kc_in_attributes(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, plant_family="lawn")
        attrs = zone.extra_state_attributes
        assert "kc" in attrs
        assert "plant_family" in attrs
        assert attrs["plant_family"] == "lawn"
        assert attrs["kc"] > 0

    def test_kc_override_in_attributes(self, di_sensor):
        zone = _make_zone_sensor(di_sensor, plant_family="lawn", kc=0.6)
        attrs = zone.extra_state_attributes
        assert attrs["kc_override"] == 0.6
        assert attrs["kc"] == 0.6

    def test_no_family_kc_is_1(self, di_sensor):
        zone = _make_zone_sensor(di_sensor)
        attrs = zone.extra_state_attributes
        assert attrs["kc"] == 1.0
        assert attrs["plant_family"] is None
