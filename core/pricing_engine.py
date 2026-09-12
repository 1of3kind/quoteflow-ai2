"""E-ZFlow pricing engine — the single authoritative quote calculator (GATE 3).

Formula (all percentages are configurable per organization):

    labor_cost      = Σ (workers × hours × hourly_rate)          per skill line
    material_cost   = Σ (unit_cost × quantity)                   per material line
    overhead        = (labor_cost + material_cost) × overhead_pct
    total_cost      = labor_cost + material_cost + overhead
    profit          = total_cost × margin_pct / (1 − margin_pct)
    recommended     = total_cost / (1 − margin_pct)   ← margin measured on the
                                                        final price, so
                                                        margin == margin_pct
    tax             = recommended × tax_rate (optional)
    total_with_tax  = recommended + tax

Every figure is rounded to cents with Decimal ROUND_HALF_UP, and the engine
reconciles its own arithmetic (Σ components == recommended within one cent)
before returning. compute() is a pure function: the same input always yields
the same output, so a stored snapshot fully reproduces any historical quote.
"""

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import List, Optional

CENT = Decimal("0.01")


class PricingError(ValueError):
    """Invalid pricing input — the quote must not be produced."""


def _d(value, field_name: str, *, minimum: Decimal = Decimal("0")) -> Decimal:
    """Coerce to Decimal, rejecting bools, junk, negatives and NaN/Inf."""
    if isinstance(value, bool) or value is None:
        raise PricingError(f"{field_name} is required and must be a number")
    try:
        d = Decimal(str(value))
    except Exception:
        raise PricingError(f"{field_name} must be a number")
    if not d.is_finite():
        raise PricingError(f"{field_name} must be finite")
    if d < minimum:
        raise PricingError(f"{field_name} must be >= {minimum}")
    return d


def _cents(value: Decimal) -> float:
    return float(value.quantize(CENT, rounding=ROUND_HALF_UP))


@dataclass
class LaborLine:
    skill_name: str
    skill_level: str = "standard"        # junior | standard | master
    workers: int = 1
    hours: float = 0.0
    hourly_rate: float = 0.0

    def cost(self) -> Decimal:
        return _d(self.workers, "workers", minimum=Decimal("1")) * \
               _d(self.hours, "hours") * _d(self.hourly_rate, "hourly_rate")

    def snapshot(self) -> dict:
        return {
            "skill_name": self.skill_name,
            "skill_level": self.skill_level,
            "workers": self.workers,
            "hours": self.hours,
            "hourly_rate": self.hourly_rate,
        }


@dataclass
class MaterialLine:
    name: str
    unit_cost: float = 0.0
    quantity: float = 1.0
    unit: str = "each"

    def cost(self) -> Decimal:
        return _d(self.unit_cost, "unit_cost") * _d(self.quantity, "quantity")

    def snapshot(self) -> dict:
        return {"name": self.name, "unit_cost": self.unit_cost,
                "quantity": self.quantity, "unit": self.unit}


@dataclass
class PricingInput:
    labor_lines: List[LaborLine] = field(default_factory=list)
    material_lines: List[MaterialLine] = field(default_factory=list)
    overhead_pct: float = 0.15
    profit_margin_pct: float = 0.20
    tax_rate: float = 0.0
    title: str = ""

    def snapshot(self) -> dict:
        return {
            "engine_version": ENGINE_VERSION,
            "labor_lines": [l.snapshot() for l in self.labor_lines],
            "material_lines": [m.snapshot() for m in self.material_lines],
            "overhead_pct": self.overhead_pct,
            "profit_margin_pct": self.profit_margin_pct,
            "tax_rate": self.tax_rate,
            "title": self.title,
        }

    @classmethod
    def from_snapshot(cls, snap: dict) -> "PricingInput":
        if not isinstance(snap, dict) or "labor_lines" not in snap:
            raise PricingError("Malformed pricing snapshot")
        return cls(
            labor_lines=[LaborLine(**l) for l in snap["labor_lines"]],
            material_lines=[MaterialLine(**m) for m in snap.get("material_lines", [])],
            overhead_pct=snap.get("overhead_pct", 0.15),
            profit_margin_pct=snap.get("profit_margin_pct", 0.20),
            tax_rate=snap.get("tax_rate", 0.0),
            title=snap.get("title", ""),
        )


