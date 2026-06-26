/**
 * NeverDry Zone Card — single-file custom Lovelace card.
 *
 * Vanilla custom element (no build step, no Lit/CDN dependency) so it can be
 * served and auto-registered directly by the Python integration.
 *
 * Shows every entity of ONE NeverDry zone-device grouped into readable blocks:
 *   - Water status (Dryness/Deficit + Rain)
 *   - Deficit-vs-threshold bar
 *   - Irrigation (last/duration/mode/flow)
 *   - Water delivered (session/yearly/last volume)
 *   - Actions (Irrigate / Mark irrigated / Reset valve) as real buttons
 *
 * Entity resolution is locale-independent: entities are matched by their
 * registry `original_name` (the English `_attr_name` defined in sensor.py /
 * button.py), not by entity_id, so user renames don't break the card.
 */

const CARD_VERSION = "0.1.1";

// Static UI strings that are NOT backed by an entity (everything else is read
// from the entity's localized friendly_name / formatEntityState, so it follows
// the integration's own translations and the user's language automatically).
const I18N = {
  en: {
    selectZone: "Select a zone in the card editor.",
    noEntities: "No NeverDry entities found for this device.",
    noZones: "No NeverDry zones found.",
    zone: "Zone",
    selectPlaceholder: "Select a zone…",
    due: "irrigation due",
    barUnavailable: "deficit / threshold unavailable",
    irrigateNow: "Irrigate now",
  },
  it: {
    selectZone: "Seleziona una zona nell'editor della scheda.",
    noEntities: "Nessuna entità NeverDry trovata per questo dispositivo.",
    noZones: "Nessuna zona NeverDry trovata.",
    zone: "Zona",
    selectPlaceholder: "Seleziona una zona…",
    due: "irrigazione necessaria",
    barUnavailable: "deficit / soglia non disponibili",
    irrigateNow: "Irriga ora",
  },
};

