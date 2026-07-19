"""Quote calculation engine using trade-specific formulas."""

import logging
from typing import Dict, List, Optional
from dataclasses import dataclass

from config.pricing_configs import TradeConfig, get_trade_config, PricingTier
from core.image_analyzer import ImageAnalysis

logger = logging.getLogger("quote_calculator")


@dataclass
class QuoteLineItem:
    description: str
    quantity: float
    unit: str
    unit_price: float
    total: float


@dataclass
class QuoteResult:
    quote_id: str
    trade: str
    customer_id: str
    line_items: List[QuoteLineItem]
    subtotal: float
    tax_rate: float
    tax_amount: float
    total: float
    breakdown: dict
    valid_days: int
    notes: str
    tier_applied: str


class QuoteCalculator:
    """Generates quotes from image analysis + pricing formulas."""

    def __init__(self):
        self._counter = 0

    def calculate(
        self,
        trade: str,
        customer_id: str,
        analysis: ImageAnalysis,
        addons: Optional[List[str]] = None,
        tax_rate: float = 0.08,
    ) -> QuoteResult:
        """Generate a complete quote from analysis data."""
        self._counter += 1
        quote_id = f"Q-{trade[:3].upper()}-{self._counter:06d}"

        config = get_trade_config(trade)
        addons = addons or []

        tier = self._select_tier(config, analysis)
        tier_applied = tier.name

        formula_result = config.formula(self._analysis_to_dict(analysis), tier)

        line_items = []
        line_items.append(QuoteLineItem(
            description=f"{config.trade_name.title()} - {tier.name} tier base service",
            quantity=1, unit="job",
            unit_price=formula_result["base_service"],
            total=formula_result["base_service"],
        ))
        line_items.append(QuoteLineItem(
            description="Materials and supplies",
            quantity=1, unit="lot",
            unit_price=formula_result["materials"],
            total=formula_result["materials"],
        ))
        line_items.append(QuoteLineItem(
            description=f"Labor ({formula_result['breakdown'].get('labor_hours', tier.labor_hours_estimate):.1f} hrs)",
            quantity=formula_result['breakdown'].get('labor_hours', tier.labor_hours_estimate),
            unit="hour", unit_price=tier.labor_rate_per_hour,
            total=formula_result["labor"],
        ))

        for addon_name in addons:
            if addon_name in config.addons:
                price = config.addons[addon_name]
                line_items.append(QuoteLineItem(
                    description=addon_name.replace("_", " ").title(),
                    quantity=1, unit="each",
                    unit_price=price, total=price,
                ))
                formula_result["subtotal"] += price

        subtotal = sum(item.total for item in line_items)
        subtotal = max(config.min_quote, min(config.max_quote, subtotal))
        tax_amount = subtotal * tax_rate
        total = subtotal + tax_amount

        return QuoteResult(
            quote_id=quote_id, trade=trade, customer_id=customer_id,
            line_items=line_items, subtotal=round(subtotal, 2),
            tax_rate=tax_rate, tax_amount=round(tax_amount, 2),
            total=round(total, 2), breakdown=formula_result["breakdown"],
            valid_days=14,
            notes=f"Quote based on photo analysis. Confidence: {analysis.confidence:.0%}. {analysis.notes}",
            tier_applied=tier_applied,
        )

    def _select_tier(self, config: TradeConfig, analysis: ImageAnalysis) -> PricingTier:
        sqft = analysis.estimated_sqft or 0
        for tier in config.pricing_tiers:
            if tier.min_sqft <= sqft <= tier.max_sqft:
                return tier
        return config.pricing_tiers[-1]

    def _analysis_to_dict(self, analysis: ImageAnalysis) -> dict:
        return {
            "estimated_sqft": analysis.estimated_sqft,
            "complexity": analysis.complexity,
            "damage_level": analysis.damage_level,
            "access_difficulty": analysis.access_difficulty,
            "materials": analysis.materials_visible,
            "roof_sqft": analysis.estimated_sqft,
            "fixture_count": 1,
            "panel_count": 1,
            "outlet_count": 1,
        }

    def format_quote_text(self, quote: QuoteResult) -> str:
        lines = [
            f"📋 QUOTE #{quote.quote_id}",
            f"Trade: {quote.trade.title()}",
            "",
            "LINE ITEMS:",
        ]
        for item in quote.line_items:
            lines.append(f"  • {item.description}")
            lines.append(f"    ${item.total:,.2f}")
        lines.extend([
            "",
            f"Subtotal: ${quote.subtotal:,.2f}",
            f"Tax ({quote.tax_rate:.0%}): ${quote.tax_amount:,.2f}",
            f"{'='*30}",
            f"TOTAL: ${quote.total:,.2f}",
            "",
            f"Valid for {quote.valid_days} days",
            "",
            "Reply ACCEPT to book or QUESTION to talk to an agent.",
        ])
        return "\n".join(lines)
