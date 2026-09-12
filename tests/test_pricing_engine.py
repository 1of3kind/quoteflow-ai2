"""GATE 4 — Quote accuracy & safety.

Full test matrix for the authoritative pricing engine: labor (hours, skill
levels, multiple workers), materials (none/one/many, quantity and price
changes), overhead (0/normal/high), profit margins (0/low/normal/high),
edge cases (negative, missing, zero, huge, decimals, rounding), and
regression goldens that must never move unless the formula intentionally
changes (bump ENGINE_VERSION).
"""

import math

import pytest

from core.pricing_engine import (
    PricingInput, PricingResult, LaborLine, MaterialLine,
    compute, recompute, PricingError, ENGINE_VERSION, CENT,
)


def make_input(**kw) -> PricingInput:
    defaults = dict(
        labor_lines=[LaborLine(skill_name="Tradesperson", skill_level="standard",
                               workers=1, hours=8.0, hourly_rate=50.0)],
        material_lines=[MaterialLine(name="Lumber", unit_cost=100.0, quantity=2)],
        overhead_pct=0.15,
        profit_margin_pct=0.20,
        tax_rate=0.08,
    )
    defaults.update(kw)
    return PricingInput(**defaults)


# ─── Canonical golden (REGRESSION) ──────────────────────────────────────────

CANONICAL = dict(
    labor=2 * 8 * 50.0,          # 800
    materials=10 * 25.50,        # 255
    overhead=0.15,               # 158.25
    margin=0.20,
)


class TestRegressionGolden:
    """Lock the canonical calculation. If any of these fail after an
    intentional formula change, bump ENGINE_VERSION and re-baseline."""

    def test_canonical_numbers(self):
        result = compute(make_input(
            labor_lines=[LaborLine(skill_name="Tradesperson", workers=2, hours=8.0, hourly_rate=50.0)],
            material_lines=[MaterialLine(name="Shingles", unit_cost=25.50, quantity=10)],
        ))
        assert result.labor_cost == 800.00
        assert result.material_cost == 255.00
        assert result.overhead == 158.25
        assert result.total_cost == 1213.25
        assert result.recommended_price == 1516.56      # 1213.25 / 0.8
        assert result.profit == 303.31
        assert result.margin == pytest.approx(0.2, abs=1e-4)
        assert result.tax_amount == 121.33              # 8% of 1516.5625 → 121.325 → HALF_UP
        assert result.total_with_tax == 1637.89

    def test_snapshot_replay_is_bit_identical(self):
        inp = make_input(
            labor_lines=[
                LaborLine(skill_name="Junior", skill_level="junior", workers=1, hours=3.5, hourly_rate=25.0),
                LaborLine(skill_name="Master", skill_level="master", workers=2, hours=6.25, hourly_rate=68.0),
            ],
            material_lines=[
                MaterialLine(name="Pipe", unit_cost=12.345, quantity=7.5),
                MaterialLine(name="Valve", unit_cost=89.99, quantity=3),
            ],
        )
        first = compute(inp)
        replayed = recompute(inp.snapshot())
        assert replayed.as_dict() == first.as_dict()

    def test_engine_version_recorded_in_snapshot(self):
        assert make_input().snapshot()["engine_version"] == ENGINE_VERSION


# ─── Labor matrix ───────────────────────────────────────────────────────────

