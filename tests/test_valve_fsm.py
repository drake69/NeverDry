"""Tests for valve_fsm — the pure-Python per-valve state machine."""

from __future__ import annotations

import pytest
from never_dry.valve_fsm import (
    CancelAllTimers,
    CancelTimer,
    EnterMaintenance,
    FailureKind,
    FsmConfig,
    NotifyFailure,
    SendSwitchOff,
    SendSwitchOn,
    StartTimer,
    TimerName,
    ValveEvent,
    ValveFsm,
    ValveState,
)

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def fsm_with_flow():
    """Build a fresh FSM configured with a flow meter."""
    return ValveFsm(FsmConfig(has_flow_meter=True))


@pytest.fixture
def fsm_no_flow():
    """Build a fresh FSM configured without a flow meter."""
    return ValveFsm(FsmConfig(has_flow_meter=False))


def _drive_to(fsm: ValveFsm, *events: ValveEvent) -> None:
    """Dispatch a sequence of events into ``fsm``, discarding the results."""
    for e in events:
        fsm.dispatch(e)


# ── Happy path ────────────────────────────────────────────────────────


def test_initial_state_is_idle(fsm_no_flow):
    """A brand-new FSM starts in IDLE with a zero failure counter."""
    assert fsm_no_flow.state == ValveState.IDLE
    assert fsm_no_flow.failure_count == 0
    assert fsm_no_flow.last_failure is None


def test_happy_path_with_flow_meter(fsm_with_flow):
    """Open + close cycle with flow meter walks through all six L-states cleanly."""
    r = fsm_with_flow.dispatch(ValveEvent.CMD_OPEN)
    assert r.from_state == ValveState.IDLE
    assert r.to_state == ValveState.REQ_OPEN
    assert SendSwitchOn() in r.actions
    assert StartTimer(TimerName.OPEN, 10.0) in r.actions

    r = fsm_with_flow.dispatch(ValveEvent.OBS_SWITCH_ON)
    assert r.to_state == ValveState.OPEN
    assert CancelTimer(TimerName.OPEN) in r.actions
    assert StartTimer(TimerName.FLOW, 10.0) in r.actions

    r = fsm_with_flow.dispatch(ValveEvent.OBS_FLOW_POSITIVE)
    assert r.to_state == ValveState.OPEN_VERIFIED
    assert CancelTimer(TimerName.FLOW) in r.actions

    r = fsm_with_flow.dispatch(ValveEvent.CMD_CLOSE)
    assert r.to_state == ValveState.REQ_CLOSE
    assert SendSwitchOff() in r.actions
    assert StartTimer(TimerName.CLOSE, 10.0) in r.actions

    r = fsm_with_flow.dispatch(ValveEvent.OBS_SWITCH_OFF)
    assert r.to_state == ValveState.CLOSED
    assert CancelTimer(TimerName.CLOSE) in r.actions
    assert StartTimer(TimerName.LEAK, 10.0) in r.actions

    r = fsm_with_flow.dispatch(ValveEvent.OBS_FLOW_ZERO)
    assert r.to_state == ValveState.IDLE
    assert CancelTimer(TimerName.LEAK) in r.actions
    assert fsm_with_flow.failure_count == 0


def test_happy_path_no_flow_meter_skips_verified_and_closed(fsm_no_flow):
    """Without a flow meter the FSM skips OPEN_VERIFIED and CLOSED entirely."""
    r = fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    assert r.to_state == ValveState.REQ_OPEN

    r = fsm_no_flow.dispatch(ValveEvent.OBS_SWITCH_ON)
    assert r.to_state == ValveState.OPEN
    assert not any(isinstance(a, StartTimer) and a.name == TimerName.FLOW for a in r.actions)

    r = fsm_no_flow.dispatch(ValveEvent.CMD_CLOSE)
    assert r.to_state == ValveState.REQ_CLOSE

    r = fsm_no_flow.dispatch(ValveEvent.OBS_SWITCH_OFF)
    assert r.to_state == ValveState.IDLE
    assert not any(isinstance(a, StartTimer) and a.name == TimerName.LEAK for a in r.actions)
    assert fsm_no_flow.failure_count == 0


def test_five_clean_cycles_keep_counter_zero(fsm_no_flow):
    """Repeated clean cycles never bump the failure counter."""
    for _ in range(5):
        _drive_to(
            fsm_no_flow,
            ValveEvent.CMD_OPEN,
            ValveEvent.OBS_SWITCH_ON,
            ValveEvent.CMD_CLOSE,
            ValveEvent.OBS_SWITCH_OFF,
        )
    assert fsm_no_flow.state == ValveState.IDLE
    assert fsm_no_flow.failure_count == 0