@dataclass
class PricingResult:
    labor_cost: float
    material_cost: float
    overhead: float
    profit: float
    recommended_price: float
    margin: float            # profit / recommended_price
    total_cost: float        # labor + materials + overhead
    tax_amount: float
    total_with_tax: float
    labor_lines: List[dict] = field(default_factory=list)
    material_lines: List[dict] = field(default_factory=list)
    explanation: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "labor_cost": round(self.labor_cost, 2),
            "material_cost": round(self.material_cost, 2),
            "overhead": round(self.overhead, 2),
            "profit": round(self.profit, 2),
            "recommended_price": round(self.recommended_price, 2),
            "margin": round(self.margin, 4),
            "total_cost": round(self.total_cost, 2),
            "tax_amount": round(self.tax_amount, 2),
            "total_with_tax": round(self.total_with_tax, 2),
            "labor_lines": self.labor_lines,
            "material_lines": self.material_lines,
            "explanation": self.explanation,
            "narrative": self.narrative,
        }

    def explanation_text(self) -> str:
        return "\n".join(self.explanation)

    @property
    def narrative(self) -> str:
        """One-paragraph plain-English answer to 'why this price?'."""
        labor_bits = "; ".join(
            f"{l['workers']} × {l['skill_name']} at "
            f"${l['hourly_rate']:,.2f}/h for {l['hours']:g} h = ${l['cost']:,.2f}"
            for l in self.labor_lines
        )
        parts = [f"Your recommended price is ${self.recommended_price:,.2f}"]
        reasons = []
        if labor_bits:
            reasons.append(f"labor is ${self.labor_cost:,.2f} ({labor_bits})")
        if self.material_lines:
            reasons.append(
                f"materials are ${self.material_cost:,.2f} "
                f"across {len(self.material_lines)} line item(s)")
        if self.overhead:
            ov_pct = (self.overhead / self.total_cost * 100) if self.total_cost else 0
            reasons.append(f"overhead adds ${self.overhead:,.2f} ({ov_pct:.0f}% of direct cost)")
        if self.profit:
            reasons.append(
                f"and your target margin is {self.margin:.0%}, "
                f"which adds ${self.profit:,.2f} profit")
        text = f"{parts[0]} because " + ", ".join(reasons) + "."
        if self.tax_amount:
            text += (f" With {self.tax_amount:,.2f} tax the customer total is "
                     f"${self.total_with_tax:,.2f}.")
        return text


ENGINE_VERSION = "1.0.0"

_MAX_PCT = Decimal("0.90")  # overhead and margin must each stay under 90%


