# Controller Reliability Architecture

This document describes the hardening applied to the NeverDry integration
controller layer — the code above the per-valve FSM/operator — and
identifies remaining gaps. It complements `valve-state-machine.md`,
which covers the per-valve FSM and `ValveOperator`.

---

## What is hardened

### 1. Logger level persistence through HA reloads

**Problem.** When HA reloads a config entry, it resets the
`custom_components.never_dry` logger level to `WARNING`, silently
dropping all `INFO` and `DEBUG` lines. The `never_dry_activity.log` file
appeared to stop updating after any reload.

**Fix.** `_setup_file_logger` calls `nd_logger.setLevel(logging.DEBUG)`
explicitly after attaching the `RotatingFileHandler`. `_teardown_file_logger`
restores `setLevel(logging.NOTSET)` so the level is not leaked across
unloads. The startup line now includes the integration version
(`NeverDry 0.9.1 — activity log → ...`) to make restarts visible in
the log.

**Note.** A reload re-executes `async_setup_entry` but does **not**
invalidate the Python module cache (`sys.modules`). Changes to `.py`
files are only picked up after a hard HA restart.

---

### 2. Controller lifecycle — no duplicate instances

**Problem.** On config entry reload, `async_setup_entry` created a new
`IrrigationController` while the previous one was still alive. Both
registered the same HA services and the same valve-state listeners.
Result: double watchdog timers, double `SESSION_RESULT` lines, and
unpredictable irrigation behaviour.

**Fix.** Two-part:

1. `sensor.py` stores the controller reference in
   `hass.data[DOMAIN][f"_controller_{entry.entry_id}"]` immediately
   after creation.
2. `async_unload_entry` pops the reference and calls
   `controller.async_stop()` **before** `async_unload_platforms`. This
   guarantees the old controller is fully terminated before the new one
   is created.

**`async_stop()` design.** Sets `_stop_requested = True`, then does
`with contextlib.suppress(Exception): await task`. It does **not** call
`task.cancel()`. Cancellation raises `CancelledError`, which bypasses
`except Exception` in the irrigation loop's finally block — leaving the
valve open and losing the `SESSION_RESULT`. The graceful path lets the
loop exit its `_wait_with_stop_check` poll within ~1 second, write the
session result, and close the valve cleanly before the new controller
starts.

---

### 3. External valve close detection in `estimated_flow` mode

**Problem.** If the user closed the valve manually during an
`estimated_flow` delivery, the controller kept waiting for the full
estimated duration. Elapsed volume and deficit adjustment were wrong
(over-counted).

**Fix.** `_wait_with_stop_check(duration_s, valve_entity=None)` polls
`hass.states.get(valve_entity).state == "off"` once per second alongside
the `_stop_requested` check. Returns the elapsed seconds so the upstream
caller can compute the correct partial volume. Analogous behaviour already
existed for `flow_meter` mode.

---

### 4. Spurious "Manual irrigation detected" after NeverDry close

**Problem.** Race condition between the `ValveOperator` FSM and the HA
state change event bus. When NeverDry closes a valve, `operator.close()`
transitions the FSM to `IDLE` synchronously. The HA entity state change
event (`valve → off`) is dispatched later, on the next event loop
iteration. `_on_valve_state_change` saw the `off` event without knowing
that NeverDry had just closed the valve, and classified it as a manual
close.

**Fix.** `self._controller_closing: set[str]` tracks valve entity IDs
currently being closed by NeverDry. `_close_valve` adds the entity before
calling `operator.close()` and removes it in `finally`. In
`_on_valve_state_change`, if `entity_id in self._controller_closing`
the close event is silently skipped (NeverDry-initiated, not manual).
`est_duration` was also added to the irrigation start log line.

---

### 5. Config flow guard against spurious reloads

**Problem.** All four `async_update_entry` call sites in `config_flow.py`
(`model_params`, `edit_zone`, `add_zone`, `remove_zone`) called
`async_update_entry` unconditionally — even when the user saved without
changing any field. Every save triggered a reload, which during active
irrigation caused the controller to stop, the valve to close, and the
deficit to reset incorrectly.