# ── Failures ──────────────────────────────────────────────────────────


def test_open_timeout(fsm_no_flow):
    """TIMEOUT_OPEN from REQ_OPEN records OPEN_FAILED and routes back to IDLE."""
    fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    r = fsm_no_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    assert r.to_state == ValveState.IDLE
    assert r.failure == FailureKind.OPEN_FAILED
    assert NotifyFailure(FailureKind.OPEN_FAILED) in r.actions
    assert fsm_no_flow.failure_count == 1
    assert fsm_no_flow.last_failure == FailureKind.OPEN_FAILED


def test_actuation_failure_sends_switch_off(fsm_with_flow):
    """TIMEOUT_FLOW from OPEN emits SendSwitchOff as belt-and-suspenders."""
    _drive_to(fsm_with_flow, ValveEvent.CMD_OPEN, ValveEvent.OBS_SWITCH_ON)
    assert fsm_with_flow.state == ValveState.OPEN
    r = fsm_with_flow.dispatch(ValveEvent.TIMEOUT_FLOW)
    assert r.to_state == ValveState.IDLE
    assert r.failure == FailureKind.ACTUATION_FAILED
    assert SendSwitchOff() in r.actions
    assert NotifyFailure(FailureKind.ACTUATION_FAILED) in r.actions
    assert fsm_with_flow.failure_count == 1


def test_close_verification_failure(fsm_no_flow):
    """TIMEOUT_CLOSE from REQ_CLOSE records CLOSE_VERIFICATION_FAILED."""
    _drive_to(
        fsm_no_flow,
        ValveEvent.CMD_OPEN,
        ValveEvent.OBS_SWITCH_ON,
        ValveEvent.CMD_CLOSE,
    )
    r = fsm_no_flow.dispatch(ValveEvent.TIMEOUT_CLOSE)
    assert r.to_state == ValveState.IDLE
    assert r.failure == FailureKind.CLOSE_VERIFICATION_FAILED
    assert fsm_no_flow.failure_count == 1


def test_close_leak_with_flow_meter(fsm_with_flow):
    """TIMEOUT_LEAK from CLOSED records CLOSE_LEAK — the most dangerous failure."""
    _drive_to(
        fsm_with_flow,
        ValveEvent.CMD_OPEN,
        ValveEvent.OBS_SWITCH_ON,
        ValveEvent.OBS_FLOW_POSITIVE,
        ValveEvent.CMD_CLOSE,
        ValveEvent.OBS_SWITCH_OFF,
    )
    assert fsm_with_flow.state == ValveState.CLOSED
    r = fsm_with_flow.dispatch(ValveEvent.TIMEOUT_LEAK)
    assert r.to_state == ValveState.IDLE
    assert r.failure == FailureKind.CLOSE_LEAK
    assert fsm_with_flow.failure_count == 1


# ── Maintenance ───────────────────────────────────────────────────────


def test_three_consecutive_failures_enter_maintenance(fsm_no_flow):
    """Three consecutive failures lock the FSM in MAINTENANCE."""
    for _ in range(3):
        fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
        fsm_no_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    assert fsm_no_flow.state == ValveState.MAINTENANCE
    assert fsm_no_flow.failure_count == 3


def test_maintenance_emits_enter_action_on_third_failure(fsm_no_flow):
    """The transition that crosses the threshold emits ``EnterMaintenance``."""
    fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    fsm_no_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    fsm_no_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    r = fsm_no_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    assert r.to_state == ValveState.MAINTENANCE
    assert EnterMaintenance() in r.actions


def test_maintenance_blocks_open(fsm_no_flow):
    """CMD_OPEN in MAINTENANCE is a no-op (state and actions unchanged)."""
    for _ in range(3):
        fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
        fsm_no_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    r = fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    assert r.to_state == ValveState.MAINTENANCE
    assert r.actions == ()


def test_maintenance_reset_returns_to_idle(fsm_no_flow):
    """CMD_RESET is the only way out of MAINTENANCE; it also clears the counter."""
    for _ in range(3):
        fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
        fsm_no_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    r = fsm_no_flow.dispatch(ValveEvent.CMD_RESET)
    assert r.to_state == ValveState.IDLE
    assert fsm_no_flow.failure_count == 0
    assert fsm_no_flow.last_failure is None


# ── Counter reset ─────────────────────────────────────────────────────