def compute(inp: PricingInput) -> PricingResult:
    """The one authoritative calculation. Pure, validated, reproducible."""
    if not inp.labor_lines and not inp.material_lines:
        raise PricingError("A quote needs at least one labor or material line")

    overhead_pct = _d(inp.overhead_pct, "overhead_pct")
    margin_pct = _d(inp.profit_margin_pct, "profit_margin_pct")
    tax_rate = _d(inp.tax_rate, "tax_rate")
    if overhead_pct > _MAX_PCT:
        raise PricingError("overhead_pct must be <= 0.90")
    if margin_pct >= _MAX_PCT:
        raise PricingError("profit_margin_pct must be < 0.90")
    if overhead_pct + margin_pct >= Decimal("0.95"):
        raise PricingError("overhead_pct + profit_margin_pct must be < 0.95")

    # ── Labor ──
    labor_line_dicts: List[dict] = []
    labor_total = Decimal("0")
    for i, line in enumerate(inp.labor_lines, start=1):
        if not str(line.skill_name or "").strip():
            raise PricingError(f"Labor line {i}: skill_name is required")
        cost = line.cost()  # validates workers/hours/rate
        labor_total += cost
        labor_line_dicts.append({
            "skill_name": line.skill_name,
            "skill_level": line.skill_level,
            "workers": line.workers,
            "hours": float(_d(line.hours, "hours")),
            "hourly_rate": _cents(_d(line.hourly_rate, "hourly_rate")),
            "cost": _cents(cost),
        })

    # ── Materials ──
    material_line_dicts: List[dict] = []
    material_total = Decimal("0")
    for i, line in enumerate(inp.material_lines, start=1):
        if not str(line.name or "").strip():
            raise PricingError(f"Material line {i}: name is required")
        cost = line.cost()
        material_total += cost
        material_line_dicts.append({
            "name": line.name,
            "unit_cost": _cents(_d(line.unit_cost, "unit_cost")),
            "quantity": float(_d(line.quantity, "quantity")),
            "unit": line.unit,
            "cost": _cents(cost),
        })

    direct_cost = labor_total + material_total
    overhead = direct_cost * overhead_pct
    total_cost = direct_cost + overhead

    margin_divisor = Decimal("1") - margin_pct
    recommended = total_cost / margin_divisor
    profit = recommended - total_cost
    tax = recommended * tax_rate
    total_with_tax = recommended + tax

    # ── Reconciliation guard: components must re-add to the price ──
    if abs((total_cost + profit) - recommended) > Decimal("0.01"):
        raise PricingError("Internal arithmetic mismatch")

    def pct(v: Decimal) -> str:
        return f"{(v * 100).quantize(Decimal('0.1'))}%"

    explanation = []
    explanation.append(f"Skill lines: {len(inp.labor_lines)} | "
                       f"Materials: {len(inp.material_lines)}")
    for l in labor_line_dicts:
        explanation.append(
            f"Labor — {l['skill_name']} ({l['skill_level']}): "
            f"{l['workers']} worker(s) × {l['hours']:g} h × ${l['hourly_rate']:,.2f}/h = ${l['cost']:,.2f}"
        )
    for m in material_line_dicts:
        explanation.append(
            f"Material — {m['name']}: {m['quantity']:g} {m['unit']} × ${m['unit_cost']:,.2f} = ${m['cost']:,.2f}"
        )
    explanation.append(f"Labor:        ${_cents(labor_total):>10,.2f}")
    explanation.append(f"Materials:    ${_cents(material_total):>10,.2f}")
    explanation.append(f"Overhead ({pct(overhead_pct)}): ${_cents(overhead):>10,.2f}")
    explanation.append(f"Target profit ({pct(margin_pct)} margin): ${_cents(profit):>10,.2f}")
    explanation.append("────────────────────────────────")
    explanation.append(f"Recommended price: ${_cents(recommended):,.2f}")
    if tax_rate > 0:
        explanation.append(f"Tax ({pct(tax_rate)}): ${_cents(tax):,.2f}")
        explanation.append(f"Total with tax:    ${_cents(total_with_tax):,.2f}")

    return PricingResult(
        labor_cost=_cents(labor_total),
        material_cost=_cents(material_total),
        overhead=_cents(overhead),
        profit=_cents(profit),
        recommended_price=_cents(recommended),
        margin=float((profit / recommended).quantize(Decimal("0.0001"))) if recommended > 0 else 0.0,
        total_cost=_cents(total_cost),
        tax_amount=_cents(tax),
        total_with_tax=_cents(total_with_tax),
        labor_lines=labor_line_dicts,
        material_lines=material_line_dicts,
        explanation=explanation,
    )


def recompute(snapshot: dict) -> PricingResult:
    """Reproduce a stored quote's numbers from its snapshot."""
    return compute(PricingInput.from_snapshot(snapshot))
