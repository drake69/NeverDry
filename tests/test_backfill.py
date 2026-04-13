"""Tests for S5.7 — Backfill deficit from HA recorder history."""

import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.const import RAIN_TYPE_DAILY_TOTAL
from never_dry.sensor import DrynessIndexSensor

# ── Helpers ──────────────────────────────────────────────────


def _make_recorder_state(value, timestamp):
    """Create a mock State object as returned by the recorder."""
    s = MagicMock()
    s.state = str(value)
    s.last_changed = timestamp
    return s


def _ts(hours_ago: float, base: datetime | None = None) -> datetime:
    """Return a datetime `hours_ago` hours before `base`."""
    base = base or datetime(2026, 4, 12, 12, 0, 0)
    return base - timedelta(hours=hours_ago)


def _patch_recorder(get_instance_rv=None, get_significant_states_rv=None):
    """Patch the recorder stubs in sys.modules for backfill tests.

    Returns (get_instance_mock, get_significant_states_mock).
    """
    recorder_mod = sys.modules["homeassistant.components.recorder"]
    history_mod = sys.modules["homeassistant.components.recorder.history"]

    mock_instance = get_instance_rv or MagicMock()
    if get_significant_states_rv is not None:
        mock_instance.async_add_executor_job = AsyncMock(
            return_value=get_significant_states_rv
        )

    recorder_mod.get_instance = MagicMock(return_value=mock_instance)
    history_mod.get_significant_states = MagicMock()
    return recorder_mod.get_instance, mock_instance


# ══════════════════════════════════════════════════════════════
#  _replay_water_balance — pure computation, no I/O
# ══════════════════════════════════════════════════════════════


class TestReplayWaterBalance:
    """Unit tests for the chronological ET/rain replay loop."""

    @pytest.fixture
    def sensor(self, hass_mock, base_config):
        return DrynessIndexSensor(hass_mock, base_config)

    def test_empty_history_returns_zero(self, sensor):
        assert sensor._replay_water_balance([], []) == 0.0

    def test_temp_only_accumulates_et(self, sensor):
        """24h at 20°C → ET_h = 0.22*(20-9)/24 ≈ 0.1008 mm/h → ~2.42 mm."""
        t0 = _ts(24)
        t1 = _ts(0)
        temps = [
            _make_recorder_state(20.0, t0),
            _make_recorder_state(20.0, t1),
        ]
        deficit = sensor._replay_water_balance(temps, [])
        expected = 0.22 * (20.0 - 9.0) / 24 * 24  # ≈ 2.42
        assert abs(deficit - expected) < 0.01

    def test_rain_reduces_deficit(self, sensor):
        """ET accumulates, then rain knocks it down."""
        t0 = _ts(24)
        t1 = _ts(12)
        t2 = _ts(0)
        temps = [
            _make_recorder_state(25.0, t0),
            _make_recorder_state(25.0, t2),
        ]
        rain = [_make_recorder_state(5.0, t1)]
        deficit = sensor._replay_water_balance(temps, rain)
        # 12h ET at 25°C, then -5mm rain, then 12h more ET
        et_h = 0.22 * (25.0 - 9.0) / 24
        expected = max(0.0, et_h * 12 - 5.0) + et_h * 12
        assert abs(deficit - expected) < 0.01

    def test_deficit_clamped_at_zero(self, sensor):
        """Heavy rain cannot produce negative deficit (rain after last temp)."""
        t0 = _ts(2)
        t1 = _ts(1)
        t2 = _ts(0)
        temps = [
            _make_recorder_state(15.0, t0),
            _make_recorder_state(15.0, t1),
        ]
        # Rain arrives AFTER last temp → no more ET accumulation
        rain = [_make_recorder_state(100.0, t2)]
        deficit = sensor._replay_water_balance(temps, rain)
        assert deficit == 0.0

    def test_deficit_clamped_at_d_max(self, sensor):
        """Very high temps over long period cap at D_max (100 mm)."""
        t0 = _ts(5000)
        t1 = _ts(0)
        temps = [
            _make_recorder_state(45.0, t0),
            _make_recorder_state(45.0, t1),
        ]
        deficit = sensor._replay_water_balance(temps, [])
        assert deficit == sensor._d_max

    def test_unknown_states_skipped(self, sensor):
        """States with 'unknown' or 'unavailable' are filtered out."""
        t0 = _ts(12)
        t1 = _ts(6)
        t2 = _ts(0)
        temps = [
            _make_recorder_state(20.0, t0),
            _make_recorder_state("unknown", t1),
            _make_recorder_state(20.0, t2),
        ]
        deficit = sensor._replay_water_balance(temps, [])
        # Only the t0→t2 interval counts (12h)
        expected = 0.22 * (20.0 - 9.0) / 24 * 12
        assert abs(deficit - expected) < 0.01

    def test_chronological_ordering(self, sensor):
        """Out-of-order input states are sorted correctly."""
        t0 = _ts(24)
        t1 = _ts(0)
        # Feed in reverse order
        temps = [
            _make_recorder_state(20.0, t1),
            _make_recorder_state(20.0, t0),
        ]
        deficit = sensor._replay_water_balance(temps, [])
        expected = 0.22 * (20.0 - 9.0) / 24 * 24
        assert abs(deficit - expected) < 0.01

    def test_mixed_timeline(self, sensor):
        """Realistic sequence: T rises, rain mid-day, T drops."""
        base = datetime(2026, 4, 12, 18, 0, 0)
        t0 = base - timedelta(hours=12)
        t1 = base - timedelta(hours=8)
        t2 = base - timedelta(hours=6)  # rain
        t3 = base - timedelta(hours=4)
        t4 = base

        temps = [
            _make_recorder_state(15.0, t0),
            _make_recorder_state(22.0, t1),
            _make_recorder_state(18.0, t3),
            _make_recorder_state(18.0, t4),
        ]
        rain = [_make_recorder_state(3.0, t2)]

        deficit = sensor._replay_water_balance(temps, rain)
        assert deficit >= 0.0

    def test_below_t_base_no_et(self, sensor):
        """Temperatures below T_base produce zero ET."""
        t0 = _ts(24)
        t1 = _ts(0)
        temps = [
            _make_recorder_state(5.0, t0),
            _make_recorder_state(5.0, t1),
        ]
        deficit = sensor._replay_water_balance(temps, [])
        assert deficit == 0.0


