# Design — Domain Object Model

**Status:** Draft
**Date:** 2026-07-05
**Related:** GH #74 (actuator abstraction discussion), GH #94 (`valve.*` support), GH #95 (master valve/pump)

## Purpose

Define the conceptual objects of NeverDry's irrigation domain, independent of the current
module layout. This model guides where new features belong (e.g. "does the master valve go
in the scheduler or in the system?") and what should become an explicit first-class object
as the codebase evolves. For the current module/data-flow architecture see
`developer_manual.md` §1.

## The five objects

| Object | Responsibility | Key attributes |
|---|---|---|
| **System** | Global model and shared infrastructure | α (ET sensitivity), D_max, global sensors (temperature, rain), *declares* the master valve/pump |
| **Zone** | Irrigation unit; translates the model into water demand | Kc / plant family, area, sun exposure; cycle & soak rule; translates mm → liters |
| **Scheduler** | The *when* | Time windows, sequences, calendars |
| **ZoneDriver** | The *how* — actuation of one zone's water demand | Entity adapter (`valve.*`/`switch.*`), delivery mode (native volume in liters vs time in seconds via flow rate), flow rate, zero-flow guard |
| **MasterDriver** | Coordination of shared hydraulics (pump / master valve) | ON when any ZoneDriver is active, OFF when none; configurable off-delay; no notion of liters |

ZoneDriver and MasterDriver are two specializations of a common **Driver** base, which owns
what they share: the entity adapter, ON/OFF command with state confirmation, adaptive
latency/timeout, and the safety layers (watchdog, close on error/stop/restart).

## Translation chain

```
System     computes the deficit (mm)            α, D_max, FAO-56 water balance
   │
Zone       translates mm → liters (via area)    applies cycle & soak
   │
ZoneDriver translates liters → actuation        native volume if supported,
   │                                            else seconds via flow rate
Scheduler  decides in which window it happens
```

Liters are the **contract** between Zone and ZoneDriver: the zone always requests liters;
only the driver knows whether to deliver them by volume or by time. This makes the fallback
natural — same request, two actuation strategies.

## Design decisions

### Master valve/pump: declared in System, executed by a Driver

The master valve is not scheduling logic — it takes no decisions. It reacts to the aggregate
execution state (an OR over zone drivers), with an off-delay to avoid pump cycling during
sequential zone runs. It is shared hydraulic infrastructure, like the global sensors, so its
*configuration* lives at system level (as requested in GH #95: "master entity configurable at
integration level").

Its *execution* however is a Driver: modeling it as a Driver specialization means the safety
layers (never leave the pump running on error/stop/restart) are written once in the base and
inherited — instead of duplicating watchdog and error handling inside "system" as a special
case.

### Cycle & soak: a Zone rule

Cycle/soak parameters depend on soil infiltration rate and zone properties (slope, soil
type), so they are per-zone configuration. The *execution* of the cycles is driver/controller
mechanics, but the rule lives in the Zone.

## Mapping to current code (2026-07-05)

| Object | Current state |
|---|---|
| System | ✅ explicit: config entry globals, `ETSensor`, `DrynessIndexSensor` |
| Zone | ✅ explicit: `IrrigationZoneSensor`, per-zone config (Kc, area, sun exposure). Cycle & soak: not implemented |
| Scheduler | ⚠️ implicit and minimal: deficit-triggered daily cycle inside `IrrigationController`; no cron/sequences/calendars (deliberately — that is Irrigation Unlimited's territory) |
| ZoneDriver | ⚠️ exists but internal: `ValveOperator` (FSM, safety layers, latency tracker) + valve/switch adapter (GH #74/#94); native volume delivery in progress |
| MasterDriver | ❌ not implemented (GH #95) |

The refactoring direction is to make the Driver base explicit when implementing GH #95, so
MasterDriver inherits the existing safety layers rather than reimplementing them.
