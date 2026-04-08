# NeverDry — Developer Manual

## Table of contents

1. [Architecture overview](#1-architecture-overview)
2. [Core formulas and their location](#2-core-formulas-and-their-location)
3. [Crop coefficient (Kc) system](#3-crop-coefficient-kc-system)
4. [Module reference](#4-module-reference)
5. [Service registration](#5-service-registration)
6. [Config flow](#6-config-flow)
7. [Testing](#7-testing)
8. [Adding a new ET tier](#8-adding-a-new-et-tier)

---

## 1. Architecture overview

```
custom_components/never_dry/
├── __init__.py        → Integration setup (YAML + config entry)
├── const.py           → All constants, defaults, system types, plant families
├── sensor.py          → compute_kc(), ETSensor, DrynessIndexSensor, IrrigationZoneSensor
├── controller.py      → IrrigationController (valve control, monitoring mode)
├── config_flow.py     → UI setup wizard + options flow
├── services.yaml      → HA service definitions
├── strings.json       → UI strings
└── translations/
    └── en.json        → English translations
```

**Data flow:**

```
Temperature sensor ──→ ETSensor (ET_h)
                            │
                            ▼
Rain sensor ─────────→ DrynessIndexSensor (reference deficit, Kc=1.0)
VWC sensor (optional) ─┘    │
                             │ broadcasts (dt_h, et_h, rain) via listener pattern
                             ▼
                      IrrigationZoneSensor × N
                      Each zone tracks its own deficit:
                        D_zone += ET_h × Kc(doy, family) × Δt - rain
                             │
                             ▼
                      IrrigationController (valve open/close, services)
```

`DrynessIndexSensor` is the "reference" sensor at Kc=1.0. Each zone sensor registers as a listener and maintains its own deficit scaled by a crop coefficient Kc. The Kc varies seasonally based on the plant family assigned to the zone, with automatic hemisphere detection from `hass.config.latitude`.

## 2. Core formulas and their location

All formulas live in `sensor.py`.

### 2.1 Hourly evapotranspiration (linear model)

```
ET_h = max(0, α · (T - T_base) / 24)   [mm/h]
```

| Item | Value |
|------|-------|
| **Class** | `ETSensor` |
| **Method** | `_on_temp_change()` |
| **Parameters** | `alpha` (default 0.22 mm/°C/day), `t_base` (default 9.0°C) |
| **Trigger** | `async_track_state_change_event` on temperature sensor |

### 2.2 Reference deficit accumulation (ET model, Kc=1.0)

```
D_ref(t) = clamp( D_ref(t-1) + ET_h · Δt - rain,  0,  D_max )
```

| Item | Value |
|------|-------|
| **Class** | `DrynessIndexSensor` |
| **Method** | `_update_from_model()` |
| **Integration** | Forward Euler, variable Δt (event-driven) |
| **Parameters** | `alpha`, `t_base`, `d_max` (default 100.0 mm) |

### 2.3 Per-zone deficit accumulation (with Kc)

```
D_zone(t) = clamp( D_zone(t-1) + ET_h · Kc(doy, family) · Δt - rain,  0,  D_max )
```

| Item | Value |
|------|-------|
| **Class** | `IrrigationZoneSensor` |
| **Method** | `_on_et_update()` |
| **Kc source** | `compute_kc()` module-level function |
| **Parameters** | `plant_family`, `kc` (manual override), `hass.config.latitude` |

Each zone accumulates independently. Rain reduces all zone deficits equally. Only the irrigated zone's deficit resets after irrigation.

### 2.4 Crop coefficient computation

```
Kc = compute_kc(day_of_year, plant_family, manual_kc, latitude)
```

| Item | Value |
|------|-------|
| **Function** | `compute_kc()` (module-level in `sensor.py`) |
| **Priority** | `manual_kc > plant_family seasonal profile > DEFAULT_KC (1.0)` |
| **Interpolation** | Linear between 4 seasonal anchors (days 15, 105, 196, 288) |
| **Hemisphere** | Southern (latitude < 0): day shifted by 182 days |
| **Plant families** | Defined in `const.py` `PLANT_FAMILIES` dict (10 families) |

### 2.5 Deficit from VWC (direct measurement)

```
D = max(0, (FC - VWC) · root_depth · 1000)   [mm]
```

| Item | Value |
|------|-------|
| **Class** | `DrynessIndexSensor` |
| **Method** | `_update_from_vwc()` |
| **Zone behavior** | In VWC mode, zones compute `D_zone = D_ref × Kc` |

### 2.6 Irrigation volume per zone

```
V = D_zone · A / η   [L]
```

| Item | Value |
|------|-------|
| **Class** | `IrrigationZoneSensor` |
| **Property** | `volume_liters` |
| **Uses** | `_zone_deficit` (per-zone, not shared) |

### 2.7 Irrigation duration per zone

```
t = V / Q · 60   [s]
```

| Item | Value |
|------|-------|
| **Class** | `IrrigationZoneSensor` |
| **Property** | `duration_s` |
| **Parameters** | `flow_rate_lpm` (Q) |

### 2.8 Resolution orders

**Efficiency**: `explicit value > system_type default > global default (0.85)`

**Kc**: `manual kc > plant_family seasonal Kc(doy) > DEFAULT_KC (1.0)`

## 3. Crop coefficient (Kc) system

### Plant families (defined in `const.py`)

| Family key | Label | Kc winter | Kc spring | Kc summer | Kc autumn |
|-----------|-------|-----------|-----------|-----------|-----------|
| `lawn` | Lawn / Turf grass | 0.45 | 0.85 | 1.00 | 0.70 |
| `vegetables` | Vegetables (seasonal) | 0.30 | 0.70 | 1.10 | 0.50 |
| `fruit_trees` | Fruit trees (deciduous) | 0.35 | 0.70 | 0.95 | 0.55 |
| `ornamental_shrubs` | Ornamental shrubs | 0.40 | 0.65 | 0.80 | 0.55 |
| `herbs` | Herbs (Mediterranean) | 0.30 | 0.55 | 0.70 | 0.40 |
| `citrus` | Citrus / Evergreen fruit | 0.60 | 0.65 | 0.70 | 0.65 |
| `roses` | Roses | 0.35 | 0.75 | 0.95 | 0.55 |
| `succulents` | Succulents / Cacti | 0.15 | 0.25 | 0.35 | 0.20 |
| `native_ground_cover` | Native ground cover | 0.25 | 0.45 | 0.55 | 0.35 |
| `mixed_garden` | Mixed garden (default) | 0.40 | 0.70 | 0.90 | 0.55 |

Seasonal anchors (northern hemisphere): day 15 (mid-Jan), 105 (mid-Apr), 196 (mid-Jul), 288 (mid-Oct).

### Listener pattern

`DrynessIndexSensor` maintains a `_zone_listeners` list. Each `IrrigationZoneSensor` registers via `register_zone_listener()` at construction. When the base sensor updates, it broadcasts `(dt_h, et_h, rain)` to all listeners.

### Per-zone reset logic

- `irrigate_zone`: resets only the irrigated zone's deficit
- `irrigate_all`: resets all zone deficits + reference deficit
- `reset` service: resets everything

## 4. Module reference

### const.py

All configuration keys (`CONF_*`), service names (`SERVICE_*`), system types, plant families, anchor days, and default values. Single source of truth for magic strings.

### sensor.py

| Element | Type | Purpose |
|---------|------|---------|
| `compute_kc()` | Function | Pure function: Kc from day, family, override, latitude |
| `ETSensor` | Class (1 instance) | Instantaneous ET rate [mm/h] |
| `DrynessIndexSensor` | Class (1 instance) | Reference deficit [mm] at Kc=1.0, RestoreEntity |
| `IrrigationZoneSensor` | Class (N instances) | Per-zone deficit, volume [L], duration [s], RestoreEntity |

### controller.py

`IrrigationController` holds references to the `DrynessIndexSensor` and all `IrrigationZoneSensor` instances.

**Key behaviors:**
- Sequential valve control with configurable inter-zone delay (default 30s)
- Per-zone deficit reset after irrigation (not global)
- Stop-check every 1 second during irrigation
- Monitoring mode: 6-hour periodic check with per-zone deficit thresholds
- Error safety: all valves closed on any exception

### config_flow.py

| Class | Purpose |
|-------|---------|
| `NeverDryConfigFlow` | Multi-step setup: sensors → zone → add another → create entry |
| `NeverDryOptionsFlow` | Edit model params or add zones after setup |

## 5. Service registration

Services are registered in `IrrigationController.register_services()`.

| Service | Handler | Behavior |
|---------|---------|----------|
| `never_dry.reset` | `_handle_reset` | Resets reference + all zone deficits |
| `never_dry.irrigate_zone` | `_handle_irrigate_zone` | Single zone: open → wait → close → reset zone deficit |
| `never_dry.irrigate_all` | `_handle_irrigate_all` | All zones sequentially, then reset all deficits |
| `never_dry.stop` | `_handle_stop` | Close all valves, abort cycle (no deficit reset) |

## 6. Config flow

### Zone fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Zone display name |
| `valve` | Yes | Switch entity controlling the valve |
| `area_m2` | Yes | Irrigated area [m²] |
| `system_type` | Yes | Irrigation system → sets default efficiency |
| `efficiency` | No | Override efficiency [0.1–1.0] |
| `plant_family` | No | Plant family → sets seasonal Kc profile |
| `kc` | No | Override Kc [0.1–2.0] |
| `flow_rate_lpm` | Yes | Valve flow rate [L/min] |
| `threshold` | No | Mode A trigger threshold [mm] (default 20) |

## 7. Testing

```bash
cd sw_artifacts
python3 -m pytest tests/ -v
```

| File | Coverage |
|------|----------|
| `test_et_sensor.py` | ET formula, custom params, edge cases, attributes |
| `test_never_dry_sensor.py` | Reference deficit accumulation, reset, VWC mode, invalid inputs |
| `test_volume_duration.py` | Per-zone volume/duration, zone attributes, multi-zone independence |
| `test_controller.py` | Valve control, sequential irrigation, emergency stop, monitoring, system types |
| `test_kc.py` | `compute_kc()` (anchors, interpolation, hemisphere, override), per-zone deficit tracking |

Async controller tests require `pytest-asyncio` (skipped if not installed).

## 8. Adding a new ET tier

To add a new ET calculation method (e.g., Hargreaves-Samani):

1. Add new config keys in `const.py` (e.g., `CONF_T_MAX_SENSOR`, `CONF_T_MIN_SENSOR`)
2. Add a new method in `DrynessIndexSensor` (e.g., `_update_from_hargreaves()`)
3. Add selection logic in `_on_sensor_change()` to choose the appropriate method
4. The broadcast to zone listeners remains the same — zones only need `(dt_h, et_h, rain)`
5. Update `config_flow.py` to expose the new sensor fields
6. Update `strings.json` and `translations/en.json` with UI labels
7. Add tests in `test_never_dry_sensor.py`