# ══════════════════════════════════════════════════════════════
#  _compute_backfill_rain_delta
# ══════════════════════════════════════════════════════════════


class TestBackfillRainDelta:
    """Unit tests for rain delta computation in backfill replay."""

    @pytest.fixture
    def sensor_event(self, hass_mock, base_config):
        """Sensor with event rain type (default)."""
        return DrynessIndexSensor(hass_mock, base_config)

    @pytest.fixture
    def sensor_daily(self, hass_mock, base_config):
        """Sensor with daily_total rain type."""
        config = {**base_config, "rain_sensor_type": RAIN_TYPE_DAILY_TOTAL}
        return DrynessIndexSensor(hass_mock, config)

    def test_event_new_value(self, sensor_event):
        assert sensor_event._compute_backfill_rain_delta(2.0, 0.0) == 2.0

    def test_event_same_value_dedup(self, sensor_event):
        assert sensor_event._compute_backfill_rain_delta(2.0, 2.0) == 0.0

    def test_daily_total_positive_delta(self, sensor_daily):
        assert sensor_daily._compute_backfill_rain_delta(8.0, 5.0) == 3.0

    def test_daily_total_midnight_reset(self, sensor_daily):
        # 8mm → 1mm = counter reset, delta = 1mm
        assert sensor_daily._compute_backfill_rain_delta(1.0, 8.0) == 1.0

    def test_negative_rain_clamped(self, sensor_event):
        assert sensor_event._compute_backfill_rain_delta(-1.0, 0.0) == 0.0


# ══════════════════════════════════════════════════════════════
#  _backfill_from_recorder — integration tests (mocked recorder)
# ══════════════════════════════════════════════════════════════


