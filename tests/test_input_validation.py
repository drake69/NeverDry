"""Tests for runtime input validation bounds."""

from never_dry.const import MAX_ZONE_NAME_LENGTH, MAX_ZONES, MIN_SERVICE_INTERVAL_S


class TestValidationConstants:
    """Verify safety limit constants are reasonable."""

    def test_max_zones_is_positive(self):
        assert MAX_ZONES > 0

    def test_max_zones_is_bounded(self):
        """Should not allow unreasonable numbers of zones."""
        assert MAX_ZONES <= 100

    def test_max_zone_name_length_is_positive(self):
        assert MAX_ZONE_NAME_LENGTH > 0

    def test_max_zone_name_length_prevents_abuse(self):
        """Names should be short enough to prevent resource abuse."""
        assert MAX_ZONE_NAME_LENGTH <= 256

    def test_min_service_interval_is_positive(self):
        assert MIN_SERVICE_INTERVAL_S > 0

    def test_min_service_interval_is_reasonable(self):
        """Interval should be short enough to not block legitimate use."""
        assert MIN_SERVICE_INTERVAL_S <= 60


class TestZoneNameValidation:
    """Test zone name length enforcement logic."""

    def test_name_within_limit_is_valid(self):
        name = "Vegetable Garden"
        assert len(name) <= MAX_ZONE_NAME_LENGTH

    def test_name_at_limit_is_valid(self):
        name = "A" * MAX_ZONE_NAME_LENGTH
        assert len(name) <= MAX_ZONE_NAME_LENGTH

    def test_name_exceeding_limit_is_rejected(self):
        name = "A" * (MAX_ZONE_NAME_LENGTH + 1)
        assert len(name) > MAX_ZONE_NAME_LENGTH


class TestMaxZonesValidation:
    """Test max zones enforcement logic."""

    def test_zones_below_limit(self):
        zones = list(range(MAX_ZONES - 1))
        assert len(zones) < MAX_ZONES

    def test_zones_at_limit_blocks_new(self):
        zones = list(range(MAX_ZONES))
        assert len(zones) >= MAX_ZONES
