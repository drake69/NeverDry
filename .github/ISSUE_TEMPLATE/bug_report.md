---
name: Bug report
about: Something doesn't work as expected
title: ''
labels: bug
assignees: ''
---

**Describe the bug**
A clear and concise description of what happens and what you expected instead.

**Environment**
- Home Assistant version:
- NeverDry version:
- Installation method: HACS / manual
- Valve hardware and integration (e.g. SONOFF SWV via Zigbee2MQTT):
- Delivery mode of the affected zone: estimated_flow / flow_meter / volume_preset

**Steps to reproduce**
1.
2.
3.

**Debug logs**
Enable debug logging and paste the relevant lines:

```yaml
logger:
  logs:
    custom_components.never_dry: debug
```

```
(paste log lines here)
```

**Diagnostics**
If possible, attach the diagnostics file (Settings → Devices & Services → NeverDry → Download diagnostics).

**Additional context**
Screenshots, zone configuration, anything else that helps.
