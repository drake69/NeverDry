# Unit System — Metric-Internal Architecture (SI / Imperial)

How NeverDry supports both metric and US-customary users without ever converting
in the hot path. This section describes **currently
implemented** behaviour (shipped across v0.10.0–v0.10.3).

Companion: `developer_manual.md` (module reference), `scientific-model.md`
(the metric model these units feed).

Note: method/file names below reflect the design as
documented; verify against current `sensor.py` / `controller.py` /
`unit_convert.py` before relying on a specific symbol.

**Authority.** Unlike the model and sensor notes, this
document has no bibliography; its authority is the **source code itself** (the
modules named above), so reproduce/verify by reading the code, not external
references.

---

## Principle: metric core, imperial only at the edges

NeverDry follows a strict **metric-internal** convention: every value stored in
the config entry and every computation in the model uses SI units
(mm, m², L, L/min, °C). This is deliberate — the FAO-56 water balance and the
Hargreaves-Samani ET estimate are defined in metric, so storing in metric keeps
the hot path (executed hundreds of times per day) **conversion-free**. Imperial
support lives entirely **at the edges**: the display layer and the config-flow UI.

```
                 ┌──────────────────────────────────────────┐
  imperial  ───► │  Config-flow UI   (bidirectional convert) │ ──► metric stored
  user input     └──────────────────────────────────────────┘
                 ┌──────────────────────────────────────────┐
  imperial  ◄─── │  Display layer    (HA auto-convert)        │ ◄── metric written
  shown to user  └──────────────────────────────────────────┘
                 ┌──────────────────────────────────────────┐
  imperial  ───► │  Runtime sensor read (normalize on input) │ ──► metric used
  hardware       └──────────────────────────────────────────┘
                          METRIC CORE  (model + storage, no conversion)
```

## 1. Display layer — automatic conversion

Every sensor declares a `device_class` together with its metric
`native_unit_of_measurement`. Home Assistant then auto-converts the displayed
value and unit when the instance is US-customary:

- mm → in
- L → gal
- L/min → gal/min
- m² → ft²
- mm/h → in/h

The integration **writes metric; HA renders imperial.** A regression suite
(`tests/test_sensor_device_classes.py`) asserts the `device_class` of every
sensor, because an accidental removal would *silently* break conversion for
imperial users.

## 2. Config-flow UI — bidirectional conversion

When `hass.config.units` is US-customary, the config flow:

1. renders imperial labels and bounds (°F, in, ft², gal/min);
2. pre-fills fields by converting the stored metric value to the display unit;
3. on submit, converts the input **back to metric before persisting**.

The pure conversion helpers live in a HA-free module (`unit_convert.py`) so they
remain unit-testable in isolation; `config_flow.py` owns only the
`_is_imperial(hass)` predicate and the unit-aware schema builders.

**No config-entry migration is needed:** storage stays metric regardless of the
user's unit system, so toggling the HA unit system never rewrites persisted data.

## 3. Runtime sensor readings — imperial hardware

External sensors (rain gauge, flow meter) may themselves report in imperial when
HA runs US-customary, because HA converts ZHA/Zigbee device units before exposing
them. The controller and `DrynessIndexSensor` therefore read each sensor's
`unit_of_measurement` attribute and normalize before use:

- **Rain** (`_compute_rain_delta`): `in` → mm (×25.4).
- **Flow rate** (`_rate_to_lpm`): `gal/min`, `gal/h` recognized as rate sensors
  and normalized to L/min, alongside metric `L/min`, `L/h`, `m³/h`.
- **Cumulative volume** (`_volume_to_liters`): `gal` → L (×3.785), `m³` → L.

Volume and unit are read in a single `states.get` call (`_read_volume_liters`)
so the value and its unit are always consistent.

> **Why this matters:** failing to normalize here would corrupt the
> delivered-volume integration by a factor of ≈3.8 for gallon-based sensors —
> silently inflating or deflating the deficit reset.

## 4. Intentional exception — alpha stays metric-only

The ET coefficient `α` (alpha) is **intentionally kept metric-only** in the
config form, even for imperial users. It is a composite unit (mm/°C/day) with no
clean imperial equivalent that users would recognize, and exposing a converted
value would create more confusion than clarity. `Kc` is dimensionless and needs
no conversion.

---

## Revision history

| Date | Change |
|---|---|
| 2026-06 | Initial — metric-internal unit architecture. |
