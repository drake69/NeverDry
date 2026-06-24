"""Regression tests: every sensor that participates in SI/imperial conversion
must declare the correct device_class so that Home Assistant can auto-convert
the displayed unit (mm→in, L→gal, L/min→gal/min, mm/h→in/h, m²→ft²).

If a device_class is accidentally removed, HA silently stops converting units
for users who have Home Assistant configured in imperial mode.
"""

from homeassistant.components.sensor import SensorDeviceClass
from never_dry.sensor import (
    ZoneAreaSensor,
    ZoneDeficitSensor,
    ZoneDurationSensor,
    ZoneFlowRateSensor,
    ZoneLastDurationSensor,
    ZoneLastVolumeSensor,
    ZoneRainSensor,
    ZoneSessionWaterSensor,
    ZoneThresholdSensor,
    ZoneYearlyWaterSensor,
)


class TestDeviceClassDeclarations:
    """Every sensor class must declare the device_class that drives unit conversion."""

    def test_et_sensor_precipitation_intensity(self, et_sensor):
        """ET rate [mm/h] → HA converts to [in/h] in imperial."""
        assert et_sensor._attr_device_class == SensorDeviceClass.PRECIPITATION_INTENSITY

    def test_dryness_index_precipitation(self, di_sensor):
        """Global deficit [mm] → HA converts to [in] in imperial."""
        assert di_sensor._attr_device_class == SensorDeviceClass.PRECIPITATION

    def test_irrigation_zone_volume_storage(self, zone_orto):
        """Zone volume sensor [L] → HA converts to [gal] in imperial."""
        assert zone_orto._attr_device_class == SensorDeviceClass.VOLUME_STORAGE

    def test_zone_deficit_sensor_precipitation(self, di_sensor, zone_orto):
        """Per-zone deficit [mm] → HA converts to [in] in imperial."""
        deficit = ZoneDeficitSensor(zone_orto)
        assert deficit._attr_device_class == SensorDeviceClass.PRECIPITATION

    def test_zone_rain_sensor_precipitation(self, di_sensor, zone_orto):
        """Zone cumulative rain [mm] → HA converts to [in] in imperial."""
        rain = ZoneRainSensor(zone_orto)
        assert rain._attr_device_class == SensorDeviceClass.PRECIPITATION

    def test_zone_session_water_volume_storage(self, di_sensor, zone_orto):
        """Session water delivered [L] → HA converts to [gal] in imperial."""
        sensor = ZoneSessionWaterSensor(zone_orto)
        assert sensor._attr_device_class == SensorDeviceClass.VOLUME_STORAGE

    def test_zone_yearly_water_volume_storage(self, di_sensor, zone_orto):
        """Yearly water delivered [L] → HA converts to [gal] in imperial."""
        sensor = ZoneYearlyWaterSensor(zone_orto)
        assert sensor._attr_device_class == SensorDeviceClass.VOLUME_STORAGE

    def test_zone_duration_sensor_duration(self, di_sensor, zone_orto):
        """Planned irrigation duration [s] — device_class=DURATION for correct display."""
        sensor = ZoneDurationSensor(zone_orto)
        assert sensor._attr_device_class == SensorDeviceClass.DURATION

    def test_zone_last_duration_sensor_duration(self, di_sensor, zone_orto):
        """Last session duration [s] — device_class=DURATION."""
        sensor = ZoneLastDurationSensor(zone_orto)
        assert sensor._attr_device_class == SensorDeviceClass.DURATION

    def test_zone_flow_rate_volume_flow_rate(self, di_sensor, zone_orto):
        """Flow rate [L/min] → HA converts to [gal/min] in imperial."""
        sensor = ZoneFlowRateSensor(zone_orto)
        assert sensor._attr_device_class == SensorDeviceClass.VOLUME_FLOW_RATE

    def test_zone_last_volume_volume_storage(self, di_sensor, zone_orto):
        """Last irrigation volume [L] → HA converts to [gal] in imperial."""
        sensor = ZoneLastVolumeSensor(zone_orto)
        assert sensor._attr_device_class == SensorDeviceClass.VOLUME_STORAGE

    def test_zone_threshold_sensor_precipitation(self, di_sensor, zone_orto):
        """Threshold [mm] → HA converts to [in] in imperial."""
        sensor = ZoneThresholdSensor(zone_orto)
        assert sensor._attr_device_class == SensorDeviceClass.PRECIPITATION

    def test_zone_area_sensor_area(self, di_sensor, zone_orto):
        """Zone area [m²] → HA converts to [ft²] in imperial."""
        sensor = ZoneAreaSensor(zone_orto)
        assert sensor._attr_device_class == SensorDeviceClass.AREA
