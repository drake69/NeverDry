# Design — Water-Balance Reference Model

**Status:** Draft (RFC)
**Last updated:** 2026-07-23
**Related:** #123, [Domain Object Model](design_domain_object_model.md), backlog AI-189 (bug), AI-174 (per-zone VWC RFC)

## Why this document exists

Discussions about the deficit / Dryness Index kept going in circles because one
question was never written down: **what is a deficit *relative to*?** Once the
reference frame is explicit, the bugs (#123 Rasen 2.1) and the fixes fall out
mechanically. This document fixes the model's reference semantics and the
strategic decisions that follow.

## The quantity

A **deficit** (mm) is the water a patch of soil needs to return to field
capacity. It is a *water-balance state*: it grows with evapotranspiration
demand, shrinks with rain and irrigation.

The single question that governs everything: **relative to which measurement is
a given deficit defined?**

## Reference frames

| Mode | Deficit relative to | Shared across zones? |
|---|---|---|
| **ET** | the system **temperature** sensor (ET input) + the system **rain** sensor | **Yes** — all zones read the same two sensors; they differ only by Kc (an ET multiplier) and irrigation history |
| **VWC, system probe** (today) | one **system moisture** sensor | Yes — all zones scale the same current reading by Kc |
| **VWC, per-zone probe** (target, AI-174) | **that zone's own moisture** sensor | **No** — each zone measures a different patch of soil |

The load-bearing consequence: **two deficits are comparable only if they share a
reference frame.** ET-mode siblings are comparable (shared weather). Two zones
each measuring their own soil probe are **not**.

## Ownership: what lives at the system level vs in the zone

| Element | Level | Notes |
|---|---|---|
| **Temperature** (→ ET) | **System feed** | one sensor for all zones |
| **Rain** (→ `rain_delta`) | **System feed** | one sensor for all zones, applied to every zone's balance |
| **Deficit** (`+ ET·Kc·Δt − rain − irrigation`) | **Zone** | authoritative state |
| **Kc, threshold, area, valve, irrigation state** | **Zone** | — |
| **VWC sensor + `field_capacity` + `root_depth`** | **System (interim)** | until AI-174 → then per-zone |

So the only permanent system-level things are the two environmental **feeds**
(temperature, rain). Everything else lives in the zone. The VWC model
(`vwc_sensor`, `field_capacity`, `root_depth`) is system-level only until the
per-zone VWC work (AI-174) lands, after which nothing but the feeds remains
shared.

## Decisions

### D1 — The deficit is authoritative **per zone**
Each zone owns its deficit (`IrrigationZoneSensor._zone_deficit`). It is:
`D_zone += ET_h · Kc_zone · Δt − rain`, clamped `[0, d_max]`, reset to 0 when
**that** zone is irrigated. This already exists and is correct.

### D2 — The global Dryness Index is **retired as ET state**
`DrynessIndexSensor._deficit` (a parallel `Kc=1.0` accumulator) is **not** an
authoritative state in ET mode. It only ever reset when *all* zones were
irrigated together (`controller.py:721-724`), so under per-zone/manual
irrigation it drifted upward and had no physical correspondence to any zone.

- `DrynessIndexSensor` is **kept** — but only in its real role: the **input hub
  / broadcaster** (owns the temperature and rain sensors, computes `et_h` and
  `rain_delta`, broadcasts `(dt_h, et_h, rain)` to zones). This needs no stored
  deficit.
- The **"Dryness Index" entity**, if retained, becomes a **derived display**
  (e.g. max/mean of zone deficits) — never a source of state. It may also be
  dropped.

### D3 — Rain is a **shared system input**; per-zone displays are projections in liters
One rain sensor -> one `rain_delta` -> applied to every zone's balance. Rain
falls on the whole garden, so the accumulated rain is a **system** quantity:
`DrynessIndexSensor._yearly_rain` [mm], resets on 1 Jan.

The zone display sensors are **projections** of that shared value into liters,
so each zone reports something informative for *itself* rather than an identical
mm repeated on every card:

- **Rain Yearly [L]** (per zone) = `yearly_rain_mm x area_m2` — the liters this
  zone caught from the shared rain. Same mm, different liters by area.
- **Irrigated Yearly [L]** (per zone) = irrigation delivered this year — the
  water you *applied* (a pure consumption figure; device_class WATER feeds the
  HA Energy dashboard, so rain is deliberately excluded). Rain is Rain Yearly;
  a user who wants the grand total sums the two.

The old per-zone `_total_rain` lifetime accumulator is removed: it drifted
between zones by creation time and inflated on intake bugs (a field install read
6418 mm). **First-year caveat:** `_yearly_rain` starts fresh at 0 on the upgrade,
so until the next 1 Jan the rain part covers only "since upgrade" while the
irrigation part is the full restored year — a running total from now on that
self-normalizes at the year boundary.

### D4 — A new zone starts at **zero** (accepted first-cycle bias) — *Decided 2026-07-23*
When a zone is created with no restored state, its deficit starts at **0** — not
inherited from the retired global reference, and not seeded from siblings.

**Rationale.** The only requirement is to avoid the spurious "irrigation due" a
new zone inherited from the inflated global (#123: Rasen 2.1 read 11 mm while its
identical sibling sat at 0). A zero start achieves that with zero machinery. It
carries a small, **explicitly accepted** bias: a fresh zone may under-read the
true deficit until ET accumulates — which is *safe* (under- not over-watering)
and **self-correcting**, because the first irrigation resets the zone to 0 and
the normal per-zone balance takes over. A user can hand-irrigate a brand-new
zone once if they want it primed immediately.

**Rejected alternative — seed from siblings (overkill).** It would need a sibling
registry, a Kc-normalized median, and is only valid *within one reference frame*:
ET siblings are comparable because they share the temperature and rain sensors,
but two zones each on their own VWC probe are not (see Reference frames). A zero
start is frame-agnostic and needs none of this.

**Manual alignment escape hatch.** If a user *wants* every zone to share the
same deficit, they already have the tool: **"Mark irrigated"** per zone resets
that zone's deficit to 0 without opening the valve (`reset_deficit`). Clicking it
on each zone aligns them all to 0 on demand — so no automatic seeding is needed
even for users who care about a uniform starting point.

`total_rain` likewise starts at 0 for a new zone: it is a per-zone lifetime
counter ("rain received since this zone existed"), so 0 at creation is correct
by definition, not a bug (see D3).

### D5 — VWC deficit target is **per-zone**
Today the VWC deficit is computed at system level (`DrynessIndexSensor._deficit`
from one probe, scaled by Kc per zone) — benign because it is a *stateless
measurement* recomputed each reading, and all zones track the same current
value (no drift, no seeding bug). The **target** (AI-174) is per-zone probes:
each zone computes its own deficit from its own sensor, or falls back to the ET
model. When that lands, `DrynessIndexSensor._deficit` disappears entirely and
the hub becomes pure plumbing.

## What is kept vs retired

| Element (class.field) | Verdict |
|---|---|
| `DrynessIndexSensor` as input hub (temp + rain → broadcast) | **Keep** — its real job |
| `DrynessIndexSensor._deficit` as **ET accumulator** | **Retire** — dead weight + the #123 seed bug |
| `DrynessIndexSensor._deficit` as **VWC system measurement** | **Interim** — stateless, benign; removed by AI-174 (per-zone probe) |
| "Dryness Index" display entity | Derived (max/mean of zones) or dropped |
| `IrrigationZoneSensor._zone_deficit` | **Keep — authoritative** |

## Open questions

- **Backfill per-zone.** Today the recorder replay bootstraps one system
  deficit. In the per-zone model each zone should replay with its own Kc — or,
  minimally, the first zone backfills and later zones seed from it (D4).
- **Derived Dryness Index semantics.** If kept for display, is it `max` (the
  driest zone → "does anything need water?") or `mean`? `max` matches the
  at-a-glance "is irrigation due somewhere" intent.
- **Rain per-zone display.** Keep a per-zone rain total (seeded per D4) or show
  one system rain figure? Per-zone only gains meaning once scaled by area
  (AI-102 / AI-179 water-saved).
