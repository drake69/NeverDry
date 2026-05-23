"""HA-aware wrapper around :class:`ValveFsm`.

One :class:`ValveOperator` instance per configured valve. It owns the
FSM, subscribes to Home Assistant state changes for the switch and the
optional flow sensor, executes the FSM's actions (switch services,
asyncio timers, failure notifications) and exposes a clean async API
returning a typed :class:`OperationResult`.

Retry policy: transient failures (``OPEN_FAILED``,
``CLOSE_VERIFICATION_FAILED``) are retried with exponential backoff up
to ``max_retries``. Physical failures (``ACTUATION_FAILED``,
``CLOSE_LEAK``) are surfaced immediately because retrying the same
software command cannot unblock a hydraulic or mechanical issue and, in
the leak case, would waste water.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from time import monotonic
from typing import ClassVar

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .valve_fsm import (
    CancelAllTimers,
    CancelTimer,
    EnterMaintenance,
    FailureKind,
    FsmAction,
    FsmConfig,
    NotifyFailure,
    SendSwitchOff,
    SendSwitchOn,
    StartTimer,
    TimerName,
    TransitionResult,
    ValveEvent,
    ValveFsm,
    ValveState,
)
from .valve_notifier import NotificationKind, Severity, ValveNotifier

_LOGGER = logging.getLogger(__name__)


# ── Public result types ───────────────────────────────────────────────


class OperationStatus(StrEnum):
    """Outcome category for :meth:`ValveOperator.open` / :meth:`close`."""

    OK = "ok"
    FAILED = "failed"
    MAINTENANCE = "maintenance"
    PRECHECK_FAILED = "precheck_failed"


@dataclass(frozen=True)
class OperationResult:
    """Typed return value of an open/close cycle.

    ``error_detail`` carries the :class:`FailureKind` name on FAILED, the
    precheck reason on PRECHECK_FAILED, or ``"in_maintenance"`` when the
    operator is locked.
    """

    status: OperationStatus
    error_detail: str | None = None
    retries_used: int = 0
    duration_ms: float = 0.0


# ── Mappings ──────────────────────────────────────────────────────────


_TIMEOUT_EVENT_FOR_TIMER: dict[TimerName, ValveEvent] = {
    TimerName.OPEN: ValveEvent.TIMEOUT_OPEN,
    TimerName.CLOSE: ValveEvent.TIMEOUT_CLOSE,
    TimerName.FLOW: ValveEvent.TIMEOUT_FLOW,
    TimerName.LEAK: ValveEvent.TIMEOUT_LEAK,
}

_TRANSIENT_FAILURES: frozenset[FailureKind] = frozenset(
    {FailureKind.OPEN_FAILED, FailureKind.CLOSE_VERIFICATION_FAILED}
)

_OPERATION_FOR_FAILURE: dict[FailureKind, str] = {
    FailureKind.OPEN_FAILED: "open",
    FailureKind.ACTUATION_FAILED: "open",
    FailureKind.CLOSE_VERIFICATION_FAILED: "close",
    FailureKind.CLOSE_LEAK: "close",
}


# ── The operator ──────────────────────────────────────────────────────


class ValveOperator:
    """HA-aware driver for a single valve, sitting on top of a :class:`ValveFsm`."""

    DEFAULT_BACKOFF_S: ClassVar[tuple[float, ...]] = (1.0, 2.0, 4.0)

    def __init__(
        self,
        hass: HomeAssistant,
        switch_entity_id: str,
        flow_sensor_entity_id: str | None = None,
        zone_name: str = "",
        fsm_config: FsmConfig | None = None,
        max_retries: int = 2,
        backoff_s: tuple[float, ...] | None = None,
        flow_zero_threshold: float = 0.05,
        notifier: ValveNotifier | None = None,
    ) -> None:
        """Wire the operator to HA, subscribe to state changes and build the FSM."""
        self._hass = hass
        self._switch_entity_id = switch_entity_id
        self._flow_sensor_entity_id = flow_sensor_entity_id
        self._zone_name = zone_name or switch_entity_id
        self._fsm_config = fsm_config or FsmConfig(has_flow_meter=flow_sensor_entity_id is not None)
        self._fsm = ValveFsm(self._fsm_config)
        self._max_retries = max_retries
        self._backoff_s = backoff_s if backoff_s is not None else self.DEFAULT_BACKOFF_S
        self._flow_zero_threshold = flow_zero_threshold
        self._notifier = notifier

        self._lock = asyncio.Lock()
        self._timers: dict[TimerName, asyncio.Task] = {}
        self._completion: asyncio.Future[OperationResult] | None = None
        self._expected_terminal: tuple[ValveState, ...] = ()
        # AI-032: tracks whether we have already attempted the last-resort
        # leak recovery for the current close() call. Reset at the start of
        # every close() invocation.
        self._leak_recovery_attempted: bool = False

        self._unsub_switch = async_track_state_change_event(hass, [switch_entity_id], self._on_switch_state)
        self._unsub_flow = None
        if flow_sensor_entity_id:
            self._unsub_flow = async_track_state_change_event(hass, [flow_sensor_entity_id], self._on_flow_state)

    # ── Properties ───────────────────────────────────────────────────

    @property
    def state(self) -> ValveState:
        """Return the underlying FSM state."""
        return self._fsm.state

    @property
    def is_in_maintenance(self) -> bool:
        """``True`` when the operator is locked in MAINTENANCE."""
        return self._fsm.state == ValveState.MAINTENANCE

    @property
    def failure_count(self) -> int:
        """Return the FSM's consecutive-failure counter."""
        return self._fsm.failure_count

    # ── Public API ───────────────────────────────────────────────────

    async def open(self) -> OperationResult:
        """Open the valve, awaiting verification.

        Returns once the FSM reaches ``OPEN_VERIFIED`` (with flow meter)
        or ``OPEN`` (without). Retries transient failures with exponential
        backoff up to ``max_retries``; physical failures return
        immediately.
        """
        terminals: tuple[ValveState, ...] = (
            (ValveState.OPEN_VERIFIED,) if self._fsm_config.has_flow_meter else (ValveState.OPEN,)
        )
        return await self._run_command(cmd=ValveEvent.CMD_OPEN, terminals=terminals)

    async def close(self) -> OperationResult:
        """Close the valve, awaiting verification.

        Returns once the FSM is back in ``IDLE`` after a clean close. On
        ``CLOSE_LEAK`` (switch off but flow still positive) AI-032's
        last-resort recovery runs **before** declaring the close failed:

        1. Re-issue ``switch.turn_off`` directly (bypassing the FSM, in
           case the first command was lost on the wire).
        2. Wait ``leak_timeout_s`` for the flow to drop.
        3. If the flow has dropped → report ``OK`` with
           ``error_detail="leak_recovered"``.
        4. If the flow is still positive → call the integration-wide
           emergency stop service to close every other valve
           preventively, emit a CRITICAL ``STUCK_OPEN`` notification
           and return the original ``FAILED`` result.

        Recovery is attempted at most **once** per ``close()`` call.
        """
        self._leak_recovery_attempted = False
        result = await self._run_command(
            cmd=ValveEvent.CMD_CLOSE,
            terminals=(ValveState.IDLE,),
        )
        if (
            result.status == OperationStatus.FAILED
            and result.error_detail == FailureKind.CLOSE_LEAK.value
            and not self._leak_recovery_attempted
        ):
            self._leak_recovery_attempted = True
            if await self._attempt_leak_recovery():
                return OperationResult(
                    status=OperationStatus.OK,
                    error_detail="leak_recovered",
                    retries_used=result.retries_used,
                    duration_ms=result.duration_ms,
                )
            await self._escalate_stuck_open()
        return result

    async def reset_maintenance(self) -> None:
        """Clear ``MAINTENANCE`` and reset the failure counter."""
        await self._dispatch(ValveEvent.CMD_RESET)

    def async_unload(self) -> None:
        """Detach state listeners and cancel every outstanding timer."""
        if self._unsub_switch:
            self._unsub_switch()
        if self._unsub_flow:
            self._unsub_flow()
        self._cancel_all_timers()

    # ── Command driver ───────────────────────────────────────────────

    async def _run_command(
        self,
        cmd: ValveEvent,
        terminals: tuple[ValveState, ...],
    ) -> OperationResult:
        """Run an open or close cycle with retry on transient failures."""
        start = monotonic()

        precheck = self._precheck()
        if precheck is not None:
            return OperationResult(
                status=precheck[0],
                error_detail=precheck[1],
                retries_used=0,
                duration_ms=self._elapsed_ms(start),
            )

        async with self._lock:
            retries = 0
            while True:
                outcome = await self._run_cycle(cmd, terminals)
                if outcome.status == OperationStatus.OK:
                    return self._finalise(outcome, retries, start)
                if not self._is_retryable(outcome) or retries >= self._max_retries:
                    return self._finalise(outcome, retries, start)
                retries += 1
                await asyncio.sleep(self._backoff_for(retries - 1))

    def _precheck(self) -> tuple[OperationStatus, str] | None:
        """Return a failure tuple if HA state forbids dispatching, else ``None``."""
        if self._fsm.state == ValveState.MAINTENANCE:
            return (OperationStatus.MAINTENANCE, "in_maintenance")
        state = self._hass.states.get(self._switch_entity_id)
        if state is None:
            return (OperationStatus.PRECHECK_FAILED, "switch_entity_not_found")
        if state.state in ("unavailable", "unknown"):
            return (OperationStatus.PRECHECK_FAILED, "switch_unavailable")
        return None

    async def _run_cycle(
        self,
        cmd: ValveEvent,
        terminals: tuple[ValveState, ...],
    ) -> OperationResult:
        """Dispatch ``cmd`` and await the resulting cycle completion."""
        loop = asyncio.get_event_loop()
        self._completion = loop.create_future()
        self._expected_terminal = terminals
        await self._dispatch(cmd)
        return await self._completion

    def _is_retryable(self, outcome: OperationResult) -> bool:
        """``True`` if ``outcome`` describes a transient (comms) failure."""
        if outcome.status != OperationStatus.FAILED or outcome.error_detail is None:
            return False
        try:
            kind = FailureKind(outcome.error_detail)
        except ValueError:
            return False
        return kind in _TRANSIENT_FAILURES

    def _backoff_for(self, retry_index: int) -> float:
        """Return the sleep duration before retry number ``retry_index``."""
        if not self._backoff_s:
            return 0.0
        idx = min(retry_index, len(self._backoff_s) - 1)
        return self._backoff_s[idx]

    def _finalise(
        self,
        outcome: OperationResult,
        retries: int,
        start: float,
    ) -> OperationResult:
        """Stamp timing and retry count on an :class:`OperationResult`."""
        return OperationResult(
            status=outcome.status,
            error_detail=outcome.error_detail,
            retries_used=retries,
            duration_ms=self._elapsed_ms(start),
        )

    @staticmethod
    def _elapsed_ms(start: float) -> float:
        """Return milliseconds elapsed since ``start`` (``monotonic()`` based)."""
        return (monotonic() - start) * 1000.0

    # ── FSM bridging ─────────────────────────────────────────────────

    async def _dispatch(self, event: ValveEvent) -> None:
        """Push ``event`` into the FSM, execute its actions, settle the future."""
        result = self._fsm.dispatch(event)
        await self._execute_actions(result.actions)
        self._check_terminal(result)

    async def _execute_actions(self, actions: tuple[FsmAction, ...]) -> None:
        """Run every action returned by the FSM, in order."""
        for action in actions:
            await self._execute_action(action)

    async def _execute_action(self, action: FsmAction) -> None:
        """Execute a single FSM action against Home Assistant."""
        if isinstance(action, SendSwitchOn):
            await self._call_switch("turn_on")
        elif isinstance(action, SendSwitchOff):
            await self._call_switch("turn_off")
        elif isinstance(action, StartTimer):
            self._start_timer(action.name, action.seconds)
        elif isinstance(action, CancelTimer):
            self._cancel_timer(action.name)
        elif isinstance(action, CancelAllTimers):
            self._cancel_all_timers()
        elif isinstance(action, NotifyFailure):
            await self._notify_failure(action.kind)
        elif isinstance(action, EnterMaintenance):
            await self._notify_maintenance()

    def _check_terminal(self, result: TransitionResult) -> None:
        """If ``result`` terminates the current cycle, resolve the awaiting future."""
        if self._completion is None or self._completion.done():
            return
        if result.to_state == ValveState.MAINTENANCE:
            detail = result.failure.value if result.failure else "in_maintenance"
            self._completion.set_result(OperationResult(OperationStatus.MAINTENANCE, detail))
            return
        if result.failure is not None:
            self._completion.set_result(OperationResult(OperationStatus.FAILED, result.failure.value))
            return
        if result.to_state in self._expected_terminal:
            self._completion.set_result(OperationResult(OperationStatus.OK))

    # ── Notifier bridging ────────────────────────────────────────────

    async def _notify_failure(self, kind: FailureKind) -> None:
        """Surface a FailureKind via the notifier (or log if none configured)."""
        _LOGGER.error("Valve '%s' failure: %s", self._zone_name, kind.name)
        if self._notifier is None:
            return
        severity = (
            Severity.CRITICAL
            if kind in (FailureKind.CLOSE_LEAK, FailureKind.CLOSE_VERIFICATION_FAILED)
            else Severity.WARNING
        )
        if kind == FailureKind.CLOSE_LEAK:
            await self._notifier.notify(
                self._zone_name,
                NotificationKind.STUCK_OPEN,
                severity,
                context={"flow": "still positive after close command"},
            )
        else:
            await self._notifier.notify(
                self._zone_name,
                NotificationKind.COMMAND_FAILED,
                severity,
                context={
                    "operation": _OPERATION_FOR_FAILURE[kind],
                    "error_detail": kind.value,
                },
            )

    async def _attempt_leak_recovery(self) -> bool:
        """Last-resort recovery from ``CLOSE_LEAK``: re-issue turn_off, re-check.

        Sends a direct ``switch.turn_off`` (no FSM cycle, since the FSM
        is already back in ``IDLE``) and waits the leak timeout for the
        flow sensor to drop below threshold. Returns ``True`` when the
        recovery succeeded, ``False`` otherwise.
        """
        _LOGGER.warning(
            "Valve '%s' CLOSE_LEAK detected — attempting recovery (direct switch.turn_off + recheck)",
            self._zone_name,
        )
        await self._call_switch("turn_off")
        await asyncio.sleep(self._fsm_config.leak_timeout_s)

        if self._flow_sensor_entity_id is None:
            # No flow meter to verify; conservatively treat as unrecovered.
            return False
        state = self._hass.states.get(self._flow_sensor_entity_id)
        if state is None:
            return False
        try:
            flow = float(state.state)
        except (ValueError, TypeError):
            return False
        recovered = flow <= self._flow_zero_threshold
        if recovered:
            _LOGGER.info(
                "Valve '%s' leak recovery succeeded (flow=%.3f)",
                self._zone_name,
                flow,
            )
        else:
            _LOGGER.error(
                "Valve '%s' leak recovery failed (flow=%.3f)",
                self._zone_name,
                flow,
            )
        return recovered

    async def _escalate_stuck_open(self) -> None:
        """Trigger integration-wide emergency stop + CRITICAL notification."""
        _LOGGER.error(
            "Valve '%s' stuck-open confirmed after recovery; calling never_dry.stop and escalating notification",
            self._zone_name,
        )
        try:
            await self._hass.services.async_call("never_dry", "stop", {}, blocking=False)
        except Exception as exc:
            _LOGGER.error(
                "Failed to trigger emergency stop for stuck-open valve '%s': %s",
                self._zone_name,
                exc,
            )
        if self._notifier is None:
            return
        await self._notifier.notify(
            self._zone_name,
            NotificationKind.STUCK_OPEN,
            Severity.CRITICAL,
            context={"flow": "still positive after recovery attempt"},
        )

    async def _notify_maintenance(self) -> None:
        """Notify that the operator just entered MAINTENANCE."""
        _LOGGER.error(
            "Valve '%s' entered MAINTENANCE (consecutive failures = %d)",
            self._zone_name,
            self._fsm.failure_count,
        )
        if self._notifier is None:
            return
        await self._notifier.notify(
            self._zone_name,
            NotificationKind.ZONE_DISABLED,
            Severity.CRITICAL,
            context={"failures": self._fsm.failure_count},
        )

    # ── HA service helper ────────────────────────────────────────────

    async def _call_switch(self, service: str) -> None:
        """Invoke ``switch.<service>`` on the configured entity, logging errors."""
        try:
            await self._hass.services.async_call(
                "switch",
                service,
                {"entity_id": self._switch_entity_id},
                blocking=False,
            )
        except Exception as exc:
            _LOGGER.error(
                "Valve '%s' switch.%s call raised: %s",
                self._zone_name,
                service,
                exc,
            )

    # ── Timer plumbing ───────────────────────────────────────────────

    def _start_timer(self, name: TimerName, seconds: float) -> None:
        """Start (or restart) a named timer that dispatches the matching TIMEOUT."""
        self._cancel_timer(name)
        self._timers[name] = self._hass.async_create_task(self._timer(name, seconds))

    def _cancel_timer(self, name: TimerName) -> None:
        """Cancel ``name`` if it is running; safe to call when absent."""
        task = self._timers.pop(name, None)
        if task and not task.done():
            task.cancel()

    def _cancel_all_timers(self) -> None:
        """Cancel every active timer for this valve."""
        for name in list(self._timers):
            self._cancel_timer(name)

    async def _timer(self, name: TimerName, seconds: float) -> None:
        """Background task: sleep, then dispatch the matching ``TIMEOUT_*`` event."""
        try:
            await asyncio.sleep(seconds)
            await self._dispatch(_TIMEOUT_EVENT_FOR_TIMER[name])
        except asyncio.CancelledError:
            pass

    # ── HA state listeners ───────────────────────────────────────────

    @callback
    def _on_switch_state(self, event) -> None:
        """HA callback: schedule the async switch-state handler."""
        self._hass.async_create_task(self._handle_switch_state(event))

    @callback
    def _on_flow_state(self, event) -> None:
        """HA callback: schedule the async flow-state handler."""
        self._hass.async_create_task(self._handle_flow_state(event))

    async def _handle_switch_state(self, event) -> None:
        """Map a switch state change to the matching FSM observation."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        value = new_state.state
        if value in ("unavailable", "unknown"):
            await self._dispatch(ValveEvent.OBS_UNAVAILABLE)
            return
        if self._fsm.state == ValveState.UNREACHABLE:
            await self._dispatch(ValveEvent.OBS_AVAILABLE)
        if value == "on":
            await self._dispatch(ValveEvent.OBS_SWITCH_ON)
        elif value == "off":
            await self._dispatch(ValveEvent.OBS_SWITCH_OFF)

    async def _handle_flow_state(self, event) -> None:
        """Map a flow-sensor state change to OBS_FLOW_POSITIVE/OBS_FLOW_ZERO."""
        new_state = event.data.get("new_state")
        if new_state is None:
            return
        try:
            flow = float(new_state.state)
        except (ValueError, TypeError):
            return
        if flow > self._flow_zero_threshold:
            await self._dispatch(ValveEvent.OBS_FLOW_POSITIVE)
        else:
            await self._dispatch(ValveEvent.OBS_FLOW_ZERO)