def test_counter_resets_after_clean_cycle(fsm_no_flow):
    """A clean open+close cycle clears the counter built up by an earlier failure."""
    fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    fsm_no_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    assert fsm_no_flow.failure_count == 1
    _drive_to(
        fsm_no_flow,
        ValveEvent.CMD_OPEN,
        ValveEvent.OBS_SWITCH_ON,
        ValveEvent.CMD_CLOSE,
        ValveEvent.OBS_SWITCH_OFF,
    )
    assert fsm_no_flow.failure_count == 0
    assert fsm_no_flow.last_failure is None


def test_counter_resets_after_clean_cycle_with_flow_meter(fsm_with_flow):
    """Same counter-reset rule applies on the flow-meter path."""
    fsm_with_flow.dispatch(ValveEvent.CMD_OPEN)
    fsm_with_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    assert fsm_with_flow.failure_count == 1
    _drive_to(
        fsm_with_flow,
        ValveEvent.CMD_OPEN,
        ValveEvent.OBS_SWITCH_ON,
        ValveEvent.OBS_FLOW_POSITIVE,
        ValveEvent.CMD_CLOSE,
        ValveEvent.OBS_SWITCH_OFF,
        ValveEvent.OBS_FLOW_ZERO,
    )
    assert fsm_with_flow.failure_count == 0


# ── Unavailable / Available ───────────────────────────────────────────


def test_unavailable_from_active_state(fsm_with_flow):
    """OBS_UNAVAILABLE from any active state moves to UNREACHABLE and cancels timers."""
    _drive_to(
        fsm_with_flow,
        ValveEvent.CMD_OPEN,
        ValveEvent.OBS_SWITCH_ON,
        ValveEvent.OBS_FLOW_POSITIVE,
    )
    r = fsm_with_flow.dispatch(ValveEvent.OBS_UNAVAILABLE)
    assert r.to_state == ValveState.UNREACHABLE
    assert CancelAllTimers() in r.actions


def test_available_returns_to_idle(fsm_with_flow):
    """OBS_AVAILABLE recovers the FSM from UNREACHABLE back to IDLE."""
    fsm_with_flow.dispatch(ValveEvent.CMD_OPEN)
    fsm_with_flow.dispatch(ValveEvent.OBS_UNAVAILABLE)
    r = fsm_with_flow.dispatch(ValveEvent.OBS_AVAILABLE)
    assert r.to_state == ValveState.IDLE


def test_unavailable_preserves_failure_counter(fsm_no_flow):
    """A round-trip through UNREACHABLE must not touch the failure counter."""
    fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    fsm_no_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    assert fsm_no_flow.failure_count == 1
    fsm_no_flow.dispatch(ValveEvent.OBS_UNAVAILABLE)
    fsm_no_flow.dispatch(ValveEvent.OBS_AVAILABLE)
    assert fsm_no_flow.state == ValveState.IDLE
    assert fsm_no_flow.failure_count == 1


def test_unavailable_when_already_unreachable_is_noop(fsm_no_flow):
    """A redundant OBS_UNAVAILABLE in UNREACHABLE emits no actions."""
    fsm_no_flow.dispatch(ValveEvent.OBS_UNAVAILABLE)
    r = fsm_no_flow.dispatch(ValveEvent.OBS_UNAVAILABLE)
    assert r.to_state == ValveState.UNREACHABLE
    assert r.actions == ()


# ── Emergency close (CMD_CLOSE in transition states) ──────────────────


def test_cmd_close_interrupts_req_open(fsm_no_flow):
    """CMD_CLOSE during REQ_OPEN cancels the open timer and starts a full close cycle."""
    fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    r = fsm_no_flow.dispatch(ValveEvent.CMD_CLOSE)
    assert r.to_state == ValveState.REQ_CLOSE
    assert CancelTimer(TimerName.OPEN) in r.actions
    assert SendSwitchOff() in r.actions
    assert StartTimer(TimerName.CLOSE, 10.0) in r.actions


def test_cmd_close_interrupts_open_waiting_for_flow(fsm_with_flow):
    """CMD_CLOSE during OPEN cancels the flow timer before driving the close cycle."""
    _drive_to(fsm_with_flow, ValveEvent.CMD_OPEN, ValveEvent.OBS_SWITCH_ON)
    r = fsm_with_flow.dispatch(ValveEvent.CMD_CLOSE)
    assert r.to_state == ValveState.REQ_CLOSE
    assert CancelTimer(TimerName.FLOW) in r.actions
    assert SendSwitchOff() in r.actions


# ── Out-of-place events are no-ops ────────────────────────────────────