**Fix.** Each call site is now guarded with:

```python
if new_data != dict(self._config_entry.data):
    _LOGGER.debug("Config updated via <step> — changed keys: %s", changed)
    self.hass.config_entries.async_update_entry(...)
```

No data change → no update → no reload → irrigation continues
uninterrupted. The debug log records exactly which keys changed, making
future spurious-reload diagnoses straightforward.

---

### 6. CLOSE_LEAK: no spurious CRITICAL on successful recovery

Covered in `valve-state-machine.md §CLOSE_LEAK recovery and
escalation`. Summary: `_notify_failure(CLOSE_LEAK)` is now silent;
CRITICAL only fires via `_escalate_stuck_open()` when recovery fails.

### 7. Per-zone stop + external close in flow modes

**Problem.** Two coupled gaps surfaced on the *Giardino Ortensia* zone
(`flow_meter` / flow-rate mode). (a) When its Sonoff valve auto-closed in
hardware (~600 s) the flow-rate delivery loop did **not** detect it: it kept
polling a dead flow sensor for the full `delivery_timeout` (up to 3600 s),
pinning the zone `is_irrigating` for an hour. The external-close detection
existed only for `estimated_flow` (`_wait_with_stop_check`),
not for the two flow-metered loops. (b) The "Reset valve" button only clears
the FSM `MAINTENANCE` lock — it does not stop a running delivery loop — so the
only escape was the **global** `never_dry.stop`, which the Lovelace card did
not expose. The card could start a session but not stop one.

**Fix.**

- **External-close early-exit** in `_deliver_flow_meter` and
  `_deliver_flow_rate`: a new `_valve_closed_externally(zone)` helper checks
  whether the valve switch reads `off` each poll; on a confirmed `off` the loop
  finalises the partial delivery and exits instead of waiting out the timeout.
  `estimated_flow` and `volume_preset` already did this; the FSM change in
  `valve-state-machine.md §External / hardware auto-close` is required so the
  subsequent `_close_valve` does not raise a spurious `CLOSE_VERIFICATION_FAILED`.
- **Per-zone stop**: new service `never_dry.stop_zone` + `_handle_stop_zone`,
  driven by a `_stop_zone` flag that the delivery loops honour via
  `_should_abort(zone)` (alongside the global `_stop_requested`). It closes the
  single zone's valve, cancels any manual safety watchdog and clears
  `is_irrigating`, leaving other zones untouched. Exposed as a per-zone
  `StopButton` and a **Stop** action in the NeverDry Zone Card.

---

## Layered reliability picture (as of 2026-06-27)

```
Delivery layer (controller.py)
  ├── volume/time target met → _close_valve
  ├── external close detected — estimated_flow (_wait_with_stop_check)
  ├── external close detected — flow_meter / flow_rate loops
  ├── per-zone stop (stop_zone + _stop_zone via _should_abort)
  └── _stop_requested flag exits poll loop within ~1s

Valve layer (valve_operator.py)
  ├── switch confirmation (L2): OPEN_FAILED / CLOSE_VERIFICATION_FAILED
  ├── flow confirmation (L3): ACTUATION_FAILED / CLOSE_LEAK
  ├── CLOSE_LEAK recovery: direct retry + recheck
  ├── Escalation only when recovery fails (CRITICAL)
  └── Software watchdog (absolute max_open_duration)

Hardware layer (Sonoff SWV / Zigbee)
  └── HW max_duration interlock (entity or MQTT direct)

Integration lifecycle (controller + __init__.py)
  ├── Graceful stop on unload (await, not cancel)
  ├── No duplicate controller instances across reloads
  ├── No spurious reloads on unchanged config save
  ├── No spurious manual-close events (_controller_closing)
  └── Logger level persists through reloads
```

---

## Remaining gaps

### HA crash resilience — risk is mode-dependent

This is the most important gap to understand, because the risk is not
uniform across zones — it depends on the delivery mode configured.

