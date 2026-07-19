"""Local store inventory checker for materials."""

import os
import logging
import json
from typing import Dict, List, Optional
from dataclasses import dataclass
from datetime import datetime

import httpx

logger = logging.getLogger("store_inventory")


@dataclass
class StoreItem:
    sku: str
    name: str
    brand: str
    price: float
    in_stock: bool
    quantity_available: int
    store_id: str
    store_name: str
    store_address: str
    distance_miles: float
    aisle_location: Optional[str]
    pickup_ready: bool  # Can be ready in < 2 hours


@dataclass
class MaterialOrder:
    order_id: str
    quote_id: str
    trade: str
    items: List[StoreItem]
    subtotal: float
    tax: float
    total: float
    store_name: str
    store_address: str
    pickup_time: str
    status: str  # pending, confirmed, picked_up, cancelled


class StoreInventoryAPI:
    """Base class for store inventory APIs."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key
        self.base_url = ""

    async def search_product(self, query: str, zip_code: str) -> List[StoreItem]:
        raise NotImplementedError

    async def check_stock(self, sku: str, store_id: str) -> Dict:
        raise NotImplementedError

    async def create_pickup_order(self, items: List[Dict], store_id: str) -> str:
        raise NotImplementedError


class HomeDepotAPI(StoreInventoryAPI):
    """Home Depot inventory integration."""

    def __init__(self, api_key: str = ""):
        super().__init__(api_key)
        self.base_url = "https://api.homedepot.com/v1"

    async def search_product(self, query: str, zip_code: str) -> List[StoreItem]:
        """Search Home Depot for product by keyword near zip code."""
        url = f"{self.base_url}/products/search"
        params = {
            "keyword": query,
            "zip": zip_code,
            "radius": 25,
            "limit": 5,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()

            items = []
            for p in data.get("products", []):
                for store in p.get("stores", []):
                    items.append(StoreItem(
                        sku=p.get("sku", ""),
                        name=p.get("name", ""),
                        brand=p.get("brand", ""),
                        price=float(p.get("price", 0)),
                        in_stock=store.get("inventory", {}).get("quantity", 0) > 0,
                        quantity_available=store.get("inventory", {}).get("quantity", 0),
                        store_id=store.get("storeId", ""),
                        store_name=f"Home Depot #{store.get('storeId', '')}",
                        store_address=store.get("address", ""),
                        distance_miles=float(store.get("distance", 0)),
                        aisle_location=store.get("aisle", ""),
                        pickup_ready=store.get("inventory", {}).get("quantity", 0) >= 5,
                    ))
            return items
        except Exception as e:
            logger.error(f"Home Depot API error: {e}")
            return []

    async def create_pickup_order(self, items: List[Dict], store_id: str) -> str:
        """Create BOPIS (Buy Online Pickup In Store) order."""
        url = f"{self.base_url}/orders/pickup"
        payload = {
            "store_id": store_id,
            "items": items,
            "notification": "sms",
        }
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=10)
                resp.raise_for_status()
                return resp.json().get("order_id", "")
        except Exception as e:
            logger.error(f"Home Depot order error: {e}")
            return ""


class LowesAPI(StoreInventoryAPI):
    """Lowe's inventory integration."""

    def __init__(self, api_key: str = ""):
        super().__init__(api_key)
        self.base_url = "https://api.lowes.com/v1"

    async def search_product(self, query: str, zip_code: str) -> List[StoreItem]:
        """Search Lowe's for product by keyword near zip code."""
        url = f"{self.base_url}/products"
        params = {
            "searchTerm": query,
            "storeNumber": "",
            "zipCode": zip_code,
            "maxResults": 5,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()

            items = []
            for p in data.get("products", []):
                for store in p.get("stores", []):
                    items.append(StoreItem(
                        sku=p.get("itemNumber", ""),
                        name=p.get("description", ""),
                        brand=p.get("brand", ""),
                        price=float(p.get("pricing", {}).get("sellingPrice", 0)),
                        in_stock=store.get("inventory", {}).get("quantity", 0) > 0,
                        quantity_available=store.get("inventory", {}).get("quantity", 0),
                        store_id=str(store.get("storeNumber", "")),
                        store_name=f"Lowe's #{store.get('storeNumber', '')}",
                        store_address=store.get("address", ""),
                        distance_miles=float(store.get("distance", 0)),
                        aisle_location=store.get("location", {}).get("aisle", ""),
                        pickup_ready=store.get("inventory", {}).get("quantity", 0) >= 3,
                    ))
            return items
        except Exception as e:
            logger.error(f"Lowe's API error: {e}")
            return []


class AceHardwareAPI(StoreInventoryAPI):
    """Ace Hardware inventory integration."""

    def __init__(self, api_key: str = ""):
        super().__init__(api_key)
        self.base_url = "https://api.acehardware.com/v1"

    async def search_product(self, query: str, zip_code: str) -> List[StoreItem]:
        """Search Ace Hardware for product."""
        url = f"{self.base_url}/products/search"
        params = {"q": query, "zip": zip_code, "radius": 15}
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, params=params, headers=headers, timeout=10)
                resp.raise_for_status()
                data = resp.json()

            items = []
            for p in data.get("products", []):
                for store in p.get("stores", []):
                    items.append(StoreItem(
                        sku=p.get("sku", ""),
                        name=p.get("name", ""),
                        brand=p.get("brand", ""),
                        price=float(p.get("price", 0)),
                        in_stock=store.get("inStock", False),
                        quantity_available=store.get("quantity", 0),
                        store_id=str(store.get("storeId", "")),
                        store_name=f"Ace Hardware #{store.get('storeId', '')}",
                        store_address=store.get("address", ""),
                        distance_miles=float(store.get("distance", 0)),
                        aisle_location=None,
                        pickup_ready=store.get("inStock", False),
                    ))
            return items
        except Exception as e:
            logger.error(f"Ace Hardware API error: {e}")
            return []


