# NeverDry — User Manual

## Table of contents

1. [Introduction](#1-introduction)
2. [How it works](#2-how-it-works)
3. [Requirements](#3-requirements)
4. [Installation](#4-installation)
5. [Configuration (UI setup wizard)](#5-configuration-ui-setup-wizard)
6. [Understanding the sensors](#6-understanding-the-sensors)
7. [Setting up automations](#7-setting-up-automations)
8. [Monitoring mode (no valves)](#8-monitoring-mode-no-valves)
9. [Editing settings after setup](#9-editing-settings-after-setup)
10. [Calibration guide](#10-calibration-guide)
11. [Dashboard examples](#11-dashboard-examples)
12. [Troubleshooting](#12-troubleshooting)
13. [FAQ](#13-faq)

---

## 1. Introduction

NeverDry is a Home Assistant custom integration that tells you **when** and **how long** to irrigate your garden. Instead of fixed timers, it tracks a real-time soil water deficit — the amount of water your soil has lost through evaporation since the last rain or irrigation.

**What makes it different from a simple timer:**
- Automatically skips irrigation on rainy days
- Irrigates more in hot weather, less in cool weather
- Calculates the exact volume needed per zone
- Works with multiple independent irrigation zones
- Directly controls smart valves (or works in monitoring-only mode)

## 2. How it works

The system continuously tracks a simple water balance:

```
Water lost (evapotranspiration) − Water gained (rain) = Deficit
```

When the deficit is high, your soil is dry and needs water. When it rains, the deficit drops. After irrigation, it resets to zero.

### The key idea

**1 mm of deficit = 1 liter per square meter of water needed.**

So if your deficit is 10 mm and your garden is 45 m², you need 450 liters (adjusted for your irrigation system's efficiency).

### Evapotranspiration (ET)

The integration estimates how much water your soil loses each hour based on temperature:

- **Below 9°C** (configurable): no water loss — plants are dormant
- **Above 9°C**: water loss increases linearly with temperature
- **Hot summer day (35°C)**: approximately 0.24 mm/h → 5.7 mm/day
- **Cool spring day (15°C)**: approximately 0.06 mm/h → 1.3 mm/day

### Two scheduling modes

| Mode | When it triggers | Best for |
|------|-----------------|----------|
| **Mode A** (threshold) | When deficit exceeds a per-zone limit (e.g., 20 mm) | Daytime safety net, drought-tolerant plants |
| **Mode B** (nightly) | Every night at a fixed time (e.g., 23:00) | Primary scheduler, sensitive plants, pots |

You can use one mode, the other, or both together.

## 3. Requirements

### Minimum

- **Home Assistant** 2024.1.0 or newer
- **Temperature sensor** — any outdoor temperature sensor [°C]
- **Rain sensor** — tipping bucket or similar, reporting mm per event

### Recommended

- **Smart valve(s)** — one per irrigation zone (e.g., Shelly, Sonoff, Zigbee valve)
- **Rain gauge** with mm/pulse output (e.g., Ecowitt, Netatmo, DIY tipping bucket)

### Optional (improved accuracy)

- **VWC (volumetric water content) sensor** — bypasses the ET model entirely with direct soil moisture measurement
- T_max / T_min sensors — enables Hargreaves-Samani formula (~1 mm/day accuracy)

## 4. Installation

### Option A: Manual installation

1. Download or clone the repository
2. Copy the `custom_components/never_dry/` folder to your Home Assistant config directory:
   ```
   /config/custom_components/never_dry/
   ```
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration**
5. Search for **NeverDry** and follow the setup wizard (see Section 5)

### Option B: Via HACS

1. In Home Assistant, go to **HACS → Integrations**
2. Click the **⋮** menu → **Custom repositories**
3. Add the repository URL (`https://github.com/drake69/NeverDry`), select category **Integration**
4. Search for **NeverDry** and click **Install**
5. Restart Home Assistant
6. Go to **Settings → Devices & Services → Add Integration**
7. Search for **NeverDry** and follow the setup wizard (see Section 5)

## 5. Configuration (UI setup wizard)

The integration is configured entirely through the Home Assistant UI — no YAML editing required.

### Step 1: Sensors and ET model

When you add the integration, the first screen asks for:

| Field | Required | Description |
|-------|----------|-------------|
| **Temperature sensor** | Yes | Outdoor temperature entity (°C). Only entities with `device_class: temperature` are shown. |
| **Rain sensor** | Yes | Precipitation entity (mm per event) |
| **Alpha (α)** | No | ET coefficient (default: 0.22 mm/°C/day). Higher = more evaporation estimated. |
| **Base temperature (T_base)** | No | Temperature below which ET = 0 (default: 9.0°C) |
| **Max deficit (D_max)** | No | Upper deficit clamp (default: 100.0 mm). Prevents runaway values during sensor outages. |
| **VWC sensor** | No | Optional soil moisture sensor (volumetric water content). If provided, the deficit is calculated directly from soil moisture instead of the ET model. |

### Step 2: Add irrigation zones

For each zone, the wizard asks:

| Field | Required | Description |
|-------|----------|-------------|
| **Zone name** | Yes | Display name (e.g., "Vegetable Garden") |
| **Valve** | No | The `switch` entity that controls this zone's valve. Leave empty for monitoring mode. |
| **Area (m²)** | Yes | Irrigated area in square meters |
| **System type** | Yes | Irrigation method — sets a default efficiency |
| **Efficiency override** | No | Custom efficiency (0.1–1.0). Overrides the system type default. |
| **Plant family** | No | Type of plants in this zone — sets a seasonal crop coefficient (Kc) that adjusts water demand throughout the year. See table below. |
| **Custom Kc** | No | Override Kc (0.1–2.0). If set, overrides the plant family seasonal profile. |
| **Flow rate (L/min)** | Yes | Measured valve flow rate |
| **Threshold (mm)** | No | Deficit threshold for Mode A triggering (default: 20.0 mm) |

**System type defaults:**

| System type | Default efficiency |
|-------------|-------------------|
| Drip irrigation | 0.92 |
| Micro-sprinklers | 0.80 |
| Pop-up sprinklers | 0.68 |
| Manual / hose | 0.55 |

**Plant family Kc profiles** (seasonal, auto-adjusted for hemisphere):

| Family | Winter | Spring | Summer | Autumn |
|--------|--------|--------|--------|--------|
| Lawn / Turf grass | 0.45 | 0.85 | 1.00 | 0.70 |
| Vegetables (seasonal) | 0.30 | 0.70 | 1.10 | 0.50 |
| Fruit trees (deciduous) | 0.35 | 0.70 | 0.95 | 0.55 |
| Ornamental shrubs | 0.40 | 0.65 | 0.80 | 0.55 |
| Herbs (Mediterranean) | 0.30 | 0.55 | 0.70 | 0.40 |
| Citrus / Evergreen fruit | 0.60 | 0.65 | 0.70 | 0.65 |
| Roses | 0.35 | 0.75 | 0.95 | 0.55 |
| Succulents / Cacti | 0.15 | 0.25 | 0.35 | 0.20 |
| Native ground cover | 0.25 | 0.45 | 0.55 | 0.35 |
| Mixed garden (default) | 0.40 | 0.70 | 0.90 | 0.55 |

The Kc values are interpolated linearly between seasons. The hemisphere is auto-detected from your Home Assistant location settings — in the southern hemisphere, the seasonal profile is automatically flipped.

### Step 3: Add more zones or finish

After each zone, you are asked whether to add another zone or complete setup. You can add as many zones as you need.

### Step 4: Verify

After setup, check that these entities exist in **Settings → Devices & Services → Entities**:

- `sensor.et_hourly_estimate` — should show a value in mm/h
- `sensor.never_dry` — should show 0.0 mm (starts fresh)
- `sensor.irrigation_<zone_name>` — one per zone, showing 0.0 L

## 6. Understanding the sensors

### ET Hourly Estimate (`sensor.et_hourly_estimate`)

Shows the current rate of water loss from the soil in mm/h.

- **0.00**: temperature is below base (plants dormant, no water loss)
- **0.05–0.10**: cool day, low water loss
- **0.15–0.25**: hot day, significant water loss
- **> 0.25**: very hot day, high water loss

### NeverDry (`sensor.never_dry`)

The reference sensor. Shows cumulative water deficit in mm at Kc=1.0 (the "raw" deficit before plant-specific adjustment). Each zone tracks its own deficit scaled by its crop coefficient.

- **0 mm**: soil is at field capacity (just rained or irrigated)
- **5–15 mm**: soil is drying but most plants are fine
- **15–25 mm**: time to irrigate sensitive plants and pots
- **25–40 mm**: most plants need water
- **> 40 mm**: severe deficit, risk of plant stress

The deficit is clamped at `D_max` (default 100 mm) to prevent runaway accumulation.

### Irrigation Zone (`sensor.irrigation_<zone_name>`)

Shows the volume of water needed for this specific zone in liters.

**Attributes** (visible in the entity detail):

| Attribute | Meaning |
|-----------|---------|
| `zone_name` | Zone display name |
| `volume_liters` | Water needed [L] |
| `duration_s` | How long to run the valve [seconds] |
| `deficit_mm` | This zone's current deficit [mm] (per-zone, not shared) |
| `plant_family` | Plant family key (e.g., "lawn", "vegetables") |
| `kc` | Current crop coefficient (varies seasonally) |
| `kc_override` | Manual Kc override value, if set |
| `valve` | Associated valve entity |
| `system_type` | Irrigation system type |
| `area_m2` | Zone area |
| `efficiency` | Distribution efficiency |
| `flow_rate_lpm` | Valve flow rate |
| `threshold_mm` | Mode A trigger threshold |
| `irrigating` | `true` if this zone is currently being irrigated |

## 7. Setting up automations

### Mode A: Threshold trigger

This automation starts irrigation when the deficit crosses a threshold. The integration handles valve control — just call the service.

```yaml
automation:
  - alias: "Irrigation threshold — Vegetable Garden"
    trigger:
      - platform: numeric_state
        entity_id: sensor.never_dry
        above: 15
    condition:
      - condition: time
        after: "06:00:00"
        before: "09:00:00"
    action:
      - service: never_dry.irrigate_zone
        data:
          zone_name: "Vegetable Garden"
```

**What happens when you call `irrigate_zone`:**
1. The controller opens the valve (e.g., `switch.valve_vegetables`)
2. Waits for the calculated duration (based on deficit, area, flow rate, efficiency)
3. Closes the valve automatically

**Tips for Mode A:**
- Set the time window to early morning (6:00–9:00) to reduce evaporation
- Each zone can have its own threshold
- If a cycle is already running, the request is ignored (no double-irrigation)

### Mode B: Nightly deficit-based

This automation runs every night and irrigates all zones sequentially.

```yaml
automation:
  - alias: "Nightly irrigation — all zones"
    trigger:
      - platform: time
        at: "23:00:00"
    condition:
      - condition: numeric_state
        entity_id: sensor.never_dry
        above: 1
    action:
      - service: never_dry.irrigate_all
```

**What happens when you call `irrigate_all`:**
1. Irrigates each zone sequentially
2. Each zone: open valve → wait for duration → close valve
3. 30-second pause between zones (configurable via `inter_zone_delay`)
4. After all zones complete, deficit is reset to zero
5. On rainy days, deficit is near 0 → condition skips → nothing happens

### Emergency stop

If you need to stop irrigation immediately:

```yaml
- service: never_dry.stop
```

This closes **all** valves instantly, regardless of which zone is active. Any in-progress irrigation cycle is aborted.

### Combining Mode A + B

Use Mode B as the primary nightly scheduler and Mode A as a daytime safety net for heat waves:

- Mode B runs every night at 23:00 with threshold > 1 mm
- Mode A triggers during the day only if deficit exceeds a high threshold (e.g., 30 mm)

### Services reference

| Service | Parameters | Description |
|---------|-----------|-------------|
| `never_dry.irrigate_zone` | `zone_name` (required) | Irrigate one zone (open valve → wait → close) |
| `never_dry.irrigate_all` | — | Irrigate all zones sequentially, then reset deficit |
| `never_dry.stop` | — | Emergency stop: close all valves immediately |
| `never_dry.reset` | — | Manually reset deficit to zero |

## 8. Monitoring mode (no valves)

If you don't have smart valves, the integration works in **monitoring mode**. This mode activates automatically when no zones have a valve configured.

- Sensors track deficit and calculate volumes as usual
- Every **6 hours**, if the deficit exceeds any zone's threshold, you receive a **persistent notification** in Home Assistant telling you how much water each zone needs
- You can then water manually based on the recommended volumes

This is useful for:
- Getting started before installing smart valves
- Zones with manual watering (hose, watering can)
- Understanding your garden's water needs

The notification looks like:

> **Irrigation needed**
>
> Soil water deficit is **18.5 mm**. Your garden needs watering:
> - **Vegetable Garden**: 411 L (51 min)
> - **Lawn**: 1321 L (88 min)
>
> No irrigation valves are configured — please water manually or configure valves in the integration settings.


## 9. Editing settings after setup

You can modify the integration settings at any time without removing and re-adding it.

Go to **Settings → Devices & Services → NeverDry → Configure**.

The options flow provides two actions:

| Option | What you can change |
|--------|-------------------|
| **Edit model parameters** | Alpha (α), base temperature (T_base), max deficit (D_max) |
| **Add zone** | Add a new irrigation zone with all its parameters |

Changes take effect immediately — no restart required.

## 10. Calibration guide

### Week 1: Start with defaults

Use the default parameters (`alpha: 0.22`, `t_base: 9.0`) and observe.

### Week 2: Adjust alpha

| Observation | Action |
|-------------|--------|
| Plants wilt before irrigation triggers | Increase `alpha` (try 0.28) |
| Soil stays too wet, over-irrigating | Decrease `alpha` (try 0.18) |
| Irrigation seems about right | Keep current value |

You can change alpha via the options flow (see Section 9) without restarting.

### Week 3: Fine-tune threshold

| Plant type | Suggested threshold |
|-----------|-------------------|
| Pots and containers | 10–15 mm |
| Vegetable garden | 15–25 mm |
| Flower beds | 20–30 mm |
| Established lawn | 25–40 mm |
| Drought-tolerant plants | 35–50 mm |

### Using a VWC sensor

If you have a soil moisture sensor that reports volumetric water content (VWC), you can configure it in the setup wizard. When a VWC sensor is provided:
- The ET model is bypassed
- Deficit is calculated directly: `deficit = (field_capacity - VWC) × root_depth × 1000`
- Default field capacity: 0.30 (30%)
- Default root depth: 0.30 m

This gives the most accurate deficit estimate, especially in variable soil conditions.

### Seasonal adjustments

The model automatically adapts to seasons through temperature:
- **Summer**: higher temperatures → higher ET → more frequent irrigation
- **Winter**: lower temperatures → ET near zero → almost no irrigation

No manual seasonal adjustment is needed. If you find the model significantly over- or under-estimates in a specific season, adjust `alpha` by ±0.05 via the options flow.

## 11. Dashboard examples

### Simple status card

```yaml
type: entities
title: Irrigation Status
entities:
  - entity: sensor.never_dry
    name: Soil Water Deficit
  - entity: sensor.et_hourly_estimate
    name: Current ET Rate
  - entity: sensor.irrigation_vegetable_garden
    name: Vegetables — Volume needed
  - entity: sensor.irrigation_lawn
    name: Lawn — Volume needed
```

### History graph

```yaml
type: history-graph
title: Deficit History (7 days)
hours_to_show: 168
entities:
  - entity: sensor.never_dry
    name: Deficit
  - entity: sensor.et_hourly_estimate
    name: ET Rate
```

### Conditional alert card

```yaml
type: conditional
conditions:
  - condition: numeric_state
    entity: sensor.never_dry
    above: 25
card:
  type: markdown
  content: >
    **Soil is getting dry!**
    Deficit: {{ states('sensor.never_dry') }} mm.
    Vegetables need {{ state_attr('sensor.irrigation_vegetable_garden', 'volume_liters') }} L.
```

## 12. Troubleshooting

### Sensors show "unavailable"

- Check that the temperature and rain sensor entity IDs are correct in the integration settings
- Verify the source sensors are online and reporting values
- Check the Home Assistant logs: **Settings → System → Logs** → filter for `never_dry`

### Deficit never increases

- Is your temperature sensor reporting values above `t_base` (default 9°C)?
- Check `sensor.et_hourly_estimate` — if it shows 0.0, ET is not being calculated
- Verify the temperature sensor entity ID is correct in the integration config

### Deficit never decreases

- Is your rain sensor reporting values? Check its entity in HA
- The rain sensor should report mm per event (tipping bucket pulse), not cumulative daily total
- If using a cumulative rain sensor, you'll need a template sensor to extract per-event increments

### Deficit grows unexpectedly large

- Check the `D_max` setting — this clamps the maximum deficit (default: 100 mm)
- If the rain sensor is offline, deficit will only accumulate. Fix the sensor and call `never_dry.reset` if needed.

### Volume shows 0 L

- Check that `area_m2` and `flow_rate_lpm` are configured for the zone
- Verify `efficiency` is not set to 0

### Irrigation runs too long / too short

- Measure your actual flow rate: run the valve for 1 minute into a bucket
- Check that `area_m2` is accurate
- Adjust `efficiency` via the options flow based on your irrigation type

### State resets after HA restart

- The integration uses `RestoreEntity` — state should survive restarts
- If state is lost, check that the entity's `unique_id` hasn't changed
- Check HA logs for restore errors

## 13. FAQ

**Q: Does it work without a rain sensor?**
A: Technically yes, but the deficit will only increase (never decrease from rain). You would need to manually call `never_dry.reset` after significant rain. A rain sensor is strongly recommended.

**Q: Can I use a weather integration instead of physical sensors?**
A: Yes. You can use any HA entity that provides temperature in °C and precipitation in mm. Weather integrations (OpenWeatherMap, Met.no, etc.) work, but physical sensors are more accurate for your specific location.

**Q: What happens during a power outage?**
A: The deficit state is persisted and restored when HA restarts. The time gap during the outage means some ET was not tracked, resulting in a slight underestimate. This is generally acceptable.

**Q: Can different zones have different deficit thresholds?**
A: Yes. Each zone has its own `threshold` parameter for Mode A. The underlying deficit is shared (same soil, same weather), but each zone can trigger at a different level.

**Q: How do I handle zones with different sun exposure?**
A: The current model uses a single deficit for all zones. For significantly different microclimates (e.g., full sun vs. deep shade), consider running two separate instances of the integration with different `alpha` values.

**Q: Can I add or remove zones after initial setup?**
A: You can add new zones via **Settings → Devices & Services → NeverDry → Configure → Add zone**. Removing zones currently requires removing and re-adding the integration.

**Q: What does D_max do?**
A: D_max (default 100 mm) is the maximum value the deficit can reach. It prevents the deficit from growing indefinitely during extended dry periods or sensor outages. In practice, values above 100 mm rarely occur in residential settings.

**Q: What is the VWC sensor option?**
A: If you have a soil moisture sensor that reports volumetric water content (as a fraction, e.g., 0.25 for 25%), the integration can calculate the deficit directly from that measurement instead of using the temperature-based ET model. This is more accurate but requires a suitable sensor.

**Q: How accurate is the ET estimate?**
A: With temperature only, the model explains 40–60% of the real ET variance in temperate climates. It's sufficient for residential use but not for professional agriculture. Adding T_max/T_min sensors (Hargreaves-Samani) improves accuracy to ~1 mm/day.

---

## Disclaimer

NeverDry is a **hobby project for residential use**. It is not certified for agricultural, commercial, or safety-critical applications. The authors accept no liability for crop damage, water waste, property damage, or any other loss resulting from the use of this software.

The ET model is a simplification of the FAO-56 standard and is **not a substitute for professional agronomic advice**. Crop coefficients (Kc) are approximate seasonal averages — actual water needs depend on soil type, microclimate, plant health, and many other factors.

**Always monitor your irrigation system** and verify that valves open and close correctly.

## Acknowledgments

This project was developed with the assistance of [Claude](https://claude.ai) by [Anthropic](https://anthropic.com).

## Scientific References

- Allen, R.G., Pereira, L.S., Raes, D., Smith, M. (1998). *Crop evapotranspiration.* [FAO Irrigation and Drainage Paper 56](https://www.fao.org/4/x0490e/x0490e00.htm).
- Hargreaves, G.H., Samani, Z.A. (1985). Reference crop evapotranspiration from temperature. *Applied Engineering in Agriculture*, 1(2), 96–99.
- Penman, H.L. (1948). Natural evaporation from open water, bare soil and grass. *Proc. R. Soc. London A*, 193, 120–145.
- Monteith, J.L. (1965). Evaporation and environment. *Symp. Soc. Exp. Biol.*, 19, 205–234.
- Fereres, E., Soriano, M.A. (2007). Deficit irrigation for reducing agricultural water use. *J. Exp. Bot.*, 58(2), 147–159.
