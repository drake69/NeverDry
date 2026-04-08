"""Constants for the NeverDry integration."""

DOMAIN = "never_dry"

# ── Sensor inputs ─────────────────────────────────────────
CONF_TEMP_SENSOR = "temperature_sensor"
CONF_RAIN_SENSOR = "rain_sensor"
CONF_VWC_SENSOR = "vwc_sensor"

# ── ET model parameters ──────────────────────────────────
CONF_ALPHA = "alpha"
CONF_T_BASE = "t_base"
CONF_D_MAX = "d_max"
CONF_FIELD_CAPACITY = "field_capacity"
CONF_ROOT_DEPTH = "root_depth_m"

# ── Zone parameters ──────────────────────────────────────
CONF_ZONES = "zones"
CONF_ZONE_NAME = "name"
CONF_ZONE_VALVE = "valve"
CONF_ZONE_AREA = "area_m2"
CONF_ZONE_EFFICIENCY = "efficiency"
CONF_ZONE_FLOW_RATE = "flow_rate_lpm"
CONF_ZONE_THRESHOLD = "threshold"
CONF_ZONE_SYSTEM_TYPE = "system_type"
CONF_ZONE_PLANT_FAMILY = "plant_family"
CONF_ZONE_KC = "kc"

# ── Controller parameters ────────────────────────────────
CONF_INTER_ZONE_DELAY = "inter_zone_delay"

# ── Irrigation system types ──────────────────────────────
SYSTEM_TYPE_DRIP = "drip"
SYSTEM_TYPE_MICRO_SPRINKLER = "micro_sprinkler"
SYSTEM_TYPE_SPRINKLER = "sprinkler"
SYSTEM_TYPE_MANUAL = "manual"

SYSTEM_TYPES = {
    SYSTEM_TYPE_DRIP: {"label": "Drip irrigation", "default_efficiency": 0.92},
    SYSTEM_TYPE_MICRO_SPRINKLER: {"label": "Micro-sprinklers", "default_efficiency": 0.80},
    SYSTEM_TYPE_SPRINKLER: {"label": "Pop-up sprinklers", "default_efficiency": 0.68},
    SYSTEM_TYPE_MANUAL: {"label": "Manual / hose", "default_efficiency": 0.55},
}

# ── Plant families (seasonal Kc profiles) ───────────────
# Tuple order: (winter, spring, summer, autumn) — northern hemisphere
# Anchor days: 15 (mid-Jan), 105 (mid-Apr), 196 (mid-Jul), 288 (mid-Oct)
# Southern hemisphere: day_of_year shifted by 182 days automatically.
PLANT_FAMILIES = {
    "lawn":              {"label": "Lawn / Turf grass",       "kc_seasonal": (0.45, 0.85, 1.00, 0.70)},
    "vegetables":        {"label": "Vegetables (seasonal)",   "kc_seasonal": (0.30, 0.70, 1.10, 0.50)},
    "fruit_trees":       {"label": "Fruit trees (deciduous)", "kc_seasonal": (0.35, 0.70, 0.95, 0.55)},
    "ornamental_shrubs": {"label": "Ornamental shrubs",       "kc_seasonal": (0.40, 0.65, 0.80, 0.55)},
    "herbs":             {"label": "Herbs (Mediterranean)",   "kc_seasonal": (0.30, 0.55, 0.70, 0.40)},
    "citrus":            {"label": "Citrus / Evergreen fruit","kc_seasonal": (0.60, 0.65, 0.70, 0.65)},
    "roses":             {"label": "Roses",                   "kc_seasonal": (0.35, 0.75, 0.95, 0.55)},
    "succulents":        {"label": "Succulents / Cacti",      "kc_seasonal": (0.15, 0.25, 0.35, 0.20)},
    "native_ground_cover": {"label": "Native ground cover",   "kc_seasonal": (0.25, 0.45, 0.55, 0.35)},
    "mixed_garden":      {"label": "Mixed garden (default)",  "kc_seasonal": (0.40, 0.70, 0.90, 0.55)},
}

KC_ANCHOR_DAYS = (15, 105, 196, 288)

# ── Services ─────────────────────────────────────────────
SERVICE_RESET = "reset"
SERVICE_IRRIGATE_ZONE = "irrigate_zone"
SERVICE_IRRIGATE_ALL = "irrigate_all"
SERVICE_STOP = "stop"

ATTR_ZONE_NAME = "zone_name"

# ── Defaults ─────────────────────────────────────────────
DEFAULT_ALPHA = 0.22
DEFAULT_T_BASE = 9.0
DEFAULT_D_MAX = 100.0
DEFAULT_EFFICIENCY = 0.85
DEFAULT_THRESHOLD = 20.0
DEFAULT_FIELD_CAPACITY = 0.30
DEFAULT_ROOT_DEPTH = 0.30
DEFAULT_INTER_ZONE_DELAY = 30
DEFAULT_KC = 1.0