class MockStoreAPI(StoreInventoryAPI):
    """Mock store API for testing without real API keys."""

    MOCK_INVENTORY = {
        "landscaping": [
            {"sku": "HD-SOD-001", "name": "Premium Sod Rolls (10 sq ft)", "brand": "Scotts", "price": 8.99, "store": "Home Depot", "aisle": "A12"},
            {"sku": "HD-MUL-001", "name": "Brown Mulch 2 cu ft", "brand": "Vigoro", "price": 3.97, "store": "Home Depot", "aisle": "A14"},
            {"sku": "HD-EDG-001", "name": "Vigoro Lawn Edging 20 ft", "brand": "Vigoro", "price": 24.98, "store": "Home Depot", "aisle": "A15"},
            {"sku": "LW-SOD-001", "name": "Sod - St Augustine", "brand": "Pennington", "price": 9.49, "store": "Lowe's", "aisle": "B8"},
            {"sku": "LW-MUL-001", "name": "Black Mulch 2 cu ft", "brand": "Scotts", "price": 3.78, "store": "Lowe's", "aisle": "B9"},
        ],
        "roofing": [
            {"sku": "HD-SHI-001", "name": "GAF Timberline HDZ Shingles (33.3 sq ft)", "brand": "GAF", "price": 42.50, "store": "Home Depot", "aisle": "R22"},
            {"sku": "HD-UND-001", "name": "Roofing Underlayment 4x250 ft", "brand": "Grace", "price": 89.00, "store": "Home Depot", "aisle": "R23"},
            {"sku": "HD-NAI-001", "name": "Roofing Nails 1.25\" 5lb", "brand": "Grip-Rite", "price": 12.98, "store": "Home Depot", "aisle": "R24"},
            {"sku": "LW-SHI-001", "name": "Owens Corning Duration Shingles", "brand": "Owens Corning", "price": 39.99, "store": "Lowe's", "aisle": "C15"},
        ],
        "plumbing": [
            {"sku": "HD-PIP-001", "name": "3/4\" Copper Pipe 10 ft", "brand": "Mueller", "price": 18.47, "store": "Home Depot", "aisle": "P10"},
            {"sku": "HD-PEX-001", "name": "1/2\" PEX Tubing 100 ft", "brand": "SharkBite", "price": 32.98, "store": "Home Depot", "aisle": "P11"},
            {"sku": "HD-FIT-001", "name": "SharkBite Push Fittings 1/2\" (10pk)", "brand": "SharkBite", "price": 24.97, "store": "Home Depot", "aisle": "P12"},
            {"sku": "LW-PEX-001", "name": "Apollo PEX Tubing 1/2\" 100 ft", "brand": "Apollo", "price": 29.98, "store": "Lowe's", "aisle": "D8"},
        ],
        "autobody": [
            {"sku": "HD-SAN-001", "name": "3M Sandpaper Assorted Grit (50pk)", "brand": "3M", "price": 14.97, "store": "Home Depot", "aisle": "A20"},
            {"sku": "HD-PRM-001", "name": "Rust-Oleum Auto Primer White", "brand": "Rust-Oleum", "price": 6.98, "store": "Home Depot", "aisle": "A21"},
            {"sku": "HD-CLE-001", "name": "Denatured Alcohol 1 qt", "brand": "Klean-Strip", "price": 8.47, "store": "Home Depot", "aisle": "A22"},
        ],
        "electrical": [
            {"sku": "HD-OUT-001", "name": "Leviton Decora Outlet 15A (10pk)", "brand": "Leviton", "price": 19.97, "store": "Home Depot", "aisle": "E10"},
            {"sku": "HD-WIR-001", "name": "12/2 Romex Wire 250 ft", "brand": "Southwire", "price": 89.00, "store": "Home Depot", "aisle": "E11"},
            {"sku": "HD-BRE-001", "name": "Square D 20A Breaker", "brand": "Square D", "price": 12.98, "store": "Home Depot", "aisle": "E12"},
            {"sku": "LW-WIR-001", "name": "CerroWire 12/2 Romex 250 ft", "brand": "CerroWire", "price": 84.99, "store": "Lowe's", "aisle": "F10"},
        ],
    }

    async def search_product(self, query: str, zip_code: str) -> List[StoreItem]:
        """Return mock products based on trade keyword matching."""
        items = []
        query_lower = query.lower()

        for trade, products in self.MOCK_INVENTORY.items():
            if trade in query_lower or any(kw in query_lower for kw in ["material", "supply"]):
                for p in products:
                    items.append(StoreItem(
                        sku=p["sku"],
                        name=p["name"],
                        brand=p["brand"],
                        price=p["price"],
                        in_stock=True,
                        quantity_available=50,
                        store_id="MOCK001",
                        store_name=f"{p['store']} - Mock Location",
                        store_address=f"123 Main St, {zip_code}",
                        distance_miles=2.5,
                        aisle_location=p.get("aisle", ""),
                        pickup_ready=True,
                    ))

        # Also do keyword matching within product names
        for trade, products in self.MOCK_INVENTORY.items():
            for p in products:
                if any(kw in query_lower for kw in p["name"].lower().split()):
                    if not any(i.sku == p["sku"] for i in items):
                        items.append(StoreItem(
                            sku=p["sku"],
                            name=p["name"],
                            brand=p["brand"],
                            price=p["price"],
                            in_stock=True,
                            quantity_available=50,
                            store_id="MOCK001",
                            store_name=f"{p['store']} - Mock Location",
                            store_address=f"123 Main St, {zip_code}",
                            distance_miles=2.5,
                            aisle_location=p.get("aisle", ""),
                            pickup_ready=True,
                        ))

        return items[:10]  # Limit results

    async def create_pickup_order(self, items: List[Dict], store_id: str) -> str:
        """Create a mock pickup order."""
        order_id = f"ORD-MOCK-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        logger.info(f"Mock order created: {order_id} for {len(items)} items")
        return order_id


