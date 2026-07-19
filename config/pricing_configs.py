"""Trade-specific pricing formulas and configs."""

from dataclasses import dataclass
from typing import Dict, List, Callable


@dataclass
class PricingTier:
    name: str
    min_sqft: float
    max_sqft: float
    base_rate_per_sqft: float
    material_multiplier: float
    labor_hours_estimate: float
    labor_rate_per_hour: float


@dataclass
class TradeConfig:
    trade_name: str
    description: str
    required_photos: List[str]
    pricing_tiers: List[PricingTier]
    addons: Dict[str, float]
    formula: Callable
    min_quote: float
    max_quote: float


# ─── LANDSCAPING ───────────────────────────────────────────────
def landscaping_formula(analysis: dict, tier: PricingTier) -> dict:
    sqft = analysis.get("estimated_sqft", 500)
    complexity = analysis.get("complexity", "medium")
    access = analysis.get("access_difficulty", "easy")

    base = sqft * tier.base_rate_per_sqft
    complexity_mult = {"low": 0.85, "medium": 1.0, "high": 1.35}
    base *= complexity_mult.get(complexity, 1.0)
    access_mult = {"easy": 1.0, "moderate": 1.15, "hard": 1.4}
    base *= access_mult.get(access, 1.0)
    material_cost = base * tier.material_multiplier
    labor_cost = tier.labor_hours_estimate * tier.labor_rate_per_hour
    labor_cost *= complexity_mult.get(complexity, 1.0)
    subtotal = base + material_cost + labor_cost

    return {
        "base_service": round(base, 2),
        "materials": round(material_cost, 2),
        "labor": round(labor_cost, 2),
        "subtotal": round(subtotal, 2),
        "breakdown": {
            "sqft": sqft,
            "complexity": complexity,
            "access": access,
            "labor_hours": tier.labor_hours_estimate * complexity_mult.get(complexity, 1.0),
        }
    }


LANDSCAPING = TradeConfig(
    trade_name="landscaping",
    description="Lawn care, sod, mulch, planting, hardscaping",
    required_photos=["full_yard_overview", "problem_areas", "access_path"],
    pricing_tiers=[
        PricingTier("small", 0, 1000, 1.50, 0.4, 4, 65),
        PricingTier("medium", 1001, 5000, 1.20, 0.35, 8, 65),
        PricingTier("large", 5001, 20000, 0.90, 0.30, 16, 65),
        PricingTier("commercial", 20001, float('inf'), 0.60, 0.25, 40, 75),
    ],
    addons={
        "tree_removal": 350.0,
        "irrigation_install": 1200.0,
        "lighting_package": 800.0,
        "maintenance_6mo": 450.0,
    },
    formula=landscaping_formula,
    min_quote=150.0,
    max_quote=50000.0,
)


# ─── ROOFING ───────────────────────────────────────────────────
def roofing_formula(analysis: dict, tier: PricingTier) -> dict:
    sqft = analysis.get("roof_sqft", 1500)
    pitch = analysis.get("roof_pitch", "medium")
    layers = analysis.get("shingle_layers", 1)
    damage_level = analysis.get("damage_level", "minor")

    base = sqft * tier.base_rate_per_sqft
    pitch_mult = {"low": 0.9, "medium": 1.0, "steep": 1.25}
    base *= pitch_mult.get(pitch, 1.0)
    base *= (1 + (layers - 1) * 0.3)
    damage_mult = {"minor": 1.0, "moderate": 1.2, "major": 1.5}
    base *= damage_mult.get(damage_level, 1.0)
    material_cost = base * tier.material_multiplier
    labor_cost = tier.labor_hours_estimate * tier.labor_rate_per_hour * pitch_mult.get(pitch, 1.0)
    subtotal = base + material_cost + labor_cost

    return {
        "base_service": round(base, 2),
        "materials": round(material_cost, 2),
        "labor": round(labor_cost, 2),
        "subtotal": round(subtotal, 2),
        "breakdown": {
            "roof_sqft": sqft,
            "pitch": pitch,
            "layers": layers,
            "damage": damage_level,
        }
    }


