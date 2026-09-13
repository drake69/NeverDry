"""Tests for the domain Zone — the object behind IrrigationZoneSensor.

Named ``test_zone_domain`` to keep it apart from the six ``test_zone_*`` files,
which exercise the Home Assistant entity. This one is about the class that owns
the deficit, and in particular about ``credit_delivery``: the single home of a
formula that used to be written out at four call sites, two of which subtracted
from a cycle snapshot and one from the live value.
"""

from dataclasses import dataclass
from datetime import datetime

import pytest
from never_dry.water_balance_model import ReferenceFrame
from never_dry.zone import CycleSoakRule, Delivery, Placement, WaterCounters, Zone

AUG = datetime(2026, 8, 9, 7, 0)


@dataclass
class FakeDelivery:
    """Anything that reports delivered litres can settle a zone."""

    liters_delivered: float
    elapsed_s: float = 0.0


def make_zone(**kwargs) -> Zone:
    defaults = {"name": "lawn", "area_m2": 100.0, "efficiency": 0.80}
    return Zone(**{**defaults, **kwargs})


class TestPlacement:
    """The matrix that makes the categorical necessary."""

    def test_only_outdoor_receives_rain(self):
        assert Placement.OUTDOOR.receives_rain
        assert not Placement.PATIO.receives_rain
        assert not Placement.GREENHOUSE.receives_rain
        assert not Placement.INDOOR.receives_rain

    def test_patio_is_rainless_but_still_weather_driven(self):
        """The row that justifies four values instead of a boolean.

        A covered terrace feels the same heat and wind as the lawn, yet no rain
        reaches it — so "receives rain" and "is outdoors" are independent.
        """
        assert not Placement.PATIO.receives_rain
        assert Placement.PATIO.driven_by_outdoor_et

    def test_indoor_is_neither(self):
        assert not Placement.INDOOR.driven_by_outdoor_et

    def test_default_placement_is_outdoor(self):
        assert make_zone().placement is Placement.OUTDOOR


class TestAccumulate:
    def test_integrates_et_scaled_by_kc_and_exposure(self):
        zone = make_zone(microclimate_factor=0.75)
        zone.accumulate(dt_h=24.0, et_h=0.20, base_kc=0.805)
        assert zone.deficit.value_mm == pytest.approx(0.20 * 24 * round(0.805 * 0.75, 4))

    def test_credits_rain_outdoors(self):
        zone = make_zone()
        zone.accumulate(dt_h=24.0, et_h=0.5, base_kc=1.0)
        zone.accumulate(dt_h=0.0, et_h=0.0, base_kc=1.0, rain_mm=5.0)
        assert zone.deficit.value_mm == pytest.approx(7.0)

    @pytest.mark.parametrize("placement", [Placement.PATIO, Placement.GREENHOUSE, Placement.INDOOR])
    def test_withholds_rain_where_it_cannot_fall(self, placement):
        """Crediting rain to a covered zone would silently under-water it."""
        zone = make_zone(placement=placement)
        zone.accumulate(dt_h=24.0, et_h=0.5, base_kc=1.0)
        before = zone.deficit.value_mm
        zone.accumulate(dt_h=0.0, et_h=0.0, base_kc=1.0, rain_mm=5.0)
        assert zone.deficit.value_mm == before

    def test_clamps_at_zero(self):
        zone = make_zone()
        zone.accumulate(dt_h=1.0, et_h=1.0, base_kc=1.0, rain_mm=99.0)
        assert zone.deficit.value_mm == 0.0

    def test_clamps_at_d_max(self):
        zone = make_zone(d_max=10.0)
        zone.accumulate(dt_h=100.0, et_h=1.0, base_kc=1.0)
        assert zone.deficit.value_mm == 10.0

    def test_effective_kc_applies_the_exposure_factor(self):
        assert make_zone(microclimate_factor=0.75).effective_kc(0.80) == 0.60

    def test_a_new_zone_starts_at_zero_in_the_et_frame(self):
        """Reference model D4: no seeding from a shared reference."""
        zone = make_zone()
        assert zone.deficit.value_mm == 0.0
        assert zone.deficit.frame is ReferenceFrame.ET
        assert zone.deficit.source == "lawn"


class TestWaterDemand:
    def test_converts_mm_to_litres_over_the_area(self):
        zone = make_zone(area_m2=100.0, efficiency=0.80)
        zone.deficit = zone.deficit.with_value(10.0)
        assert zone.water_demand_l == pytest.approx(1250.0)

    def test_zero_efficiency_is_not_a_division_error(self):
        zone = make_zone(efficiency=0.0)
        zone.deficit = zone.deficit.with_value(10.0)
        assert zone.water_demand_l == 0.0

    def test_needs_water_at_the_threshold(self):
        zone = make_zone(threshold_mm=20.0)
        zone.deficit = zone.deficit.with_value(20.0)
        assert zone.needs_water

    def test_does_not_need_water_below_it(self):
        zone = make_zone(threshold_mm=20.0)
        zone.deficit = zone.deficit.with_value(19.9)
        assert not zone.needs_water

    def test_delivered_mm_guards_a_zero_area(self):
        assert make_zone(area_m2=0.0).delivered_mm(100.0) == 0.0