class StoreFinder:
    """Finds materials across multiple local stores."""

    def __init__(self, zip_code: str = "", use_mock: bool = False):
        self.zip_code = zip_code or os.getenv("BUSINESS_ZIP", "90210")
        self.use_mock = use_mock or not os.getenv("STORE_API_KEY")

        if self.use_mock:
            self.stores = {"mock": MockStoreAPI()}
        else:
            self.stores = {
                "homedepot": HomeDepotAPI(os.getenv("HOMEDEPOT_API_KEY", "")),
                "lowes": LowesAPI(os.getenv("LOWES_API_KEY", "")),
                "ace": AceHardwareAPI(os.getenv("ACE_API_KEY", "")),
            }

    async def find_materials(self, trade: str, materials_needed: List[str]) -> Dict[str, List[StoreItem]]:
        """Find all needed materials across stores."""
        results = {}

        for material in materials_needed:
            all_items = []
            for store_name, api in self.stores.items():
                try:
                    items = await api.search_product(material, self.zip_code)
                    all_items.extend(items)
                except Exception as e:
                    logger.warning(f"Store {store_name} search failed: {e}")

            # Sort by price, then by distance
            all_items.sort(key=lambda x: (x.price, x.distance_miles))
            results[material] = all_items

        return results

    async def create_optimal_order(
        self,
        quote_id: str,
        trade: str,
        materials_needed: List[str],
        quantities: Dict[str, int],
    ) -> Optional[MaterialOrder]:
        """Create an order picking the best price/availability across stores."""

        materials = await self.find_materials(trade, materials_needed)

        selected_items = []
        subtotal = 0.0
        store_votes = {}

        for material, items in materials.items():
            if not items:
                continue

            # Pick first in-stock item (already sorted by price)
            best = next((i for i in items if i.in_stock), None)
            if not best:
                best = items[0]  # Fallback to first result

            qty = quantities.get(material, 1)
            selected_items.append(best)
            subtotal += best.price * qty
            store_votes[best.store_name] = store_votes.get(best.store_name, 0) + 1

        if not selected_items:
            return None

        # Pick the store with the most items
        primary_store = max(store_votes, key=store_votes.get)
        store_items = [i for i in selected_items if i.store_name == primary_store]

        # If we can't get everything at one store, note it
        if len(store_items) < len(selected_items):
            logger.info(f"Split order: {len(selected_items) - len(store_items)} items at other stores")

        tax = subtotal * 0.08
        total = subtotal + tax

        # Create order at primary store
        order_items = [{"sku": i.sku, "quantity": quantities.get(i.name, 1)} for i in store_items]

        if self.use_mock:
            order_id = await MockStoreAPI().create_pickup_order(order_items, "MOCK001")
        else:
            # Use the actual store API
            store_api = self.stores.get("homedepot")  # Default to Home Depot
            order_id = await store_api.create_pickup_order(order_items, store_items[0].store_id)

        return MaterialOrder(
            order_id=order_id,
            quote_id=quote_id,
            trade=trade,
            items=selected_items,
            subtotal=round(subtotal, 2),
            tax=round(tax, 2),
            total=round(total, 2),
            store_name=primary_store,
            store_address=store_items[0].store_address if store_items else "",
            pickup_time=(datetime.now().replace(hour=8, minute=0)).isoformat(),
            status="confirmed" if order_id else "pending",
        )