ROOFING = TradeConfig(
    trade_name="roofing",
    description="Roof repair, replacement, inspection",
    required_photos=["roof_overview", "damage_closeups", "gutter_area", "interior_ceiling"],
    pricing_tiers=[
        PricingTier("repair", 0, 500, 8.0, 0.5, 6, 85),
        PricingTier("partial", 501, 1500, 6.5, 0.45, 16, 85),
        PricingTier("full", 1501, 4000, 5.0, 0.4, 40, 85),
        PricingTier("commercial", 4001, float('inf'), 4.0, 0.35, 80, 95),
    ],
    addons={
        "gutter_replacement": 8.0,
        "skylight_install": 1200.0,
        "ventilation_upgrade": 650.0,
        "warranty_extension": 450.0,
    },
    formula=roofing_formula,
    min_quote=300.0,
    max_quote=100000.0,
)


# ─── PLUMBING ──────────────────────────────────────────────────
def plumbing_formula(analysis: dict, tier: PricingTier) -> dict:
    fixture_count = analysis.get("fixture_count", 1)
    pipe_material = analysis.get("pipe_material", "copper")
    access_type = analysis.get("access_type", "open")
    emergency = analysis.get("emergency", False)

    base = fixture_count * tier.base_rate_per_sqft
    material_rates = {"copper": 1.3, "pex": 0.9, "pvc": 0.7}
    material_cost = base * material_rates.get(pipe_material, 1.0)
    access_mult = {"open": 1.0, "wall": 1.4, "slab": 2.0}
    labor_hours = tier.labor_hours_estimate * fixture_count * access_mult.get(access_type, 1.0)
    labor_cost = labor_hours * tier.labor_rate_per_hour
    subtotal = base + material_cost + labor_cost
    if emergency:
        subtotal *= 1.5

    return {
        "base_service": round(base, 2),
        "materials": round(material_cost, 2),
        "labor": round(labor_cost, 2),
        "subtotal": round(subtotal, 2),
        "breakdown": {
            "fixtures": fixture_count,
            "pipe_material": pipe_material,
            "access": access_type,
            "emergency": emergency,
            "labor_hours": labor_hours,
        }
    }


PLUMBING = TradeConfig(
    trade_name="plumbing",
    description="Repairs, installs, drain cleaning, water heaters",
    required_photos=["problem_area", "under_sink", "water_heater", "main_line"],
    pricing_tiers=[
        PricingTier("basic", 0, 2, 150.0, 0.3, 2, 95),
        PricingTier("standard", 3, 5, 125.0, 0.3, 4, 95),
        PricingTier("complex", 6, 10, 100.0, 0.25, 8, 95),
        PricingTier("commercial", 11, float('inf'), 85.0, 0.25, 12, 110),
    ],
    addons={
        "water_heater": 1200.0,
        "sewer_camera": 250.0,
        "hydro_jetting": 450.0,
        "backflow_install": 650.0,
    },
    formula=plumbing_formula,
    min_quote=100.0,
    max_quote=25000.0,
)


# ─── AUTO BODY / PAINT ─────────────────────────────────────────
def autobody_formula(analysis: dict, tier: PricingTier) -> dict:
    panel_count = analysis.get("panel_count", 1)
    damage_type = analysis.get("damage_type", "dent")
    paint_match = analysis.get("paint_match_required", True)

    base = panel_count * tier.base_rate_per_sqft
    damage_mult = {"dent": 0.8, "scratch": 0.6, "collision": 1.5, "rust": 1.2}
    base *= damage_mult.get(damage_type, 1.0)
    material_cost = base * tier.material_multiplier
    labor_cost = tier.labor_hours_estimate * panel_count * tier.labor_rate_per_hour
    if paint_match:
        material_cost *= 1.15
    subtotal = base + material_cost + labor_cost

    return {
        "base_service": round(base, 2),
        "materials": round(material_cost, 2),
        "labor": round(labor_cost, 2),
        "subtotal": round(subtotal, 2),
        "breakdown": {
            "panels": panel_count,
            "damage_type": damage_type,
            "paint_match": paint_match,
        }
    }