class TestCrediting:
    """The one formula. Its contract is that repeated credits do not double-count."""

    def test_intermediate_credits_are_idempotent(self):
        """A flow meter reports cumulative litres, so each credit is absolute.

        500 then 1000 then 1500 must land where a single 1500 would, not 3000
        below it.
        """
        zone = make_zone(area_m2=100.0, efficiency=0.80)
        zone.deficit = zone.deficit.with_value(24.0)
        zone.begin_cycle()
        for cumulative in (500.0, 1000.0, 1500.0):
            zone.credit_delivery(FakeDelivery(cumulative))
        assert zone.deficit.value_mm == pytest.approx(12.0)

    def test_without_a_cycle_it_subtracts_from_the_current_value(self):
        """The manual path opens no cycle; it must credit against the live deficit."""
        zone = make_zone(area_m2=100.0, efficiency=0.80)
        zone.deficit = zone.deficit.with_value(24.0)
        zone.credit_delivery(FakeDelivery(500.0))
        zone.credit_delivery(FakeDelivery(500.0))
        assert zone.deficit.value_mm == pytest.approx(16.0)

    def test_over_delivery_clamps_at_zero(self):
        zone = make_zone(area_m2=100.0, efficiency=0.80)
        zone.deficit = zone.deficit.with_value(4.0)
        zone.credit_delivery(FakeDelivery(9999.0))
        assert zone.deficit.value_mm == 0.0

    def test_a_delivery_result_shape_satisfies_the_protocol(self):
        assert isinstance(FakeDelivery(1.0, 2.0), Delivery)


class TestSettleAndMark:
    def test_settle_credits_stamps_and_closes_the_cycle(self):
        zone = make_zone(area_m2=100.0, efficiency=0.80)
        zone.deficit = zone.deficit.with_value(24.0)
        zone.begin_cycle()
        zone.settle(FakeDelivery(1500.0, 900.0), source="schedule", at=AUG)
        assert zone.deficit.value_mm == pytest.approx(12.0)
        assert zone.counters.last_volume_l == 1500.0
        assert zone.last_source == "schedule"
        assert zone.last_irrigated == AUG
        assert zone.last_duration_s == 900
        assert zone.cycle_baseline_mm is None

    def test_settling_twice_does_not_move_the_deficit_again(self):
        """The snapshot is dropped on settle, so a repeat is not a second subtraction."""
        zone = make_zone(area_m2=100.0, efficiency=0.80)
        zone.deficit = zone.deficit.with_value(24.0)
        zone.begin_cycle()
        zone.settle(FakeDelivery(500.0), source="schedule", at=AUG)
        after_first = zone.deficit.value_mm
        assert after_first == pytest.approx(20.0)

    def test_mark_irrigated_clears_the_deficit_and_infers_the_volume(self):
        """The hose case: nothing was measured, so the volume comes from the deficit."""
        zone = make_zone(area_m2=100.0, efficiency=0.80)
        zone.deficit = zone.deficit.with_value(10.0)
        expected = zone.water_demand_l
        zone.mark_irrigated(source="manual", at=AUG)
        assert zone.deficit.value_mm == 0.0
        assert zone.counters.last_volume_l == pytest.approx(round(expected, 1))
        assert zone.last_source == "manual"


class TestWaterCounters:
    def test_credit_updates_every_total(self):
        counters = WaterCounters()
        counters.credit(120.0, year=2026)
        assert counters.last_volume_l == 120.0
        assert counters.total_water_l == 120.0
        assert counters.yearly_water_l == 120.0
        assert counters.session_water_l == 0.0, "crediting is not what makes a session's running total"

    def test_ending_a_session_clears_only_its_running_total(self):
        """What the run delivered survives in ``last_volume_l``; the tally does not."""
        counters = WaterCounters()
        counters.session_water_l = 18.0
        counters.credit(120.0, year=2026)

        counters.end_session()

        assert counters.session_water_l == 0.0
        assert counters.last_volume_l == 120.0
        assert counters.total_water_l == 120.0

    def test_lifetime_accumulates_while_last_is_replaced(self):
        counters = WaterCounters()
        counters.credit(100.0, year=2026)
        counters.credit(50.0, year=2026)
        assert counters.last_volume_l == 50.0
        assert counters.total_water_l == 150.0

    def test_yearly_rolls_over_but_lifetime_does_not(self):
        counters = WaterCounters()
        counters.credit(100.0, year=2026)
        counters.credit(30.0, year=2027)
        assert counters.yearly_water_l == 30.0
        assert counters.total_water_l == 130.0

    def test_reset_yearly_preserves_the_lifetime_total(self):
        counters = WaterCounters()
        counters.credit(100.0, year=2026)
        counters.reset_yearly(year=2026)
        assert counters.yearly_water_l == 0.0
        assert counters.total_water_l == 100.0


class TestCycleSoakRule:
    def test_inactive_unless_both_halves_are_set(self):
        assert not CycleSoakRule().is_active
        assert not CycleSoakRule(max_segment_s=360).is_active
        assert not CycleSoakRule(soak_s=900).is_active
        assert CycleSoakRule(max_segment_s=360, soak_s=900).is_active