| Delivery mode | HA crash behaviour | Risk |
|---|---|---|
| **`volume_preset`** | The valve receives the target volume via `number.set_value` and stores it in hardware firmware. It self-closes when the volume is reached — independently of HA. A crash after `set_value` has been sent does not leave the valve open. | **LOW** |
| **`estimated_flow`** | The valve is a dumb on/off relay. NeverDry sends `turn_on` and waits a calculated duration before sending `turn_off`. If HA crashes between the two, the valve stays open indefinitely. | **HIGH** |
| **`flow_meter`** | Same as `estimated_flow` — HA must send `turn_off`. The flow sensor only controls when NeverDry decides to close; it cannot close the valve by itself. | **HIGH** |

**Consequence for this gap (HA restart recovery).** The implementation
priority depends on which delivery modes are actually in use. If all zones
use `volume_preset`, the HA crash risk is already mitigated at the hardware
level and this fix is a nice-to-have. If any zone uses `estimated_flow` or
`flow_meter`, this fix is HIGH priority.

**Layer 3 partial mitigation.** The hardware interlock sets a
`max_duration` timer on the valve via `number` entity or MQTT. If
configured, this closes `estimated_flow`/`flow_meter` valves even if HA
crashes — acting as a hardware fuse. However, `hw_max_duration_topic` is
not yet exposed in the config flow, so it is not available to end users.

**Recommendation.** For Sonoff SWV (and any valve that accepts a volume
target in its firmware), `volume_preset` mode is the correct choice if
HA crash resilience is a priority. It trades actuation verification (L3)
and CLOSE_LEAK detection for hardware-level close guarantee.

| Gap | Risk |
|---|---|
| **HA restart recovery** (`estimated_flow`/`flow_meter` zones) — valve stays open if HA crashes mid-irrigation. | HIGH for those modes |
| **HW interlock not in config flow** — `hw_max_duration_topic`/`payload` only configurable via YAML, invisible to UI users. | MEDIUM |
| **volume_preset FSM bypass** — no actuation verification (L3), no CLOSE_LEAK detection for smart valves. Acceptable trade-off for volume-target valves; document it per valve type. | LOW (mitigated by hardware metering) |
| **Flow meter hardware fault** (Ortensia) — flow sensor always reads 0. `flow_meter` mode non-functional on that zone. | LOW (zone reconfigured to `estimated_flow`) |
| **CLOSE_VERIFICATION_FAILED recurring** (Melino) — hardware Zigbee issue; valve closes physically but confirmation never arrives. | LOW (valve is closed; notification is spurious) |
| **Config flow: missing field validation** — inconsistent zone config (e.g. `flow_meter` mode with no flow sensor) not caught at setup time. | MEDIUM |
| **Flow meter guard** — no fallback if flow sensor drops to `unavailable` mid-session. | MEDIUM |
| **Latency tracker cold start** — first `MIN_SAMPLES=3` cycles use fixed timeouts; slow Zigbee valves may time out incorrectly at first. | LOW |
| **Test J.5 / J.6 unverified in field** — `_controller_closing` and CRITICAL suppression need HA restart before field verification. | — |

---

## Design invariants to preserve

1. `async_stop()` must use `await task` (not `task.cancel()`). Cancel bypasses the finally block and leaves the valve open.
2. `_notify_failure(CLOSE_LEAK)` must remain silent. CRITICAL for CLOSE_LEAK is only sent by `_escalate_stuck_open()`.
3. `_controller_closing` must be populated before `operator.close()` is called and cleared in `finally`. Do not add await points between `add` and `discard` without re-examining the guard logic.
4. `async_update_entry` in `config_flow.py` must always be guarded by `if new_data != dict(entry.data)`.
5. The controller reference in `hass.data[DOMAIN][f"_controller_{entry.entry_id}"]` must be populated before any valve is opened. `async_unload_entry` depends on it to find and stop the controller.

---

## Revision history

| Date | Change |
|---|---|
| 2026-06 | Initial — controller reliability hardening and invariants. |