AUTOBODY = TradeConfig(
    trade_name="autobody",
    description="Dent repair, paint, collision, restoration",
    required_photos=["full_vehicle", "damage_closeup", "opposite_angle", "interior"],
    pricing_tiers=[
        PricingTier("minor", 0, 1, 450.0, 0.4, 4, 85),
        PricingTier("moderate", 2, 3, 400.0, 0.4, 6, 85),
        PricingTier("major", 4, 6, 350.0, 0.35, 10, 85),
        PricingTier("restoration", 7, float('inf'), 300.0, 0.35, 16, 95),
    ],
    addons={
        "rental_car": 45.0,
        "ceramic_coating": 1200.0,
        "ppf_install": 1800.0,
        "detail_package": 350.0,
    },
    formula=autobody_formula,
    min_quote=200.0,
    max_quote=30000.0,
)


# ─── ELECTRICAL ────────────────────────────────────────────────
def electrical_formula(analysis: dict, tier: PricingTier) -> dict:
    outlet_count = analysis.get("outlet_count", 1)
    amperage = analysis.get("amperage", 120)
    wiring_type = analysis.get("wiring_type", "romex")
    permit_required = analysis.get("permit_required", False)

    base = outlet_count * tier.base_rate_per_sqft
    amp_mult = {120: 1.0, 240: 1.5, 480: 2.5}
    base *= amp_mult.get(amperage, 1.0)
    wire_mult = {"romex": 1.0, "conduit": 1.3, "bx": 1.2}
    material_cost = base * tier.material_multiplier * wire_mult.get(wiring_type, 1.0)
    labor_hours = tier.labor_hours_estimate * outlet_count
    labor_cost = labor_hours * tier.labor_rate_per_hour
    subtotal = base + material_cost + labor_cost
    if permit_required:
        subtotal += 250

    return {
        "base_service": round(base, 2),
        "materials": round(material_cost, 2),
        "labor": round(labor_cost, 2),
        "subtotal": round(subtotal, 2),
        "breakdown": {
            "outlets": outlet_count,
            "amperage": amperage,
            "wiring": wiring_type,
            "permit": permit_required,
            "labor_hours": labor_hours,
        }
    }


ELECTRICAL = TradeConfig(
    trade_name="electrical",
    description="Repairs, panel upgrades, EV charging, lighting",
    required_photos=["panel_area", "work_area", "existing_wiring", "exterior_access"],
    pricing_tiers=[
        PricingTier("basic", 0, 3, 180.0, 0.35, 2, 110),
        PricingTier("standard", 4, 10, 150.0, 0.35, 3, 110),
        PricingTier("panel_upgrade", 11, 20, 120.0, 0.3, 8, 110),
        PricingTier("commercial", 21, float('inf'), 100.0, 0.3, 12, 125),
    ],
    addons={
        "ev_charger_install": 850.0,
        "smart_panel": 2200.0,
        "surge_protector": 450.0,
        "inspection_cert": 150.0,
    },
    formula=electrical_formula,
    min_quote=150.0,
    max_quote=20000.0,
)


# ─── REGISTRY ──────────────────────────────────────────────────
TRADE_CONFIGS: Dict[str, TradeConfig] = {
    "landscaping": LANDSCAPING,
    "roofing": ROOFING,
    "plumbing": PLUMBING,
    "autobody": AUTOBODY,
    "electrical": ELECTRICAL,
}


def get_trade_config(trade: str) -> TradeConfig:
    trade = trade.lower().strip()
    if trade not in TRADE_CONFIGS:
        available = ", ".join(TRADE_CONFIGS.keys())
        raise ValueError(f"Unknown trade '{trade}'. Available: {available}")
    return TRADE_CONFIGS[trade]


def list_trades() -> List[str]:
    return list(TRADE_CONFIGS.keys())