function t(hass, key) {
  const lang = ((hass && hass.language) || "en").split("-")[0];
  return (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
}

// Map of role -> entity_id suffix (the English _attr_name slug, stable for
// un-renamed entities). `hass.entities` exposes entity_id/device_id/platform but
// NOT original_name, so we match on the object_id suffix instead. Longest suffix
// wins (see _zoneEntities) so "_last_volume" beats "_volume", etc.
const ROLE_SUFFIX = {
  volume: "_volume",
  deficit: "_deficit",
  rain: "_rain",
  threshold: "_threshold",
  sessionWater: "_session_water",
  yearlyWater: "_yearly_water",
  lastVolume: "_last_volume",
  flowRate: "_flow_rate",
  duration: "_duration",
  lastDuration: "_last_duration",
  lastIrrigated: "_last_irrigated",
  lastSource: "_last_source",
  irrigationMode: "_irrigation_mode",
  irrigationTime: "_irrigation_time",
  kc: "_kc",
  area: "_area",
  efficiency: "_efficiency",
  // buttons
  btnIrrigate: "_irrigate",
  btnMark: "_mark_irrigated",
  btnReset: "_reset_valve",
};

// A NeverDry zone is a device created by the integration with this model.
const ZONE_MODEL = "Irrigation Zone";

class NeverDryZoneCard extends HTMLElement {
  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    this._config = config;
    this._built = false;
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._render();
  }

  getCardSize() {
    return 7;
  }

  static getConfigElement() {
    return document.createElement("never-dry-zone-card-editor");
  }

  static getStubConfig(hass) {
    const device = pickFirstZoneDevice(hass);
    return { type: "custom:never-dry-zone-card", device_id: device || "" };
  }

  // ---- entity resolution ------------------------------------------------

  _zoneEntities() {
    // Returns { role: stateObj } for the configured device, matching each
    // entity by the longest entity_id suffix in ROLE_SUFFIX.
    const hass = this._hass;
    const deviceId = this._config && this._config.device_id;
    const out = {};
    if (!hass || !deviceId || !hass.entities) return out;

    const roles = Object.entries(ROLE_SUFFIX).sort((a, b) => b[1].length - a[1].length);
    for (const ent of Object.values(hass.entities)) {
      if (ent.device_id !== deviceId) continue;
      const objectId = (ent.entity_id.split(".")[1] || "").toLowerCase();
      const st = hass.states[ent.entity_id];
      if (!st) continue;
      for (const [role, suffix] of roles) {
        if (!out[role] && objectId.endsWith(suffix)) {
          out[role] = st;
          break;
        }
      }
    }
    return out;
  }

  _deviceName() {
    const hass = this._hass;
    const deviceId = this._config && this._config.device_id;
    if (hass && hass.devices && hass.devices[deviceId]) {
      const d = hass.devices[deviceId];
      return d.name_by_user || d.name || "NeverDry zone";
    }
    return "NeverDry zone";
  }

  /** Localized short label for an entity = its friendly_name minus device prefix. */
  _label(st, fallback) {
    const fn = st && st.attributes && st.attributes.friendly_name;
    if (fn) {
      const dn = this._deviceName();
      return fn.startsWith(dn + " ") ? fn.slice(dn.length + 1) : fn;
    }
    return fallback;
  }

  // ---- rendering --------------------------------------------------------

  _render() {
    if (!this._hass || !this._config) return;

    if (!this._config.device_id) {
      this._renderEmpty(t(this._hass, "selectZone"));
      return;
    }
    const ents = this._zoneEntities();
    if (Object.keys(ents).length === 0) {
      this._renderEmpty(t(this._hass, "noEntities"));
      return;
    }

    if (!this._built) this._buildStructure();
    this._update(ents);
  }

  _renderEmpty(msg) {
    this._built = false;
    this.innerHTML = `
      <ha-card header="NeverDry">
        <div style="padding:16px;color:var(--secondary-text-color)">${msg}</div>
      </ha-card>`;
  }

  _buildStructure() {
    this.innerHTML = `
      <ha-card>
        <style>${CARD_CSS}</style>
        <div class="nd-head">
          <ha-icon icon="mdi:sprinkler-variant"></ha-icon>
          <span class="nd-title"></span>
        </div>

        <div class="nd-bar-wrap">
          <div class="nd-bar-labels">
            <span class="nd-bar-lbl"></span><span class="nd-bar-val"></span>
          </div>
          <div class="nd-bar"><div class="nd-bar-fill"></div></div>
          <div class="nd-bar-sub"></div>
        </div>

        <div class="nd-grid" data-block="status"></div>
        <div class="nd-sep"></div>
        <div class="nd-grid" data-block="irrigation"></div>
        <div class="nd-sep"></div>
        <div class="nd-grid" data-block="water"></div>

        <div class="nd-actions"></div>
      </ha-card>`;

    this._el = {
      title: this.querySelector(".nd-title"),
      barLbl: this.querySelector(".nd-bar-lbl"),
      barVal: this.querySelector(".nd-bar-val"),
      barFill: this.querySelector(".nd-bar-fill"),
      barSub: this.querySelector(".nd-bar-sub"),
      status: this.querySelector('[data-block="status"]'),
      irrigation: this.querySelector('[data-block="irrigation"]'),
      water: this.querySelector('[data-block="water"]'),
      actions: this.querySelector(".nd-actions"),
    };
    this._buildActions();
    this._built = true;
  }

  _buildActions() {
    // Labels are filled in _update() from each button entity's localized
    // friendly_name; "irrigateNow" has a dedicated static string for emphasis.
    this._actionDefs = [
      { role: "btnIrrigate", icon: "mdi:sprinkler", i18n: "irrigateNow", cls: "primary" },
      { role: "btnMark", icon: "mdi:water-check", cls: "" },
      { role: "btnReset", icon: "mdi:lock-reset", cls: "warn" },
    ];
    this._el.actions.innerHTML = "";
    this._actionBtns = {};
    for (const d of this._actionDefs) {
      const btn = document.createElement("button");
      btn.className = `nd-btn ${d.cls}`.trim();
      btn.innerHTML = `<ha-icon icon="${d.icon}"></ha-icon><span class="nd-btn-lbl"></span>`;
      btn.addEventListener("click", () => this._press(d.role));
      this._el.actions.appendChild(btn);
      this._actionBtns[d.role] = btn;
    }
  }

  _press(role) {
    const ents = this._zoneEntities();
    const st = ents[role];
    if (!st) return;
    this._hass.callService("button", "press", { entity_id: st.entity_id });
  }

  _update(ents) {
    const hass = this._hass;
    this._el.title.textContent = this._deviceName();

    // --- deficit vs threshold bar ---
    // The percentage is a ratio of two same-unit values (mm), so it is
    // independent of the user's measurement system. Displayed values go
    // through formatEntityState → unit-system + locale aware.
    const deficit = numState(ents.deficit);
    const threshold = numState(ents.threshold);
    this._el.barLbl.textContent = this._label(ents.deficit, "Deficit");
    if (deficit != null && threshold != null && threshold > 0) {
      const pct = Math.max(0, Math.min(100, (deficit / threshold) * 100));
      this._el.barFill.style.width = `${pct}%`;
      this._el.barFill.style.background = barColor(pct);
      this._el.barVal.textContent = `${pct.toFixed(0)}%`;
      const dStr = fmtState(hass, ents.deficit);
      const tStr = fmtState(hass, ents.threshold);
      this._el.barSub.textContent =
        `${dStr} / ${tStr}` + (deficit >= threshold ? ` — ${t(hass, "due")}` : "");
    } else {
      this._el.barFill.style.width = "0%";
      this._el.barVal.textContent = "—";
      this._el.barSub.textContent = t(hass, "barUnavailable");
    }

    // --- blocks (label = localized friendly_name, value = formatEntityState) ---
    this._el.status.innerHTML = this._rows([
      ["mdi:water-percent-alert", ents.deficit, "Deficit"],
      ["mdi:weather-rainy", ents.rain, "Rain"],
      ["mdi:cup-water", ents.volume, "Volume"],
    ]);

    this._el.irrigation.innerHTML = this._rows([
      ["mdi:clock-outline", ents.lastIrrigated, "Last irrigated"],
      ["mdi:timer-sand", ents.duration, "Duration"],
      ["mdi:history", ents.lastDuration, "Last duration"],
      ["mdi:cog", ents.irrigationMode, "Mode"],
      ["mdi:gauge", ents.flowRate, "Flow rate"],
      ["mdi:target", ents.threshold, "Threshold"],
    ]);

    this._el.water.innerHTML = this._rows([
      ["mdi:water", ents.sessionWater, "Session water"],
      ["mdi:water-plus", ents.yearlyWater, "Yearly water"],
      ["mdi:water-outline", ents.lastVolume, "Last volume"],
      ["mdi:source-branch", ents.lastSource, "Last source"],
    ]);

    // --- action buttons (localized label from button entity friendly_name) ---
    for (const d of this._actionDefs) {
      const btn = this._actionBtns[d.role];
      const st = ents[d.role];
      btn.disabled = !st;
      const lbl = d.i18n ? t(hass, d.i18n) : this._label(st, d.role);
      btn.querySelector(".nd-btn-lbl").textContent = lbl;
    }
  }

  _rows(items) {
    return items
      .map(([icon, st, fallback]) => {
        const v = fmtState(this._hass, st);
        if (v === null) return "";
        const label = this._label(st, fallback);
        return `
        <div class="nd-cell">
          <ha-icon icon="${icon}"></ha-icon>
          <div class="nd-cell-txt">
            <span class="nd-cell-lbl">${escapeHtml(label)}</span>
            <span class="nd-cell-val">${escapeHtml(v)}</span>
          </div>
        </div>`;
      })
      .join("");
  }
}