class TestBackfillFromRecorder:
    """Test the async backfill orchestration with mocked recorder."""

    @pytest.fixture
    def sensor(self, hass_mock, base_config):
        s = DrynessIndexSensor(hass_mock, base_config)
        s.async_write_ha_state = MagicMock()
        return s

    @pytest.mark.asyncio
    async def test_backfill_sets_deficit(self, sensor):
        """Full path: recorder returns temp states, deficit is computed."""
        t0 = _ts(24)
        t1 = _ts(0)
        temp_states = [
            _make_recorder_state(20.0, t0),
            _make_recorder_state(20.0, t1),
        ]

        _patch_recorder(
            get_significant_states_rv={
                sensor._temp_sensor: temp_states,
                sensor._rain_sensor: [],
            }
        )

        await sensor._backfill_from_recorder()

        expected = 0.22 * (20.0 - 9.0) / 24 * 24
        assert abs(sensor._deficit - expected) < 0.01
        sensor.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_backfill_graceful_on_db_error(self, sensor):
        """Exception from recorder query yields D=0."""
        _, mock_instance = _patch_recorder()
        mock_instance.async_add_executor_job = AsyncMock(
            side_effect=RuntimeError("DB gone")
        )

        await sensor._backfill_from_recorder()
        assert sensor._deficit == 0.0

    @pytest.mark.asyncio
    async def test_backfill_empty_history(self, sensor):
        """Recorder returns empty dict; deficit stays 0."""
        _patch_recorder(get_significant_states_rv={})

        await sensor._backfill_from_recorder()
        assert sensor._deficit == 0.0

    @pytest.mark.asyncio
    async def test_backfill_no_temp_history(self, sensor):
        """Only rain history, no temp → D=0."""
        _patch_recorder(
            get_significant_states_rv={
                sensor._rain_sensor: [_make_recorder_state(5.0, _ts(12))],
            }
        )

        await sensor._backfill_from_recorder()
        assert sensor._deficit == 0.0

    @pytest.mark.asyncio
    async def test_backfill_instance_none(self, sensor):
        """get_instance returns None → graceful exit."""
        recorder_mod = sys.modules["homeassistant.components.recorder"]
        recorder_mod.get_instance = MagicMock(return_value=None)

        await sensor._backfill_from_recorder()
        assert sensor._deficit == 0.0


# ══════════════════════════════════════════════════════════════
#  async_added_to_hass — backfill trigger logic
# ══════════════════════════════════════════════════════════════


class TestAsyncAddedToHassBackfill:
    """Test that backfill is triggered (or not) in async_added_to_hass."""

    @pytest.fixture
    def sensor(self, hass_mock, base_config):
        s = DrynessIndexSensor(hass_mock, base_config)
        s.async_write_ha_state = MagicMock()
        s._backfill_from_recorder = AsyncMock()
        return s

    @pytest.mark.asyncio
    async def test_backfill_called_when_no_restore_state(self, sensor):
        """No prior state → backfill is called."""
        sensor.async_get_last_state = AsyncMock(return_value=None)
        await sensor.async_added_to_hass()
        sensor._backfill_from_recorder.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_backfill_not_called_when_state_restored(self, sensor):
        """Valid prior state → backfill is NOT called."""
        last = MagicMock()
        last.state = "12.5"
        sensor.async_get_last_state = AsyncMock(return_value=last)
        await sensor.async_added_to_hass()
        sensor._backfill_from_recorder.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backfill_called_on_unknown_state(self, sensor):
        """Prior state is 'unknown' → backfill IS triggered."""
        last = MagicMock()
        last.state = "unknown"
        sensor.async_get_last_state = AsyncMock(return_value=last)
        await sensor.async_added_to_hass()
        sensor._backfill_from_recorder.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_backfill_called_on_unavailable_state(self, sensor):
        """Prior state is 'unavailable' → backfill IS triggered."""
        last = MagicMock()
        last.state = "unavailable"
        sensor.async_get_last_state = AsyncMock(return_value=last)
        await sensor.async_added_to_hass()
        sensor._backfill_from_recorder.assert_awaited_once()
