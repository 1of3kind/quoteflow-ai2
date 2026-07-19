"""Tests for QuoteFlow AI."""

import pytest
from datetime import datetime

from config.pricing_configs import get_trade_config, list_trades
from core.image_analyzer import MockAnalyzer
from core.quote_calculator import QuoteCalculator
from core.conversation_manager import ConversationManager, ConversationStage, get_response


class TestPricingConfigs:
    def test_all_trades_load(self):
        trades = list_trades()
        assert len(trades) == 5
        for trade in trades:
            config = get_trade_config(trade)
            assert config.trade_name == trade
            assert len(config.pricing_tiers) > 0
            assert config.min_quote > 0

    def test_landscaping_formula(self):
        config = get_trade_config("landscaping")
        tier = config.pricing_tiers[1]
        result = config.formula({"estimated_sqft": 2500, "complexity": "medium", "access": "easy"}, tier)
        assert result["subtotal"] > 0
        assert "labor" in result

    def test_roofing_formula(self):
        config = get_trade_config("roofing")
        tier = config.pricing_tiers[2]
        result = config.formula({"roof_sqft": 1800, "pitch": "medium", "layers": 1, "damage": "moderate"}, tier)
        assert result["subtotal"] > 0


class TestImageAnalyzer:
    @pytest.mark.asyncio
    async def test_mock_landscaping(self):
        analyzer = MockAnalyzer()
        analysis = await analyzer.analyze_image("fake.jpg", "landscaping")
        assert analysis.trade == "landscaping"
        assert analysis.estimated_sqft == 2500
        assert analysis.complexity == "medium"

    @pytest.mark.asyncio
    async def test_mock_roofing(self):
        analyzer = MockAnalyzer()
        analysis = await analyzer.analyze_image("fake.jpg", "roofing")
        assert analysis.damage_level == "moderate"


class TestQuoteCalculator:
    @pytest.mark.asyncio
    async def test_landscaping_quote(self):
        analyzer = MockAnalyzer()
        calc = QuoteCalculator()
        analysis = await analyzer.analyze_image("fake.jpg", "landscaping")
        quote = calc.calculate("landscaping", "cust_001", analysis)
        assert quote.total > quote.subtotal
        assert quote.quote_id.startswith("Q-LAN")
        assert len(quote.line_items) >= 3

    @pytest.mark.asyncio
    async def test_with_addons(self):
        analyzer = MockAnalyzer()
        calc = QuoteCalculator()
        analysis = await analyzer.analyze_image("fake.jpg", "landscaping")
        quote = calc.calculate("landscaping", "cust_002", analysis, addons=["tree_removal"])
        item_names = [i.description for i in quote.line_items]
        assert any("Tree Removal" in name for name in item_names)


class TestConversationManager:
    def test_create_conversation(self):
        mgr = ConversationManager()
        conv = mgr.get_or_create("cust_001", phone="+1234567890")
        assert conv.stage == ConversationStage.GREETING
        assert conv.customer_phone == "+1234567890"

    def test_stage_transitions(self):
        mgr = ConversationManager()
        mgr.get_or_create("cust_001")
        mgr.update_stage("cust_001", ConversationStage.TRADE_SELECT)
        conv = mgr.get("cust_001")
        assert conv.stage == ConversationStage.TRADE_SELECT

    def test_conversion_rate(self):
        mgr = ConversationManager()
        mgr.get_or_create("c1")
        mgr.get_or_create("c2")
        mgr.update_stage("c1", ConversationStage.BOOKED)
        assert mgr.get_conversion_rate() == 50.0


class TestResponseTemplates:
    def test_greeting_response(self):
        resp = get_response("greeting")
        assert "QuoteFlow" in resp

    def test_analyzing_response(self):
        resp = get_response("analyzing")
        assert "analyzing" in resp




class TestMaterialsOrdering:
    @pytest.mark.asyncio
    async def test_store_finder_mock(self):
        from materials.store_inventory import StoreFinder
        finder = StoreFinder(zip_code="90210", use_mock=True)
        results = await finder.find_materials("landscaping", ["mulch", "sod"])
        assert "mulch" in results
        assert len(results["mulch"]) > 0
        assert results["mulch"][0].price > 0

    @pytest.mark.asyncio
    async def test_materials_order_creation(self):
        from materials.order_manager import get_order_manager
        from core.quote_calculator import QuoteCalculator
        from core.image_analyzer import MockAnalyzer

        analyzer = MockAnalyzer()
        calc = QuoteCalculator()

        analysis = await analyzer.analyze_image("fake.jpg", "landscaping")
        quote = calc.calculate("landscaping", "cust_001", analysis)

        manager = get_order_manager()
        order = await manager.create_materials_order(quote, "landscaping", "90210")

        assert order is not None
        assert order.order_id.startswith("ORD-")
        assert len(order.items) > 0
        assert order.total > 0

    def test_appointment_with_materials(self):
        from materials.order_manager import get_order_manager
        from datetime import datetime, timedelta

        manager = get_order_manager()

        # Create a fake materials order first
        from materials.store_inventory import MaterialOrder, StoreItem
        order = MaterialOrder(
            order_id="ORD-TEST-001",
            quote_id="Q-TEST-001",
            trade="landscaping",
            items=[],
            subtotal=100.0,
            tax=8.0,
            total=108.0,
            store_name="Home Depot",
            store_address="123 Main St",
            pickup_time="2026-07-20T07:00:00",
            status="confirmed",
        )
        manager.orders[order.order_id] = order
        manager.quote_to_order["Q-TEST-001"] = order.order_id

        apt = manager.schedule_appointment_with_pickup(
            customer_id="cust_001",
            customer_name="Test Customer",
            customer_phone="+1234567890",
            trade="landscaping",
            quote_id="Q-TEST-001",
            preferred_date=datetime.now() + timedelta(days=1),
        )

        assert apt.appointment_id.startswith("APT-")
        assert apt.materials_order is not None
        assert apt.materials_order.store_name == "Home Depot"

    def test_pickup_reminder(self):
        from materials.order_manager import get_order_manager
        from datetime import datetime

        manager = get_order_manager()
        apt = manager.schedule_appointment_with_pickup(
            customer_id="cust_002",
            customer_name="Jane Doe",
            customer_phone="+1987654321",
            trade="roofing",
            quote_id="Q-TEST-002",
            preferred_date=datetime.now(),
        )

        reminder = manager.get_pickup_reminder(apt)
        assert "MATERIALS PICKUP REMINDER" in reminder


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