class TestLabor:
    def test_one_hour(self):
        r = compute(make_input(labor_lines=[LaborLine(skill_name="X", hours=1, hourly_rate=50)],
                               material_lines=[]))
        assert r.labor_cost == 50.0

    def test_eight_hours(self):
        r = compute(make_input(material_lines=[]))
        assert r.labor_cost == 400.0

    def test_forty_hours(self):
        r = compute(make_input(labor_lines=[LaborLine(skill_name="X", hours=40, hourly_rate=50)],
                               material_lines=[]))
        assert r.labor_cost == 2000.0

    def test_skill_level_rates_flow_through(self):
        rates = {"junior": 25.0, "standard": 45.0, "master": 68.0}
        results = {lvl: compute(make_input(
            labor_lines=[LaborLine(skill_name=lvl, skill_level=lvl, hours=8, hourly_rate=rate)],
            material_lines=[])).labor_cost
            for lvl, rate in rates.items()}
        assert results == {"junior": 200.0, "standard": 360.0, "master": 544.0}

    def test_multiple_workers_multiply(self):
        r = compute(make_input(
            labor_lines=[
                LaborLine(skill_name="A", workers=2, hours=8, hourly_rate=50),
                LaborLine(skill_name="B", workers=1, hours=4, hourly_rate=60),
            ], material_lines=[]))
        assert r.labor_cost == 2 * 8 * 50 + 1 * 4 * 60  # 800 + 240

    def test_zero_hours_allowed_but_costs_zero(self):
        r = compute(make_input(
            labor_lines=[LaborLine(skill_name="X", hours=0, hourly_rate=50)],
            material_lines=[MaterialLine(name="M", unit_cost=10, quantity=1)]))
        assert r.labor_cost == 0.0
        assert r.recommended_price > 0.0  # still priced from materials


# ─── Materials matrix ───────────────────────────────────────────────────────

class TestMaterials:
    def test_no_materials(self):
        r = compute(make_input(material_lines=[]))
        assert r.material_cost == 0.0

    def test_one_material(self):
        r = compute(make_input(material_lines=[MaterialLine(name="M", unit_cost=30, quantity=1)]))
        assert r.material_cost == 30.0

    def test_many_materials_sum(self):
        r = compute(make_input(material_lines=[
            MaterialLine(name="A", unit_cost=10, quantity=1),
            MaterialLine(name="B", unit_cost=20, quantity=2),
            MaterialLine(name="C", unit_cost=30, quantity=3),
        ]))
        assert r.material_cost == 10 + 40 + 90

    def test_quantity_change_scales(self):
        base = compute(make_input(material_lines=[MaterialLine(name="M", unit_cost=10, quantity=1)]))
        tripled = compute(make_input(material_lines=[MaterialLine(name="M", unit_cost=10, quantity=3)]))
        assert tripled.material_cost == base.material_cost * 3

    def test_price_change_scales(self):
        base = compute(make_input(material_lines=[MaterialLine(name="M", unit_cost=10, quantity=5)]))
        bumped = compute(make_input(material_lines=[MaterialLine(name="M", unit_cost=12, quantity=5)]))
        assert bumped.material_cost == base.material_cost * 1.2


# ─── Overhead matrix ────────────────────────────────────────────────────────

class TestOverhead:
    def test_zero_overhead(self):
        r = compute(make_input(overhead_pct=0.0))
        assert r.overhead == 0.0
        # total_cost == labor + materials exactly
        assert r.total_cost == r.labor_cost + r.material_cost

    def test_normal_overhead(self):
        r = compute(make_input())
        direct = r.labor_cost + r.material_cost
        assert r.overhead == round(direct * 0.15, 2)

    def test_high_overhead(self):
        r = compute(make_input(overhead_pct=0.60))
        direct = r.labor_cost + r.material_cost
        assert r.overhead == round(direct * 0.60, 2)

    def test_overhead_above_cap_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(overhead_pct=0.95))


# ─── Profit margin matrix ───────────────────────────────────────────────────

class TestProfitMargin:
    def test_zero_margin_breaks_even_on_price(self):
        r = compute(make_input(profit_margin_pct=0.0))
        assert r.profit == 0.0
        assert r.recommended_price == r.total_cost
        assert r.margin == 0.0

    def test_low_margin(self):
        r = compute(make_input(profit_margin_pct=0.05))
        assert r.margin == pytest.approx(0.05, abs=1e-4)

    def test_normal_margin(self):
        r = compute(make_input(profit_margin_pct=0.20))
        assert r.margin == pytest.approx(0.20, abs=1e-4)

    def test_high_margin(self):
        r = compute(make_input(profit_margin_pct=0.40))
        assert r.margin == pytest.approx(0.40, abs=1e-4)

    def test_margin_measured_on_price_not_cost(self):
        # 50% markup-on-cost would double the price; margin-on-price means
        # recommended = total_cost / (1 - 0.5) = 2 × cost.
        r = compute(make_input(profit_margin_pct=0.50))
        assert r.recommended_price == pytest.approx(r.total_cost * 2, abs=0.02)


