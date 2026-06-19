# NeverDry

**Smart irrigation for Home Assistant** — knows exactly when your garden needs water, calculates how long to run the valve, and makes sure it actually closes.

[![Tests](https://github.com/drake69/NeverDry/actions/workflows/tests.yml/badge.svg)](https://github.com/drake69/NeverDry/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/drake69/NeverDry/graph/badge.svg)](https://codecov.io/gh/drake69/NeverDry)
[![HACS Validation](https://github.com/drake69/NeverDry/actions/workflows/hacs.yml/badge.svg)](https://github.com/drake69/NeverDry/actions/workflows/hacs.yml)
[![Release](https://github.com/drake69/NeverDry/actions/workflows/release.yml/badge.svg)](https://github.com/drake69/NeverDry/actions/workflows/release.yml)
[![Security](https://github.com/drake69/NeverDry/actions/workflows/security.yml/badge.svg)](https://github.com/drake69/NeverDry/actions/workflows/security.yml)
[![Lint](https://github.com/drake69/NeverDry/actions/workflows/lint.yml/badge.svg)](https://github.com/drake69/NeverDry/actions/workflows/lint.yml)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5)](https://github.com/hacs/integration)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![GitHub release](https://img.shields.io/github/v/release/drake69/NeverDry)](https://github.com/drake69/NeverDry/releases)

---

## Why NeverDry

**Your garden tells you when it's thirsty — NeverDry listens.**

NeverDry tracks how much water your soil has lost to heat and wind, and how much it got back from rain. When the deficit crosses your threshold, it opens the valve for exactly the right amount of time. No fixed timers. No guessing. 1 mm of deficit = 1 liter per m² of water needed.

**And it makes sure the valve always closes.**

If a valve doesn't respond after three attempts, NeverDry blocks that zone and shows a warning on your dashboard. Three independent mechanisms make sure water can't run indefinitely — hardware timeout, software watchdog, and a per-valve state machine that remembers what happened.

## Features

- **Knows when your garden is thirsty** — tracks heat, evaporation, and rainfall in real time; irrigates only when needed
- **Each plant gets its own schedule** — 10 plant profiles (lawn, citrus, succulents, roses, ...) with seasonal variation; NeverDry knows your lawn drinks more in July than your lavender ever will
- **Knows how much water to deliver** — calculates exactly how many liters each zone needs; if you have a flow meter, it measures delivery directly; otherwise it computes run time from flow rate
- **Zones are independent** — the rose bed and the lawn dry out at different rates; each zone tracks its own deficit
- **Skips irrigation after rain** — tracks how much rain actually fell and subtracts it from the deficit
- **Valve always closes** — if a valve doesn't respond 3 times, NeverDry blocks it, shows the state on the dashboard, and waits for you to check
- **Two scheduling modes** — water when the deficit crosses a threshold (Mode A) or every night based on current deficit (Mode B)
- **Works without valves too** — no hardware? NeverDry sends a notification when watering is needed and by how much
- **Emergency stop** — one button closes all valves immediately
- **Grabbed the hose? Just tell NeverDry** — "Mark as irrigated" button keeps the deficit accurate even when you water manually
- **Survives restarts** — your deficit history is saved across HA restarts
- **Update in one click** — HACS notifies you when a new version is available; your settings are always preserved
- **Set up from the UI** — no YAML required
- **Zero dependencies** — pure Python, no extra packages

## Sensors and entities

| Entity | Unit | Description |
|--------|------|-------------|
| `sensor.et_hourly_estimate` | mm/h | Instantaneous evapotranspiration rate |
| `sensor.never_dry` | mm | Reference soil water deficit (Kc=1.0) |
| `sensor.<zone>_volume` | L | Per-zone volume delivered; attributes: `duration_s`, `deficit_mm`, `kc`, `plant_family`, `irrigating`, ... |
| `sensor.<zone>_deficit` | mm | Per-zone water deficit; attributes: `valve_fsm_state`, `valve_in_maintenance`, `irrigating`, `flow_rate_lpm` |
| `sensor.<zone>_valve` | — | Mirror of the physical valve state (`open`/`closed`) inside the zone device card |
| `sensor.<zone>_battery` | % | Mirror of the valve battery sensor inside the zone device card |
| `sensor.<zone>_flow_meter` | L/min | Mirror of the flow meter inside the zone device card |
| `button.<zone>_reset_maintenance` | — | Unlock a valve that NeverDry blocked after repeated failures |

## Services

| Service | Description |
|---------|-------------|
| `never_dry.irrigate_zone` | Open valve, water for the calculated duration, close, update zone deficit |
| `never_dry.irrigate_all` | Water all zones one by one, then mark all as done |
| `never_dry.stop` | Emergency stop — close all valves immediately |
| `never_dry.reset` | Reset all zone deficits to zero |
| `never_dry.reset_valve` | Unlock a valve blocked by NeverDry after repeated close failures |

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

## Updating

**Via HACS**: HACS notifies you when a new version is available. Click **Update** and restart HA.

**Manual**: Download the latest `never_dry.zip` from [Releases](https://github.com/drake69/NeverDry/releases), replace the `custom_components/never_dry/` folder, and restart HA.

Your configuration and sensor history are preserved automatically. If the new version changes the config schema, settings are migrated seamlessly — no need to remove and re-add the integration.

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
| Valve | No | — | Switch entity controlling the valve (omit for monitoring-only mode) |
| Area | Yes | — | Irrigated area [m²] |
| System type | Yes | — | Drip / micro-sprinkler / sprinkler / manual |
| Efficiency | No | (from type) | Override distribution efficiency [0.1–1.0] |
| Plant family | No | — | Sets seasonal Kc profile |
| Custom Kc | No | — | Override Kc [0.1–2.0] |
| Flow rate | Yes | — | Valve flow rate [L/min] |
| Threshold | No | 20.0 | Mode A trigger [mm] |
| Battery sensor | No | — | Valve battery sensor — mirrored in the zone device card |
| Flow meter sensor | No | — | Flow meter entity — mirrored in the zone device card |

---

## Scientific Background

NeverDry tracks how much water your soil has lost to heat (evapotranspiration) and gained from rain, and computes the difference — the *water deficit* — in millimetres. 1 mm = 1 litre per m². When the deficit crosses your threshold, NeverDry calculates the volume needed and runs the valve for exactly that long. Every rain event reduces the deficit; every irrigation session resets it. Plants that drink more in summer (lawn, vegetables) get a higher multiplier; drought-tolerant plants (lavender, succulents) get a lower one — that multiplier is called Kc.

The model is based on FAO-56 (Allen et al., 1998):

```
D_zone(t) = clamp(D_zone(t-1) + ET_h × Kc × Δt − ΔP,  0,  D_max)

ET_h = max(0, α × (T − T_base) / 24)     [mm/h]  evapotranspiration
Kc   = f(day_of_year, plant_family)        [—]     crop coefficient
ΔP   = rain_delta(sensor_type)             [mm]    precipitation increment
V    = D_zone × Area / Efficiency          [L]     volume needed
t    = V / FlowRate × 60                   [s]     irrigation duration
```

**Key design choices:**
- Integration is event-driven (forward Euler, variable Δt) — no fixed polling interval
- Each zone tracks its own deficit scaled by Kc, not a shared global value
- Rain is always processed as a **delta** (increment since last reading), not a raw value — this correctly handles both tipping-bucket sensors (mm per event) and cumulative daily-total sensors (mm since midnight)

---

## Documentation

- [User Manual](docs/user_manual.md)
- [Developer Manual](docs/developer_manual.md)
- [Project Homepage](https://drake69.github.io/NeverDry/)

## Support

If NeverDry saves your garden (and your water bill), consider a one-time donation:

<a href="https://ko-fi.com/drake69"><img src="https://img.shields.io/badge/Support_on_Ko--fi-FF5E5B?style=for-the-badge&logo=ko-fi&logoColor=white" alt="Support on Ko-fi" height="35"></a>

---

## Disclaimer

NeverDry is a **hobby project for residential use**. It is not certified for agricultural, commercial, or safety-critical applications. The authors accept no liability for crop damage, water waste, property damage, or any other loss resulting from the use of this software.

The ET model is a simplification of the FAO-56 standard and is **not a substitute for professional agronomic advice**. Crop coefficients (Kc) are approximate seasonal averages for typical residential plants — actual water needs depend on soil type, microclimate, plant health, and many other factors.

**Always monitor your irrigation system** and verify that valves open and close correctly. Use the emergency stop service (`never_dry.stop`) if anything goes wrong.

---

## Acknowledgments

Developed by [drake69](https://github.com/drake69) with AI assistance ([Claude](https://claude.ai) by Anthropic).

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
