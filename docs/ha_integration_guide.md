# NeverDry — How it works with Home Assistant

A practical guide that follows the NeverDry code to explain **how** and **why** each piece integrates with Home Assistant. Written for developers who know Python but are new to HA custom integrations.

---

## Table of contents

1. [The big picture: how HA loads your code](#1-the-big-picture)
2. [Entry point: `__init__.py`](#2-entry-point)
3. [Entities: what HA expects from you](#3-entities)
4. [Listening to changes: the reactive pattern](#4-listening-to-changes)
5. [Persisting state across restarts: RestoreEntity](#5-persisting-state)
6. [Services: letting the user (and automations) call you](#6-services)
7. [Events: telling the world something happened](#7-events)
8. [Monitoring external state: valve and battery watchers](#8-monitoring-external-state)
9. [The listener/broadcast pattern between our own entities](#9-internal-broadcast)
10. [Config flow: the setup wizard](#10-config-flow)
11. [Putting it all together: what happens when temperature changes](#11-full-walkthrough)

---

## 1. The big picture

Home Assistant discovers integrations by looking inside `custom_components/`. Each subfolder is a "domain" — ours is `never_dry`. HA looks for specific files:

```
custom_components/never_dry/
├── __init__.py        ← HA calls this first
├── manifest.json      ← metadata: name, dependencies, version
├── sensor.py          ← "sensor" platform — HA calls async_setup_entry()
├── button.py          ← "button" platform — same pattern
├── controller.py      ← our own module, not called by HA directly
├── const.py           ← our own constants
├── config_flow.py     ← UI setup wizard
├── services.yaml      ← service definitions for Developer Tools
└── strings.json       ← UI translations
```

**Key concept**: HA has a lifecycle. When the user adds the integration (via UI or YAML), HA:

1. Calls `__init__.py → async_setup_entry()` — you register your platforms
2. For each platform (sensor, button, ...) HA calls `sensor.py → async_setup_entry()` — you create your entities
3. Your entities start receiving events and updating their state

HA manages the entity lifecycle — you just create objects that follow specific patterns.

---

## 2. Entry point: `__init__.py`

```python
# __init__.py (simplified)

PLATFORMS = ["sensor", "button"]

async def async_setup_entry(hass, entry):
    """Called when HA loads our config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True
```

**What it does**: tells HA "I have two platforms: sensor and button. Go load them."

**`hass.data`** is a global dict shared across the entire HA instance. Each integration stores its runtime data there under its domain key. This is how different parts of your integration communicate — there's no other shared state mechanism.

**`entry.data`** is the config that the user entered during setup (temperature sensor, rain sensor, zones, etc.). It's a frozen dict stored in `.storage/` and persists across restarts.

**Why `async_`?** HA runs on asyncio. Every function that does I/O or talks to HA must be async. If you block the event loop, the entire HA UI freezes.

---

## 3. Entities: what HA expects from you

An entity is a Python object that represents something in HA — a sensor, a button, a switch. HA doesn't care how you calculate your values. It only cares that your object:

1. Has a `native_value` property (what to show in the UI)
2. Has metadata properties (name, unique_id, unit, icon)
3. Calls `self.async_write_ha_state()` when the value changes

### Our sensors

```python
# sensor.py — async_setup_entry is called by HA for the "sensor" platform

async def async_setup_entry(hass, entry, async_add_entities):
    """HA calls this. We create entities and hand them over."""
    config = dict(entry.data)
    entities, di_sensor, zone_sensors = _create_entities(hass, config)
    async_add_entities(entities, True)    # ← "here, HA, manage these"
    _setup_controller(hass, config, di_sensor, zone_sensors)
```

**`async_add_entities(entities, True)`** — the `True` means "call `async_update` on each entity immediately". After this call, HA owns these entities. They appear in the UI, get recorded in history, and are available for automations.

### Anatomy of ETSensor

```python
class ETSensor(SensorEntity):
    _attr_name = "ET Hourly Estimate"          # shown in UI
    _attr_unique_id = "et_hourly_estimate"     # must be globally unique
    _attr_native_unit_of_measurement = "mm/h"  # HA shows this next to value
    _attr_state_class = SensorStateClass.MEASUREMENT  # tells HA this is numeric
    _attr_icon = "mdi:sun-thermometer"         # Material Design Icon

    @property
    def native_value(self) -> float:
        """HA reads this to get the current sensor value."""
        return round(self._value, 4)
```

**`_attr_*` class attributes** are HA's "declarative" pattern. Instead of defining `@property` methods for name, icon, etc., you set class attributes prefixed with `_attr_`. HA's base class translates these into the right properties automatically.

**`SensorStateClass.MEASUREMENT`** tells HA "this is a point-in-time measurement" (vs. `TOTAL_INCREASING` for counters like energy meters). This affects how HA records history and calculates statistics.

---

## 4. Listening to changes: the reactive pattern

NeverDry doesn't poll. It **reacts** to sensor changes. This is the core HA pattern for efficiency.

### How ETSensor listens to temperature

```python
class ETSensor(SensorEntity):

    async def async_added_to_hass(self) -> None:
        """Called by HA when this entity is registered and ready."""
        async_track_state_change_event(
            self._hass,
            [self._temp_sensor],        # listen to this entity
            self._on_temp_change         # call this when it changes
        )

    @callback
    def _on_temp_change(self, event) -> None:
        """Runs every time the temperature sensor updates."""
        new_state = event.data.get("new_state")
        t = float(new_state.state)
        self._value = max(0.0, self._alpha * (t - self._t_base) / 24)
        self.async_write_ha_state()  # ← tell HA our value changed
```

**`async_added_to_hass()`** — this is a lifecycle hook. HA calls it after the entity is fully registered. This is the right place to set up listeners because:
- The entity has a valid `hass` reference
- Other entities may already be loaded
- The entity can safely call `async_write_ha_state()`

**`async_track_state_change_event(hass, entity_ids, callback)`** — registers a listener on HA's event bus. Every time any of `entity_ids` changes state, HA calls your callback with a state change event.

**`@callback`** — this decorator marks the function as a "fast callback" that runs **synchronously** on the event loop. Use it when your function is pure computation (no I/O, no `await`). This avoids creating a new asyncio Task for every sensor update, which matters when temperature changes every 30 seconds.

**`self.async_write_ha_state()`** — tells HA "my value changed, update the UI and record in history". Without this call, HA would never know your sensor updated. This is the **only way** to push state to HA.

### The chain reaction

When temperature changes, here's what happens:

```
Temperature sensor changes (e.g., from 18°C to 19°C)
    │
    ├─→ ETSensor._on_temp_change()      → updates ET, writes HA state
    │
    └─→ DrynessIndexSensor._on_sensor_change()  → updates deficit
              │
              └─→ broadcasts (dt_h, et_h, rain) to all zone listeners
                    │
                    ├─→ IrrigationZoneSensor[Orto]._on_et_update()
                    └─→ IrrigationZoneSensor[Prato]._on_et_update()
```

One temperature change triggers **4 state updates** (ET + deficit + 2 zones). HA records all of them.

---

## 5. Persisting state across restarts: RestoreEntity

When HA restarts, all Python objects are destroyed and recreated. Without `RestoreEntity`, your deficit would reset to zero every time.

```python
class DrynessIndexSensor(SensorEntity, RestoreEntity):

    async def async_added_to_hass(self) -> None:
        """Restore previous state after HA restart."""
        last = await self.async_get_last_state()
        if last and last.state not in ("unknown", "unavailable"):
            self._deficit = float(last.state)
```

**`RestoreEntity`** — a mixin from HA that stores your entity's last state in `.storage/core.restore_state`. When your entity starts up, you call `async_get_last_state()` to get a `State` object with the previous value and attributes.

**What's restored**: the state value (the main sensor number) and all `extra_state_attributes`. This is why `IrrigationZoneSensor` stores `deficit_mm` as an attribute — so it can restore the zone-specific deficit.

```python
class IrrigationZoneSensor(SensorEntity, RestoreEntity):

    async def async_added_to_hass(self) -> None:
        last = await self.async_get_last_state()
        if last and last.attributes:
            self._zone_deficit = float(last.attributes.get("deficit_mm", 0.0))
            # Also restore irrigation history
            ts = last.attributes.get("last_irrigated")
            if ts:
                self._last_irrigated = datetime.fromisoformat(ts)
                self._last_volume_delivered = float(
                    last.attributes.get("last_volume_delivered", 0.0)
                )
```

**Why not use a database?** HA's restore mechanism is lightweight and doesn't require external dependencies. For a custom integration, this is the standard way. If you need full history (not just last value), you query the HA recorder — which is what our backfill feature does.

---

## 6. Services: letting the user call you

Services are actions that the user (or automations) can trigger. They appear in Developer Tools → Services.

### Registration

```python
# controller.py

class IrrigationController:

    def register_services(self) -> None:
        self._hass.services.async_register(
            DOMAIN,                        # "never_dry"
            SERVICE_IRRIGATE_ZONE,         # "irrigate_zone"
            self._handle_irrigate_zone     # async handler
        )
```

**`hass.services.async_register(domain, service_name, handler)`** — registers a service. The handler receives a `ServiceCall` object with `call.data` containing the parameters.

### Service definition (services.yaml)

```yaml
irrigate_zone:
  name: Irrigate zone
  description: Start irrigation for a single zone.
  fields:
    zone_name:
      name: Zone name
      description: The name of the zone to irrigate.
      required: true
      selector:
        text:
```

**`services.yaml`** tells the HA UI how to render the service call form. Without it, the service works but the UI won't know what fields to show. `selector: text:` means "render a text input".

### Handler

```python
async def _handle_irrigate_zone(self, call: ServiceCall) -> None:
    zone_name = call.data.get(ATTR_ZONE_NAME)
    if zone_name not in self._zones:
        _LOGGER.error("Zone '%s' not found", zone_name)
        return
    if self._running:
        _LOGGER.warning("Irrigation already in progress")
        return
    self._hass.async_create_task(self._irrigate_zones([zone_name]))
```

**`hass.async_create_task()`** — schedules a coroutine to run in the background without blocking the service call. The service returns immediately, and the irrigation runs in the background. This is important because service calls have a timeout — if your handler takes too long, HA will error.

---

## 7. Events: telling the world something happened

Events are HA's pub/sub mechanism. Unlike services (which are "call me"), events are "I'm telling you something happened".

```python
# controller.py — after zone irrigation completes

self._hass.bus.async_fire(
    EVENT_IRRIGATION_COMPLETE,      # "never_dry_irrigation_complete"
    {
        "zone": zone_name,
        "source": "automatic",      # or "manual"
        "volume_liters": 45.2,
        "duration_s": 340,
        "deficit_mm": 5.0,
    },
)
```

**`hass.bus.async_fire(event_type, data)`** — fires an event on HA's event bus. Anyone can listen — automations, other integrations, scripts.

**Why events instead of notifications?** Events are structured and automatable. The user can build automations like:

```yaml
# HA automation example
trigger:
  - platform: event
    event_type: never_dry_irrigation_complete
    event_data:
      source: automatic
action:
  - service: notify.mobile_app
    data:
      title: "Irrigation complete"
      message: "Zone {{ trigger.event.data.zone }}: {{ trigger.event.data.volume_liters }}L"
```

With a `persistent_notification`, the user can only see it — not act on it programmatically.

---

## 8. Monitoring external state: valve and battery watchers

NeverDry monitors valve switches and battery sensors that it doesn't own. This is the same `async_track_state_change_event` pattern, but applied to **external** entities.

### Valve monitoring (detecting manual irrigation)

```python
def register_services(self) -> None:
    # ... service registration ...

    # Watch all configured valve switches
    valve_entities = [v for v in self._valve_to_zone if v]
    if valve_entities:
        async_track_state_change_event(
            self._hass, valve_entities, self._on_valve_state_change
        )
```

**The problem**: if the user opens a valve manually (from HA dashboard, or physically), NeverDry doesn't know water was delivered. The deficit stays high and NeverDry will irrigate again unnecessarily.

**The solution**: listen to valve state changes. If the valve goes `off → on → off` and the controller is NOT running (`self._running is False`), it was manual irrigation.

```python
@callback
def _on_valve_state_change(self, event) -> None:
    if self._running:
        return  # we're driving the valve, ignore

    # ... extract entity_id, old_state, new_state ...

    if old_state.state == "off" and new_state.state == "on":
        # Record flow meter baseline if available
        self._manual_valve_open[entity_id] = flow_start

    elif old_state.state == "on" and new_state.state == "off":
        # Valve closed — compensate deficit
        if zone.flow_meter_sensor and flow_start is not None:
            delivered_liters = flow_end - flow_start
            delivered_mm = delivered_liters / zone._area
            zone._zone_deficit -= delivered_mm * zone._efficiency
        else:
            zone.reset_deficit()  # no meter → assume fully irrigated
```

**With flow meter**: we measure exactly how much water was delivered and subtract the equivalent mm from the deficit. Formula: `mm = liters / area_m² × efficiency`.

**Without flow meter**: we can't know how much water was delivered, so we reset the deficit to zero (conservative assumption: enough water was delivered).

### Battery monitoring

Same pattern — we listen to battery sensor changes and send a notification when the level drops below 15%.

```python
@callback
def _on_battery_change(self, event) -> None:
    level = float(new_state.state)
    if level <= DEFAULT_BATTERY_LOW_THRESHOLD:
        if zone_name not in self._battery_alerted:
            self._battery_alerted.add(zone_name)  # alert once
            self._hass.async_create_task(
                self._hass.services.async_call(
                    "persistent_notification", "create", {...}
                )
            )
    else:
        self._battery_alerted.discard(zone_name)  # reset on recovery
```

**Why `async_create_task` around `services.async_call`?** Because `_on_battery_change` is marked `@callback` (synchronous), but `services.async_call` is a coroutine. You can't `await` inside a `@callback`. So we wrap it in `async_create_task` to schedule it without blocking.

**Why `_battery_alerted` set?** To avoid spamming. Battery sensors update frequently (every few minutes). Without the set, you'd get 100+ notifications per day when the battery is at 14%.

---

## 9. The listener/broadcast pattern between our entities

NeverDry uses its own internal pub/sub to connect `DrynessIndexSensor` to `IrrigationZoneSensor` instances. This is NOT a HA API — it's pure Python.

```python
# In DrynessIndexSensor
class DrynessIndexSensor:
    def __init__(self, ...):
        self._zone_listeners: list[Callable] = []

    def register_zone_listener(self, listener: Callable) -> None:
        self._zone_listeners.append(listener)

    def _broadcast_to_zones(self, dt_h, et_h, rain):
        for listener in self._zone_listeners:
            listener(dt_h, et_h, rain)

# In IrrigationZoneSensor
class IrrigationZoneSensor:
    def __init__(self, ..., dryness_sensor):
        # Register at construction time
        dryness_sensor.register_zone_listener(self._on_et_update)

    def _on_et_update(self, dt_h, et_h, rain):
        kc = self._get_current_kc()
        self._zone_deficit += et_h * kc * dt_h - rain
        self.async_write_ha_state()
```

**Why not use HA's event bus?** Performance. HA's event bus serializes data, checks permissions, filters subscribers. For internal communication between our own objects that happens on every temperature change, a direct Python callback is orders of magnitude faster.

**Why not have zones listen to the temperature sensor directly?** Because the `DrynessIndexSensor` already computes `dt_h` (time delta), `et_h` (ET rate), and `rain` (rain delta). If each zone did this independently, we'd:
1. Duplicate the rain delta computation (which has state — `_last_rain`)
2. Risk inconsistencies between zones
3. Create N+1 listeners instead of 1 listener + N callbacks

---

## 10. Config flow: the setup wizard

`config_flow.py` implements the multi-step UI that appears when the user adds the integration.

```python
class NeverDryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = CONFIG_VERSION  # for migration

    async def async_step_user(self, user_input=None):
        """Step 1: basic sensors."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_zone()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_TEMP_SENSOR): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                # ...
            })
        )
```

**`self.async_show_form()`** — renders a form in the UI. HA uses `vol.Schema` (voluptuous) for validation and `selector.*` for UI widgets.

**`selector.EntitySelector`** — renders an entity picker dropdown filtered by domain. The user sees a searchable list of all their sensors instead of typing entity IDs manually.

**Why multi-step?** Because we need to collect zones iteratively. Step 1 collects global sensors, Step 2 collects one zone, Step 3 asks "add another zone?". Each step returns either `await self.async_step_next()` (go to next step) or `self.async_create_entry()` (done, save config).

---

## 11. Putting it all together

Here's a complete trace of what happens when the temperature sensor changes from 18°C to 20°C, with a rain sensor at 0mm, one zone "Orto" (Kc=0.85, area=20m², efficiency=0.9), and a deficit of 5.0mm.

```
1. HA event bus fires state_changed for sensor.temperature
   │
   ├──→ ETSensor._on_temp_change()
   │    T=20, α=0.22, T_base=9
   │    ET_h = 0.22 × (20 - 9) / 24 = 0.1008 mm/h
   │    → async_write_ha_state() → HA records 0.1008
   │
   └──→ DrynessIndexSensor._on_sensor_change()
        now - last_update = 300s → dt_h = 0.0833h
        ET_h = 0.1008 mm/h (same formula)
        rain_delta = 0.0 mm (no rain)
        deficit = 5.0 + 0.1008 × 0.0833 - 0.0 = 5.0084 mm
        → async_write_ha_state() → HA records 5.01
        │
        └──→ _broadcast_to_zones(0.0833, 0.1008, 0.0)
             │
             └──→ IrrigationZoneSensor[Orto]._on_et_update()
                  Kc = 0.85 (from plant family + day of year)
                  zone_deficit = 5.0 + 0.1008 × 0.85 × 0.0833 - 0.0
                                = 5.0071 mm
                  volume = 5.0071 × 20 / 0.9 = 111.3 L
                  duration = 111.3 / 8.0 × 60 = 835 s ≈ 14 min
                  → async_write_ha_state() → HA records 111.3 L
```

**One temperature change → 3 entities updated → 3 history records → 3 UI updates.** All synchronous, all on the event loop, total time < 1ms.

When the user presses the "Irrigate Orto" button:

```
2. ButtonEntity.async_press()
   │
   └──→ hass.services.async_call("never_dry", "irrigate_zone", {zone: "Orto"})
        │
        └──→ IrrigationController._handle_irrigate_zone()
             │
             └──→ hass.async_create_task(_irrigate_zones(["Orto"]))
                  │ (runs in background)
                  │
                  ├──→ _open_valve("switch.valve_orto")
                  │    → hass.services.async_call("switch", "turn_on", ...)
                  │    zone.set_irrigating(True) → async_write_ha_state()
                  │
                  ├──→ _wait_with_stop_check(835)  # 835 seconds
                  │    (checks stop_requested every 1s)
                  │
                  ├──→ _close_valve("switch.valve_orto")
                  │    → hass.services.async_call("switch", "turn_off", ...)
                  │    zone.set_irrigating(False) → async_write_ha_state()
                  │
                  ├──→ hass.bus.async_fire("never_dry_irrigation_complete", {...})
                  │
                  └──→ zone.reset_deficit()
                       _last_irrigated = now
                       _last_volume_delivered = 111.3 L
                       _zone_deficit = 0.0
                       → async_write_ha_state()
```

---

## Quick reference: HA APIs we use

| API | What | Where we use it |
|-----|------|-----------------|
| `SensorEntity` | Base class for numeric sensors | ETSensor, DrynessIndexSensor, IrrigationZoneSensor |
| `RestoreEntity` | Persist state across restarts | DrynessIndexSensor, IrrigationZoneSensor |
| `ButtonEntity` | Pressable button in UI | MarkIrrigatedButton, IrrigateButton |
| `async_track_state_change_event()` | Listen to entity state changes | Temperature, rain, valve, battery monitoring |
| `async_track_time_interval()` | Periodic timer | Monitoring mode (6h check) |
| `hass.services.async_register()` | Register a callable service | irrigate_zone, irrigate_all, stop, reset, mark_irrigated |
| `hass.services.async_call()` | Call another service | switch.turn_on/off, persistent_notification |
| `hass.bus.async_fire()` | Fire an event | never_dry_irrigation_complete |
| `hass.states.get()` | Read another entity's state | Flow meter, valve state |
| `hass.data` | Shared runtime storage | Store config entry data |
| `hass.config.latitude` | Read HA installation location | Hemisphere detection for Kc |
| `async_get_last_state()` | Restore previous state (RestoreEntity) | Deficit, last_irrigated |
| `config_entries.ConfigFlow` | Multi-step setup wizard | NeverDryConfigFlow |
| `@callback` | Mark sync function as event-loop-safe | All state change handlers |
| `hass.async_create_task()` | Schedule background coroutine | Irrigation cycles, battery notifications |