# ─── Edge cases ─────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_negative_hourly_rate_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(labor_lines=[LaborLine(skill_name="X", hours=1, hourly_rate=-5)]))

    def test_negative_quantity_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(material_lines=[MaterialLine(name="M", unit_cost=10, quantity=-1)]))

    def test_negative_overhead_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(overhead_pct=-0.1))

    def test_negative_margin_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(profit_margin_pct=-0.2))

    def test_missing_skill_name_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(labor_lines=[LaborLine(skill_name="", hours=1, hourly_rate=50)]))

    def test_missing_material_name_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(material_lines=[MaterialLine(name="  ", unit_cost=10)]))

    def test_missing_values_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(labor_lines=[LaborLine(skill_name="X", hours=None, hourly_rate=50)]))
        with pytest.raises(PricingError):
            compute(make_input(overhead_pct=None))

    def test_no_lines_at_all_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(labor_lines=[], material_lines=[]))

    def test_zero_material_cost_allowed(self):
        r = compute(make_input(material_lines=[MaterialLine(name="Loaner", unit_cost=0, quantity=2)]))
        assert r.material_cost == 0.0

    def test_bool_inputs_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(overhead_pct=True))

    def test_garbage_input_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(overhead_pct="expensive"))

    def test_nan_rejected(self):
        with pytest.raises(PricingError):
            compute(make_input(overhead_pct=float("nan")))

    def test_extremely_large_numbers(self):
        r = compute(make_input(
            labor_lines=[LaborLine(skill_name="X", hours=1000, hourly_rate=10000)],
            material_lines=[MaterialLine(name="Bridge steel", unit_cost=250000, quantity=1000)],
            profit_margin_pct=0.10, overhead_pct=0.10, tax_rate=0))
        assert math.isfinite(r.recommended_price)
        # (1e7 + 2.5e8) × 1.1 / 0.9 = 317_777_777.78
        assert r.recommended_price == pytest.approx(317_777_777.78, abs=0.02)

    def test_decimal_values(self):
        r = compute(make_input(
            labor_lines=[LaborLine(skill_name="X", hours=7.5, hourly_rate=42.37)],
            material_lines=[MaterialLine(name="M", unit_cost=19.99, quantity=3.33)]))
        assert r.labor_cost == 317.78          # 7.5 × 42.37 = 317.775 → HALF_UP
        assert r.material_cost == 66.57        # 19.99 × 3.33 = 66.5667 → 66.57

    def test_currency_rounding_half_up(self):
        # 0.125 must round to 0.13 (half-up), never banker's 0.12
        r = compute(make_input(material_lines=[MaterialLine(name="M", unit_cost=0.125, quantity=1)],
                               labor_lines=[]))
        assert r.material_cost == 0.13

    def test_margin_sum_reconciliation_holds(self):
        """The engine's own guard: components must re-add to the price."""
        for margin in (0.0, 0.05, 0.2, 0.35, 0.6):
            r = compute(make_input(profit_margin_pct=margin))
            assert r.total_cost + r.profit == pytest.approx(r.recommended_price, abs=0.016)


# ─── Explanation (defensibility) ────────────────────────────────────────────

class TestExplanation:
    def test_explanation_contains_all_components(self):
        r = compute(make_input())
        text = r.explanation_text()
        for needle in ("Labor", "Materials", "Overhead", "Target profit",
                       "Recommended price"):
            assert needle in text

    def test_explanation_shows_numbers(self):
        r = compute(make_input(
            labor_lines=[LaborLine(skill_name="Tradesperson", workers=2, hours=8.0, hourly_rate=50.0)],
            material_lines=[MaterialLine(name="Shingles", unit_cost=25.50, quantity=10)],
        ))
        assert "$800.00" in r.explanation_text()
        assert "$1,516.56" in r.explanation_text()