def test_obs_flow_zero_in_idle_is_noop(fsm_with_flow):
    """OBS_FLOW_ZERO is meaningless in IDLE; the FSM ignores it."""
    r = fsm_with_flow.dispatch(ValveEvent.OBS_FLOW_ZERO)
    assert r.to_state == ValveState.IDLE
    assert r.actions == ()


def test_cmd_open_while_open_is_noop(fsm_no_flow):
    """A redundant CMD_OPEN while already OPEN does nothing."""
    _drive_to(fsm_no_flow, ValveEvent.CMD_OPEN, ValveEvent.OBS_SWITCH_ON)
    r = fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    assert r.to_state == ValveState.OPEN
    assert r.actions == ()


def test_cmd_close_in_idle_is_noop(fsm_no_flow):
    """CMD_CLOSE in IDLE has nothing to close, so it is a no-op."""
    r = fsm_no_flow.dispatch(ValveEvent.CMD_CLOSE)
    assert r.to_state == ValveState.IDLE
    assert r.actions == ()


def test_no_flow_meter_ignores_timeout_flow_in_open(fsm_no_flow):
    """Without a flow meter the FSM must not react to TIMEOUT_FLOW."""
    _drive_to(fsm_no_flow, ValveEvent.CMD_OPEN, ValveEvent.OBS_SWITCH_ON)
    r = fsm_no_flow.dispatch(ValveEvent.TIMEOUT_FLOW)
    assert r.to_state == ValveState.OPEN
    assert r.actions == ()


# ── Per-state no-op coverage (every handler's fall-through) ──────────


def test_noop_branches_for_each_state(fsm_with_flow):
    """Each state handler's fall-through path returns a no-op result."""
    # IDLE + unexpected event (covered by other tests, sanity check)
    r = fsm_with_flow.dispatch(ValveEvent.TIMEOUT_OPEN)
    assert r.actions == ()

    # REQ_OPEN + unexpected event (e.g. OBS_FLOW_POSITIVE before switch-on)
    fsm_with_flow.dispatch(ValveEvent.CMD_OPEN)
    r = fsm_with_flow.dispatch(ValveEvent.OBS_FLOW_POSITIVE)
    assert r.from_state == ValveState.REQ_OPEN
    assert r.to_state == ValveState.REQ_OPEN
    assert r.actions == ()

    # OPEN (with flow meter) + unexpected event
    fsm_with_flow.dispatch(ValveEvent.OBS_SWITCH_ON)
    r = fsm_with_flow.dispatch(ValveEvent.OBS_SWITCH_ON)
    assert r.to_state == ValveState.OPEN
    assert r.actions == ()

    # OPEN_VERIFIED + unexpected event
    fsm_with_flow.dispatch(ValveEvent.OBS_FLOW_POSITIVE)
    r = fsm_with_flow.dispatch(ValveEvent.OBS_FLOW_POSITIVE)
    assert r.to_state == ValveState.OPEN_VERIFIED
    assert r.actions == ()

    # REQ_CLOSE + unexpected event
    fsm_with_flow.dispatch(ValveEvent.CMD_CLOSE)
    r = fsm_with_flow.dispatch(ValveEvent.OBS_FLOW_POSITIVE)
    assert r.to_state == ValveState.REQ_CLOSE
    assert r.actions == ()

    # CLOSED + unexpected event
    fsm_with_flow.dispatch(ValveEvent.OBS_SWITCH_OFF)
    r = fsm_with_flow.dispatch(ValveEvent.OBS_SWITCH_ON)
    assert r.to_state == ValveState.CLOSED
    assert r.actions == ()


def test_unreachable_ignores_non_available_events(fsm_no_flow):
    """In UNREACHABLE every event other than OBS_AVAILABLE is a no-op."""
    fsm_no_flow.dispatch(ValveEvent.OBS_UNAVAILABLE)
    assert fsm_no_flow.state == ValveState.UNREACHABLE
    r = fsm_no_flow.dispatch(ValveEvent.CMD_OPEN)
    assert r.to_state == ValveState.UNREACHABLE
    assert r.actions == ()


def test_no_flow_meter_does_not_use_closed_state(fsm_no_flow):
    """ValveState.CLOSED must never be reached when ``has_flow_meter`` is False."""
    for _ in range(10):
        _drive_to(
            fsm_no_flow,
            ValveEvent.CMD_OPEN,
            ValveEvent.OBS_SWITCH_ON,
            ValveEvent.CMD_CLOSE,
            ValveEvent.OBS_SWITCH_OFF,
        )
        assert fsm_no_flow.state != ValveState.CLOSED
