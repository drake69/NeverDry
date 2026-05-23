"""Tests for valve_operator — the HA-aware wrapper around ValveFsm."""

from __future__ import annotations

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock

import pytest
from never_dry.valve_fsm import FailureKind, FsmConfig, ValveState
from never_dry.valve_operator import OperationResult, OperationStatus, ValveOperator

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def hass():
    """Mock HomeAssistant instance suitable for ValveOperator tests."""
    hass = MagicMock()
    hass.states = MagicMock()
    hass.states.get = MagicMock(return_value=MagicMock(state="off"))
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.async_create_task = lambda coro: asyncio.ensure_future(coro)
    return hass


def _fast_fsm_config(has_flow_meter: bool) -> FsmConfig:
    """Return an FSM config with tiny timeouts for snappy tests."""
    return FsmConfig(
        has_flow_meter=has_flow_meter,
        open_timeout_s=0.05,
        close_timeout_s=0.05,
        flow_verify_timeout_s=0.05,
        leak_timeout_s=0.05,
        max_consecutive_failures=3,
    )


def _make_operator(
    hass,
    *,
    has_flow_meter: bool = False,
    max_retries: int = 0,
    backoff_s: tuple[float, ...] = (0.01,),
) -> ValveOperator:
    """Build a ValveOperator wired to the mock HA with fast timeouts."""
    return ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        flow_sensor_entity_id="sensor.flow" if has_flow_meter else None,
        zone_name="testzone",
        fsm_config=_fast_fsm_config(has_flow_meter),
        max_retries=max_retries,
        backoff_s=backoff_s,
    )


def _state_event(value: str) -> MagicMock:
    """Build a mock state-change event carrying the given new value."""
    event = MagicMock()
    event.data = {"new_state": MagicMock(state=value)}
    return event


async def _yield_loop(times: int = 3) -> None:
    """Yield to the asyncio loop ``times`` times so scheduled tasks run."""
    for _ in range(times):
        await asyncio.sleep(0)


# ── Initial state ─────────────────────────────────────────────────────


def test_initial_state_is_idle(hass):
    """A fresh operator starts in IDLE, not in maintenance."""
    op = _make_operator(hass)
    assert op.state == ValveState.IDLE
    assert op.is_in_maintenance is False
    assert op.failure_count == 0


# ── Pre-checks ────────────────────────────────────────────────────────


async def test_precheck_switch_entity_not_found(hass):
    """Opening returns PRECHECK_FAILED when the switch entity is missing."""
    hass.states.get.return_value = None
    op = _make_operator(hass)
    result = await op.open()
    assert result.status == OperationStatus.PRECHECK_FAILED
    assert result.error_detail == "switch_entity_not_found"
    hass.services.async_call.assert_not_called()


async def test_precheck_switch_unavailable(hass):
    """Opening returns PRECHECK_FAILED when the switch is unavailable."""
    hass.states.get.return_value = MagicMock(state="unavailable")
    op = _make_operator(hass)
    result = await op.open()
    assert result.status == OperationStatus.PRECHECK_FAILED
    assert result.error_detail == "switch_unavailable"


# ── Happy paths ──────────────────────────────────────────────────────


async def test_open_happy_path_no_flow_meter(hass):
    """Open completes successfully when switch state confirms quickly."""
    op = _make_operator(hass)

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    sim = asyncio.create_task(simulate())
    result = await op.open()
    await sim

    assert result.status == OperationStatus.OK
    assert result.retries_used == 0
    assert result.duration_ms > 0
    hass.services.async_call.assert_any_call("switch", "turn_on", {"entity_id": "switch.valve"}, blocking=False)
    assert op.state == ValveState.OPEN


async def test_open_happy_path_with_flow_meter(hass):
    """Open completes when both switch and flow confirm."""
    op = _make_operator(hass, has_flow_meter=True)

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    sim = asyncio.create_task(simulate())
    result = await op.open()
    await sim

    assert result.status == OperationStatus.OK
    assert op.state == ValveState.OPEN_VERIFIED


