## NeverDry — Smart Irrigation for Home Assistant

NeverDry is a custom integration that calculates a real-time **soil water deficit** using a scientific water balance model (FAO-56 simplified). It tells you exactly *when* and *how long* to irrigate each zone of your garden.

### Highlights

- **Per-zone crop coefficient (Kc)** with 10 plant families and seasonal variation
- **Direct valve control** with sequential multi-zone irrigation
- **Rain-aware** — skips irrigation automatically on rainy days
- **Monitoring mode** — works without smart valves (notification alerts)
- **UI config flow** — no YAML needed

### Requirements

- Home Assistant 2024.1.0+
- Outdoor temperature sensor (°C)
- Rain sensor (mm/event)
- Optional: smart valve(s), soil moisture sensor

### Links

- [User Manual](https://github.com/drake69/NeverDry/blob/main/docs/user_manual.md)
- [Project Homepage](https://drake69.github.io/NeverDry/)
