# NeverDry

**Smart irrigation for Home Assistant** — a scientific water balance model that knows *when* and *how long* to water your garden.

[![Tests](https://github.com/drake69/NeverDry/actions/workflows/tests.yml/badge.svg)](https://github.com/drake69/NeverDry/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/drake69/NeverDry/graph/badge.svg)](https://codecov.io/gh/drake69/NeverDry)
[![HACS Validation](https://github.com/drake69/NeverDry/actions/workflows/hacs.yml/badge.svg)](https://github.com/drake69/NeverDry/actions/workflows/hacs.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/drake69/NeverDry)](https://github.com/drake69/NeverDry/releases)

<a href="https://www.patreon.com/c/69Drake"><img src="https://img.shields.io/badge/Support_on_Patreon-FF424D?style=for-the-badge&logo=patreon&logoColor=white" alt="Support on Patreon" height="35"></a>

---

## What it does

NeverDry tracks a real-time **soil water deficit** for each irrigation zone. Instead of fixed timers, it calculates exactly how much water your garden has lost through evaporation and how much it got back from rain — then irrigates only when needed, for exactly the right duration.

**Key idea: 1 mm of deficit = 1 liter per m² of water needed.**

## Features

- **Scientific model** — simplified FAO-56 water balance with two calibratable parameters
- **Per-zone crop coefficient (Kc)** — 10 plant families with seasonal variation, auto-adjusted for hemisphere
- **Direct valve control** — opens/closes valves, calculates exact duration, sequential multi-zone
- **Per-zone deficit tracking** — each zone dries out at its own rate based on plant type
- **Rain-aware** — deficit decreases with each rain event, skips irrigation on rainy days
- **Two scheduling modes** — reactive threshold (Mode A) and nightly deficit-based (Mode B)
- **Monitoring mode** — no valves? Get a notification every 6h when irrigation is needed
- **Emergency stop** — instantly closes all valves
- **State persistence** — survives HA restarts via RestoreEntity
- **UI config flow** — set up entirely from the HA interface
- **Zero dependencies** — pure Python, no external libraries

## Sensors

| Sensor | Unit | Description |
|--------|------|-------------|
| `sensor.et_hourly_estimate` | mm/h | Instantaneous evapotranspiration rate |
| `sensor.never_dry` | mm | Reference soil water deficit (Kc=1.0) |
| `sensor.irrigation_<zone>` | L | Per-zone volume with duration, deficit, Kc |

Each zone sensor exposes: `volume_liters`, `duration_s`, `deficit_mm`, `kc`, `plant_family`, `valve`, `irrigating`, and more.

## Services

| Service | Description |
|---------|-------------|
| `never_dry.irrigate_zone` | Irrigate one zone: open valve, wait, close, reset zone deficit |
| `never_dry.irrigate_all` | All zones sequentially, then reset all deficits |
| `never_dry.stop` | Emergency stop — close all valves immediately |
| `never_dry.reset` | Reset all deficits to zero |

## Plant Families

Each zone can be assigned a plant family with seasonal Kc values (northern hemisphere — auto-flipped for southern):

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

You can also set a **manual Kc override** per zone (0.1–2.0) if you know the exact value.

---

## Installation

### HACS (recommended)

1. Open **HACS** in Home Assistant
2. Go to **Integrations** > **Custom repositories**
3. Add `https://github.com/drake69/NeverDry` — category **Integration**
4. Search for **NeverDry** and install
5. Restart Home Assistant
6. **Settings** > **Devices & Services** > **Add Integration** > search **NeverDry**

### Manual

1. Copy `custom_components/never_dry/` into your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Add the integration from the UI

---

## Configuration

NeverDry is configured entirely through the UI — no YAML required.

### Step 1: Sensors and model

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| Temperature sensor | Yes | — | Outdoor temperature [°C] |
| Rain sensor | Yes | — | Precipitation sensor [mm] |
| Rain sensor type | No | event | `event` (mm per event, tipping bucket) or `daily_total` (cumulative mm since midnight) |
| Alpha (α) | No | 0.22 | ET coefficient [mm/°C/day] |
| Base temperature | No | 9.0 | Below this, ET = 0 [°C] |
| Max deficit (D_max) | No | 100.0 | Upper clamp [mm] |
| VWC sensor | No | — | Soil moisture (bypasses ET model) |

### Step 2: Irrigation zones

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| Zone name | Yes | — | Display name |
| Valve | No | — | Switch entity controlling the valve (omit for monitoring mode) |
| Area | Yes | — | Irrigated area [m²] |
| System type | Yes | — | Drip / micro-sprinkler / sprinkler / manual |
| Efficiency | No | (from type) | Override distribution efficiency [0.1–1.0] |
| Plant family | No | — | Sets seasonal Kc profile |
| Custom Kc | No | — | Override Kc [0.1–2.0] |
| Flow rate | Yes | — | Valve flow rate [L/min] |
| Threshold | No | 20.0 | Mode A trigger [mm] |

---

## Automations

### Mode A — Threshold trigger (per zone)

```yaml
automation:
  - alias: "Irrigation — Vegetable Garden"
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

### Mode B — Nightly all zones

```yaml
automation:
  - alias: "Nightly irrigation"
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

---

## Scientific Background

Based on the FAO-56 water balance (Allen et al., 1998):

```
D_zone(t) = clamp(D_zone(t-1) + ET_h × Kc × Δt − rain,  0,  D_max)

ET_h = max(0, α × (T − T_base) / 24)     [mm/h]
Kc   = f(day_of_year, plant_family)        [—]
V    = D_zone × Area / Efficiency          [L]
t    = V / FlowRate × 60                   [s]
```

Integration is event-driven (forward Euler, variable Δt).

---

## Documentation

- [User Manual](docs/user_manual.md)
- [Developer Manual](docs/developer_manual.md)
- [HACS Publishing Guide](docs/hacs_publishing_guide.md)
- [Project Homepage](https://drake69.github.io/NeverDry/)

---

## Support

If NeverDry saves your garden (and your water bill), consider supporting the project:

<a href="https://www.patreon.com/c/69Drake"><img src="https://img.shields.io/badge/Support_on_Patreon-FF424D?style=for-the-badge&logo=patreon&logoColor=white" alt="Support on Patreon" height="35"></a>

---

## Disclaimer

NeverDry is a **hobby project for residential use**. It is not certified for agricultural, commercial, or safety-critical applications. The authors accept no liability for crop damage, water waste, property damage, or any other loss resulting from the use of this software.

The ET model is a simplification of the FAO-56 standard and is **not a substitute for professional agronomic advice**. Crop coefficients (Kc) are approximate seasonal averages for typical residential plants — actual water needs depend on soil type, microclimate, plant health, and many other factors.

**Always monitor your irrigation system** and verify that valves open and close correctly. Use the emergency stop service (`never_dry.stop`) if anything goes wrong.

---

## Acknowledgments

This project was developed with the assistance of [Claude](https://claude.ai) by [Anthropic](https://anthropic.com) — an AI assistant that contributed to architecture design, code implementation, scientific modeling, documentation, and testing.

---

## Scientific References

NeverDry is based on established agronomic science. The key references are:

### Core Model

- **Allen, R.G., Pereira, L.S., Raes, D., Smith, M.** (1998). *Crop evapotranspiration: guidelines for computing crop water requirements.* FAO Irrigation and Drainage Paper 56. Rome: FAO. — [Full text (FAO)](https://www.fao.org/4/x0490e/x0490e00.htm)

### Evapotranspiration Methods

- **Hargreaves, G.H., Samani, Z.A.** (1985). Reference crop evapotranspiration from temperature. *Applied Engineering in Agriculture*, 1(2), 96–99. DOI: [10.13031/2013.26773](https://doi.org/10.13031/2013.26773) — [PDF](https://academic.uprm.edu/hdc/TMAG4035_ETo/hargreaves%20samani%201985.pdf)
- **Penman, H.L.** (1948). Natural evaporation from open water, bare soil and grass. *Proc. R. Soc. London A*, 193(1032), 120–145. DOI: [10.1098/rspa.1948.0037](https://doi.org/10.1098/rspa.1948.0037)
- **Monteith, J.L.** (1965). Evaporation and environment. *Symp. Soc. Exp. Biol.*, 19, 205–234. — [Rothamsted Repository](https://repository.rothamsted.ac.uk/item/8v5v7/evaporation-and-environment) | [PubMed](https://pubmed.ncbi.nlm.nih.gov/5321565/)

### Deficit Irrigation

- **Fereres, E., Soriano, M.A.** (2007). Deficit irrigation for reducing agricultural water use. *J. Exp. Bot.*, 58(2), 147–159. DOI: [10.1093/jxb/erl165](https://doi.org/10.1093/jxb/erl165)

---

## License

[MIT](LICENSE) — Luigi Corsaro