async def test_close_happy_path_no_flow_meter(hass):
    """Close completes when switch reports off."""
    op = _make_operator(hass)

    # Drive the FSM to OPEN first.
    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    _bg = asyncio.create_task(open_sim())
    await op.open()
    await _bg

    async def close_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))

    sim = asyncio.create_task(close_sim())
    result = await op.close()
    await sim

    assert result.status == OperationStatus.OK
    hass.services.async_call.assert_any_call("switch", "turn_off", {"entity_id": "switch.valve"}, blocking=False)
    assert op.state == ValveState.IDLE


async def test_close_happy_path_with_flow_meter(hass):
    """Close completes when flow drops to zero after switch off."""
    op = _make_operator(hass, has_flow_meter=True)

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    _bg = asyncio.create_task(open_sim())
    await op.open()
    await _bg

    async def close_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.0"))

    sim = asyncio.create_task(close_sim())
    result = await op.close()
    await sim

    assert result.status == OperationStatus.OK
    assert op.state == ValveState.IDLE
    assert op.failure_count == 0


# ── Retries on transient failures ────────────────────────────────────


async def test_open_fails_when_switch_never_confirms(hass):
    """With max_retries=0, an open-timeout returns FAILED immediately."""
    op = _make_operator(hass, max_retries=0)
    result = await op.open()
    assert result.status == OperationStatus.FAILED
    assert result.error_detail == FailureKind.OPEN_FAILED.value
    assert result.retries_used == 0


async def test_open_succeeds_after_one_retry(hass):
    """A transient open failure is retried; the second attempt succeeds."""
    op = _make_operator(hass, max_retries=2, backoff_s=(0.0,))
    attempt = {"count": 0}

    async def watcher():
        # Wait until the SECOND attempt has been dispatched (= retry).
        while attempt["count"] < 2:
            await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    # Track service calls to count attempts.
    real_call = hass.services.async_call

    async def counting_call(*args, **kwargs):
        if args[:2] == ("switch", "turn_on"):
            attempt["count"] += 1
        return await real_call(*args, **kwargs)

    hass.services.async_call = counting_call

    sim = asyncio.create_task(watcher())
    result = await op.open()
    await sim

    assert result.status == OperationStatus.OK
    assert result.retries_used >= 1


async def test_actuation_failure_not_retried(hass):
    """Switch on but no flow → ACTUATION_FAILED returns immediately."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=5, backoff_s=(0.0,))

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        # No flow event → flow timer expires.

    sim = asyncio.create_task(simulate())
    result = await op.open()
    await sim

    assert result.status == OperationStatus.FAILED
    assert result.error_detail == FailureKind.ACTUATION_FAILED.value
    assert result.retries_used == 0


# ── AI-032: leak recovery + escalation ───────────────────────────────


async def _drive_open_then_close_leak(op, hass):
    """Helper: drive the operator to a CLOSE_LEAK failure and return the result."""

    async def open_sim():
        """Take the operator from IDLE to OPEN_VERIFIED."""
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    _bg = asyncio.create_task(open_sim())
    await op.open()
    await _bg

    async def close_sim():
        """Confirm switch off but leave flow positive → leak."""
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))

    sim = asyncio.create_task(close_sim())
    return await op.close(), sim


async def test_close_leak_recovery_succeeds(hass):
    """If the post-leak ``turn_off`` makes the flow drop, close returns OK."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=0, backoff_s=(0.0,))

    flow_value = {"value": "0.5"}

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state=flow_value["value"])
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)

    real_call = hass.services.async_call

    async def call_then_drop_flow(*args, **kwargs):
        if args[:2] == ("switch", "turn_off"):
            flow_value["value"] = "0.0"
        return await real_call(*args, **kwargs)

    hass.services.async_call = call_then_drop_flow

    result, sim = await _drive_open_then_close_leak(op, hass)
    await sim

    assert result.status == OperationStatus.OK
    assert result.error_detail == "leak_recovered"