// ---- helpers ------------------------------------------------------------

function numState(st) {
  if (!st) return null;
  const n = Number(st.state);
  return Number.isFinite(n) ? n : null;
}

/**
 * Display a state value the way Home Assistant would: applies the user's
 * measurement system (unit conversion), locale number formatting and enum
 * state translation. Falls back to raw state + unit if the helper is absent.
 */
function fmtState(hass, st) {
  if (!st || st.state === "unknown" || st.state === "unavailable") return null;
  try {
    if (typeof hass.formatEntityState === "function") {
      return hass.formatEntityState(st);
    }
  } catch (e) {
    /* fall through to raw rendering */
  }
  const unit = st.attributes && st.attributes.unit_of_measurement;
  return unit ? `${st.state} ${unit}` : st.state;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function barColor(pct) {
  if (pct >= 90) return "var(--error-color, #db4437)";
  if (pct >= 60) return "var(--warning-color, #ffa600)";
  return "var(--success-color, #43a047)";
}

function pickFirstZoneDevice(hass) {
  const zones = zoneDevices(hass);
  return zones.length ? zones[0].id : "";
}

function zoneDevices(hass) {
  // Returns [{id, name}] for every NeverDry zone device, identified by the
  // never_dry identifier + the "Irrigation Zone" model (so the hub is excluded).
  const out = [];
  if (!hass || !hass.devices) return out;
  for (const d of Object.values(hass.devices)) {
    const isNeverDry = (d.identifiers || []).some((t) => t[0] === "never_dry");
    if (isNeverDry && d.model === ZONE_MODEL) {
      out.push({ id: d.id, name: d.name_by_user || d.name || d.id });
    }
  }
  out.sort((a, b) => a.name.localeCompare(b.name));
  return out;
}

const CARD_CSS = `
  ha-card { padding: 12px 12px 16px; }
  .nd-head { display:flex; align-items:center; gap:8px; margin-bottom:12px; }
  .nd-head ha-icon { color: var(--primary-color); }
  .nd-title { font-size:1.15rem; font-weight:600; }
  .nd-bar-wrap { margin: 4px 0 14px; }
  .nd-bar-labels { display:flex; justify-content:space-between;
    font-size:.8rem; color:var(--secondary-text-color); margin-bottom:4px; }
  .nd-bar-val { font-weight:600; color:var(--primary-text-color); }
  .nd-bar { height:12px; border-radius:6px; overflow:hidden;
    background: var(--divider-color, #e0e0e0); }
  .nd-bar-fill { height:100%; width:0%; border-radius:6px;
    transition: width .4s ease, background .4s ease; }
  .nd-bar-sub { font-size:.75rem; color:var(--secondary-text-color); margin-top:4px; }
  .nd-grid { display:grid; grid-template-columns:1fr 1fr; gap:8px 14px; }
  .nd-cell { display:flex; align-items:center; gap:8px; min-width:0; }
  .nd-cell ha-icon { color:var(--state-icon-color, var(--paper-item-icon-color));
    flex:0 0 auto; }
  .nd-cell-txt { display:flex; flex-direction:column; min-width:0; }
  .nd-cell-lbl { font-size:.72rem; color:var(--secondary-text-color); }
  .nd-cell-val { font-size:.95rem; font-weight:500;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .nd-sep { height:1px; background:var(--divider-color); margin:12px 0; }
  .nd-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:16px; }
  .nd-btn { display:inline-flex; align-items:center; gap:6px; cursor:pointer;
    border:none; border-radius:18px; padding:8px 14px; font-size:.85rem;
    font-weight:500; color:var(--primary-text-color);
    background: var(--secondary-background-color, #eee); transition:filter .15s; }
  .nd-btn ha-icon { --mdc-icon-size:18px; }
  .nd-btn:hover:not(:disabled) { filter:brightness(.95); }
  .nd-btn:disabled { opacity:.4; cursor:not-allowed; }
  .nd-btn.primary { background: var(--primary-color); color: var(--text-primary-color,#fff); }
  .nd-btn.warn { background: var(--error-color, #db4437); color:#fff; }
`;

// ---- visual config editor ----------------------------------------------

class NeverDryZoneCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (this._config && !this._built) this._render();
  }

  _render() {
    const devices = zoneDevices(this._hass);
    const current = this._config.device_id || "";
    const options = devices
      .map(
        (d) =>
          `<option value="${d.id}" ${d.id === current ? "selected" : ""}>${d.name}</option>`
      )
      .join("");
    this.innerHTML = `
      <div style="padding:8px 4px;display:flex;flex-direction:column;gap:6px">
        <label style="font-size:.85rem;color:var(--secondary-text-color)">${t(this._hass, "zone")}</label>
        <select id="nd-zone"
          style="padding:8px;border-radius:6px;border:1px solid var(--divider-color);
                 background:var(--card-background-color);color:var(--primary-text-color);font-size:.95rem">
          <option value="" ${current ? "" : "selected"} disabled>${t(this._hass, "selectPlaceholder")}</option>
          ${options}
        </select>
        ${
          devices.length === 0
            ? `<span style="font-size:.8rem;color:var(--error-color)">${t(this._hass, "noZones")}</span>`
            : ""
        }
      </div>`;
    this.querySelector("#nd-zone").addEventListener("change", (e) => {
      this._config = { ...this._config, device_id: e.target.value };
      this.dispatchEvent(
        new CustomEvent("config-changed", {
          detail: { config: this._config },
          bubbles: true,
          composed: true,
        })
      );
    });
    this._built = true;
  }
}

// ---- registration -------------------------------------------------------

if (!customElements.get("never-dry-zone-card")) {
  customElements.define("never-dry-zone-card", NeverDryZoneCard);
}
if (!customElements.get("never-dry-zone-card-editor")) {
  customElements.define("never-dry-zone-card-editor", NeverDryZoneCardEditor);
}

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "never-dry-zone-card")) {
  window.customCards.push({
    type: "never-dry-zone-card",
    name: "NeverDry Zone Card",
    description: "All entities of one NeverDry irrigation zone in a clean layout.",
    preview: false,
    documentationURL: "https://github.com/drake69/dryness_index",
  });
}

console.info(
  `%c NeverDry Zone Card %c v${CARD_VERSION} `,
  "color:#fff;background:#43a047;border-radius:3px 0 0 3px;padding:2px 4px",
  "color:#43a047;background:#fff;border-radius:0 3px 3px 0;padding:2px 4px"
);