async def test_close_leak_recovery_fails_triggers_emergency_stop(hass):
    """When recovery cannot clear the leak, ``never_dry.stop`` is invoked."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=0, backoff_s=(0.0,))

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state="0.5")
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)

    result, sim = await _drive_open_then_close_leak(op, hass)
    await sim

    assert result.status == OperationStatus.FAILED
    assert result.error_detail == FailureKind.CLOSE_LEAK.value

    stop_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("never_dry", "stop")]
    assert len(stop_calls) == 1


async def test_close_leak_recovery_attempted_once(hass):
    """The recovery flag prevents a second attempt within the same close()."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=0, backoff_s=(0.0,))

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state="0.5")
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)

    result, sim = await _drive_open_then_close_leak(op, hass)
    await sim

    # Two turn_off calls: one from the FSM during REQ_CLOSE, one from
    # the recovery attempt. Never three.
    turn_off_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("switch", "turn_off")]
    assert 1 <= len(turn_off_calls) <= 2
    assert result.status == OperationStatus.FAILED


async def test_close_leak_recovery_resets_between_close_calls(hass):
    """A second close() call must be able to retry recovery again."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=0, backoff_s=(0.0,))

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state="0.5")
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)

    # First close → leak + recovery attempt
    _, sim1 = await _drive_open_then_close_leak(op, hass)
    await sim1

    # Operator went through MAINTENANCE? Reset it.
    if op.is_in_maintenance:
        await op.reset_maintenance()

    # Re-open and re-leak
    _, sim2 = await _drive_open_then_close_leak(op, hass)
    await sim2

    # Both attempts should have called never_dry.stop independently.
    stop_calls = [c for c in hass.services.async_call.call_args_list if c.args[:2] == ("never_dry", "stop")]
    assert len(stop_calls) >= 2


async def test_close_leak_not_retried(hass):
    """Switch off but flow persists → CLOSE_LEAK returns immediately, no retry."""
    op = _make_operator(hass, has_flow_meter=True, max_retries=5, backoff_s=(0.0,))

    async def open_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))
        await _yield_loop()
        await op._handle_flow_state(_state_event("0.5"))

    _bg = asyncio.create_task(open_sim())
    await op.open()
    await _bg

    async def close_sim():
        await _yield_loop()
        await op._handle_switch_state(_state_event("off"))
        # Flow stays > threshold; leak timer expires.

    sim = asyncio.create_task(close_sim())
    result = await op.close()
    await sim

    assert result.status == OperationStatus.FAILED
    assert result.error_detail == FailureKind.CLOSE_LEAK.value
    assert result.retries_used == 0


# ── Maintenance ──────────────────────────────────────────────────────


async def test_three_consecutive_failures_enter_maintenance(hass):
    """Three open timeouts in a row lock the operator in MAINTENANCE."""
    op = _make_operator(hass, max_retries=0)
    for _ in range(3):
        await op.open()
    assert op.is_in_maintenance is True


async def test_open_in_maintenance_returns_maintenance_status(hass):
    """Once locked, open() refuses without touching switch services."""
    op = _make_operator(hass, max_retries=0)
    for _ in range(3):
        await op.open()
    hass.services.async_call.reset_mock()
    result = await op.open()
    assert result.status == OperationStatus.MAINTENANCE
    hass.services.async_call.assert_not_called()


async def test_reset_maintenance_clears_state(hass):
    """``reset_maintenance`` returns the operator to IDLE with a zero counter."""
    op = _make_operator(hass, max_retries=0)
    for _ in range(3):
        await op.open()
    await op.reset_maintenance()
    assert op.is_in_maintenance is False
    assert op.failure_count == 0


# ── Unavailable / available ──────────────────────────────────────────


async def test_switch_unavailable_during_op_moves_to_unreachable(hass):
    """An unavailable observation during an open cycle parks the FSM in UNREACHABLE."""
    op = _make_operator(hass, max_retries=0)

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("unavailable"))

    sim = asyncio.create_task(simulate())
    # The open will not complete OK; we expect FAILED or be parked. Use a
    # short timeout to avoid hanging if the operator misbehaves.
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(op.open(), timeout=0.2)
    await sim
    assert op.state == ValveState.UNREACHABLE


# ── Unload ───────────────────────────────────────────────────────────


def test_async_unload_releases_subscriptions(hass):
    """Unload calls the unsubscribe handles returned by the HA helper."""
    op = _make_operator(hass, has_flow_meter=True)
    op.async_unload()
    # async_track_state_change_event is mocked to MagicMock(); calling its
    # return value should have been requested by async_unload.
    assert op._unsub_switch.called
    assert op._unsub_flow.called


# ── Service exception ────────────────────────────────────────────────


async def test_switch_service_exception_is_caught(hass, caplog):
    """A raising switch service does not crash the operator."""
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    op = _make_operator(hass, max_retries=0)
    result = await op.open()
    # The FSM still drives to OPEN_FAILED via the open timeout.
    assert result.status == OperationStatus.FAILED
    assert "boom" in caplog.text


# ── Coverage of less-traveled branches ───────────────────────────────


async def test_is_retryable_rejects_garbage_error_detail(hass):
    """An unknown error_detail string is treated as non-transient."""
    op = _make_operator(hass)
    outcome = OperationResult(OperationStatus.FAILED, "not_a_real_failure_kind")
    assert op._is_retryable(outcome) is False


async def test_backoff_for_handles_empty_tuple(hass):
    """An empty backoff tuple yields a zero sleep."""
    op = _make_operator(hass, backoff_s=())
    assert op._backoff_for(0) == 0.0


def test_make_operator_uses_default_backoff_when_none(hass):
    """Passing ``backoff_s=None`` activates the class default."""
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="z",
        fsm_config=_fast_fsm_config(False),
        backoff_s=None,
    )
    assert op._backoff_s == ValveOperator.DEFAULT_BACKOFF_S


async def test_notifier_receives_command_failed_on_open_fail(hass):
    """An OPEN_FAILED with a notifier configured produces a notification."""
    from never_dry.valve_notifier import NotificationKind, Severity, ValveNotifier

    notifier = ValveNotifier(hass)
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="z1",
        fsm_config=_fast_fsm_config(False),
        max_retries=0,
        backoff_s=(0.0,),
        notifier=notifier,
    )
    result = await op.open()
    assert result.status == OperationStatus.FAILED
    assert notifier.is_active("z1", NotificationKind.COMMAND_FAILED)
    active = notifier._active[("z1", NotificationKind.COMMAND_FAILED)]
    assert active.severity == Severity.WARNING


async def test_notifier_receives_stuck_open_on_close_leak(hass):
    """A CLOSE_LEAK with a notifier configured emits STUCK_OPEN CRITICAL."""
    from never_dry.valve_notifier import NotificationKind, Severity, ValveNotifier

    notifier = ValveNotifier(hass)
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        flow_sensor_entity_id="sensor.flow",
        zone_name="z2",
        fsm_config=_fast_fsm_config(True),
        max_retries=0,
        backoff_s=(0.0,),
        notifier=notifier,
    )

    def _state_for(entity_id):
        if entity_id == "sensor.flow":
            return MagicMock(state="0.5")
        return MagicMock(state="off")

    hass.states.get = MagicMock(side_effect=_state_for)
    result, sim = await _drive_open_then_close_leak(op, hass)
    await sim

    assert result.status == OperationStatus.FAILED
    assert notifier.is_active("z2", NotificationKind.STUCK_OPEN)
    active = notifier._active[("z2", NotificationKind.STUCK_OPEN)]
    assert active.severity == Severity.CRITICAL


async def test_notifier_receives_zone_disabled_on_maintenance(hass):
    """Three consecutive failures notify ZONE_DISABLED with CRITICAL severity."""
    from never_dry.valve_notifier import NotificationKind, ValveNotifier

    notifier = ValveNotifier(hass)
    op = ValveOperator(
        hass=hass,
        switch_entity_id="switch.valve",
        zone_name="z3",
        fsm_config=_fast_fsm_config(False),
        max_retries=0,
        backoff_s=(0.0,),
        notifier=notifier,
    )
    for _ in range(3):
        await op.open()
    assert notifier.is_active("z3", NotificationKind.ZONE_DISABLED)


async def test_leak_recovery_no_flow_meter_treats_as_unrecovered(hass):
    """Without a flow meter, _attempt_leak_recovery returns False."""
    op = _make_operator(hass, has_flow_meter=False)
    op._flow_sensor_entity_id = None
    recovered = await op._attempt_leak_recovery()
    assert recovered is False


async def test_leak_recovery_none_state_treats_as_unrecovered(hass):
    """If hass.states.get returns None during recovery, treat as unrecovered."""
    op = _make_operator(hass, has_flow_meter=True)
    hass.states.get = MagicMock(return_value=None)
    recovered = await op._attempt_leak_recovery()
    assert recovered is False


async def test_leak_recovery_unparseable_flow_treats_as_unrecovered(hass):
    """If the flow sensor reports a non-numeric value, treat as unrecovered."""
    op = _make_operator(hass, has_flow_meter=True)
    hass.states.get = MagicMock(return_value=MagicMock(state="unavailable"))
    recovered = await op._attempt_leak_recovery()
    assert recovered is False


async def test_escalate_stuck_open_handles_service_exception(hass, caplog):
    """If never_dry.stop raises, _escalate_stuck_open logs and continues."""
    op = _make_operator(hass, has_flow_meter=True)
    hass.services.async_call = AsyncMock(side_effect=RuntimeError("boom"))
    await op._escalate_stuck_open()
    assert "Failed to trigger emergency stop" in caplog.text


async def test_escalate_stuck_open_without_notifier(hass):
    """``_escalate_stuck_open`` is safe when no notifier is configured."""
    op = _make_operator(hass, has_flow_meter=True)
    assert op._notifier is None
    await op._escalate_stuck_open()


async def test_sync_callback_schedules_async_handler(hass):
    """The HA-facing sync callbacks must schedule the async handler."""
    op = _make_operator(hass)
    seen = []

    async def fake_handler(event):
        seen.append(event)

    op._handle_switch_state = fake_handler  # type: ignore[assignment]
    op._on_switch_state(_state_event("on"))
    await asyncio.sleep(0)
    assert seen


async def test_flow_sync_callback_schedules_async_handler(hass):
    """The flow sync callback also routes to the async handler."""
    op = _make_operator(hass, has_flow_meter=True)
    seen = []

    async def fake_handler(event):
        seen.append(event)

    op._handle_flow_state = fake_handler  # type: ignore[assignment]
    op._on_flow_state(_state_event("0.5"))
    await asyncio.sleep(0)
    assert seen


async def test_handle_switch_state_ignores_none_new_state(hass):
    """If the event has no new_state, the handler returns silently."""
    op = _make_operator(hass)
    event = MagicMock()
    event.data = {"new_state": None}
    await op._handle_switch_state(event)
    assert op.state == ValveState.IDLE


async def test_handle_switch_state_obs_available_on_recovery(hass):
    """A switch reporting ``on`` after UNREACHABLE dispatches OBS_AVAILABLE first."""
    op = _make_operator(hass)
    await op._handle_switch_state(_state_event("unavailable"))
    assert op.state == ValveState.UNREACHABLE
    await op._handle_switch_state(_state_event("on"))
    assert op.state == ValveState.IDLE


async def test_handle_flow_state_ignores_none_new_state(hass):
    """If the flow event has no new_state, the handler returns silently."""
    op = _make_operator(hass, has_flow_meter=True)
    event = MagicMock()
    event.data = {"new_state": None}
    await op._handle_flow_state(event)
    assert op.state == ValveState.IDLE


async def test_handle_flow_state_ignores_non_numeric(hass):
    """A non-parseable flow reading is silently ignored."""
    op = _make_operator(hass, has_flow_meter=True)
    await op._handle_flow_state(_state_event("unavailable"))
    assert op.state == ValveState.IDLE


# ── Timing ───────────────────────────────────────────────────────────


async def test_duration_ms_is_populated(hass):
    """``duration_ms`` is set to a positive value on every result."""
    op = _make_operator(hass)

    async def simulate():
        await _yield_loop()
        await op._handle_switch_state(_state_event("on"))

    sim = asyncio.create_task(simulate())
    result = await op.open()
    await sim

    assert result.duration_ms > 0.0
