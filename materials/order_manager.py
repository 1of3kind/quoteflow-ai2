"""Material order manager - links quotes to store orders and appointments."""

import os
import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from materials.store_inventory import StoreFinder, MaterialOrder
from core.quote_calculator import QuoteResult

logger = logging.getLogger("order_manager")


@dataclass
class AppointmentWithMaterials:
    """An appointment that includes material pickup info."""
    appointment_id: str
    customer_id: str
    customer_name: str
    customer_phone: str
    trade: str
    quote_id: str
    scheduled_date: datetime
    estimated_duration_hours: float
    materials_order: Optional[MaterialOrder]
    pickup_reminder_sent: bool = False
    materials_confirmed: bool = False
    notes: str = ""


class OrderManager:
    """Manages material orders and links them to appointments."""

    def __init__(self):
        self.orders: Dict[str, MaterialOrder] = {}
        self.appointments: Dict[str, AppointmentWithMaterials] = {}
        self.quote_to_order: Dict[str, str] = {}  # quote_id -> order_id
        self._counter = 0

    def _generate_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{datetime.now().strftime('%Y%m%d')}-{self._counter:04d}"

    async def create_materials_order(
        self,
        quote: QuoteResult,
        trade: str,
        zip_code: str = "",
    ) -> Optional[MaterialOrder]:
        """Create a materials order based on quote analysis."""

        # Extract materials from quote breakdown
        materials_needed = self._extract_materials(quote)

        if not materials_needed:
            logger.info(f"No materials needed for quote {quote.quote_id}")
            return None

        # Calculate quantities based on quote breakdown
        quantities = self._calculate_quantities(quote, materials_needed)

        # Find and order materials
        finder = StoreFinder(zip_code=zip_code)
        order = await finder.create_optimal_order(
            quote_id=quote.quote_id,
            trade=trade,
            materials_needed=materials_needed,
            quantities=quantities,
        )

        if order:
            self.orders[order.order_id] = order
            self.quote_to_order[quote.quote_id] = order.order_id
            logger.info(f"Created materials order {order.order_id} for quote {quote.quote_id}")

        return order

    def _extract_materials(self, quote: QuoteResult) -> List[str]:
        """Extract material names from quote breakdown."""
        materials = []

        # Map trade to common materials
        trade_materials = {
            "landscaping": ["sod", "mulch", "edging", "topsoil", "fertilizer"],
            "roofing": ["shingles", "underlayment", "nails", "flashing", "vent"],
            "plumbing": ["pipe", "fittings", "valve", "sealant", "tape"],
            "autobody": ["primer", "paint", "sandpaper", "clear coat", "filler"],
            "electrical": ["wire", "outlet", "breaker", "conduit", "box"],
        }

        # Get materials from breakdown if available
        breakdown = quote.breakdown
        if "materials" in breakdown:
            materials.extend(breakdown["materials"])

        # Add trade-specific defaults
        trade = quote.trade.lower()
        if trade in trade_materials:
            for mat in trade_materials[trade]:
                if mat not in [m.lower() for m in materials]:
                    materials.append(mat)

        return materials

    def _calculate_quantities(self, quote: QuoteResult, materials: List[str]) -> Dict[str, int]:
        """Calculate how much of each material is needed."""
        quantities = {}
        breakdown = quote.breakdown

        # Base quantities on job size
        sqft = breakdown.get("sqft") or breakdown.get("roof_sqft", 1000)

        for material in materials:
            mat_lower = material.lower()

            if "sod" in mat_lower:
                quantities[material] = max(1, sqft // 10)
            elif "mulch" in mat_lower:
                quantities[material] = max(1, sqft // 100)
            elif "shingle" in mat_lower:
                quantities[material] = max(1, sqft // 33)
            elif "pipe" in mat_lower:
                quantities[material] = max(1, breakdown.get("fixtures", 1) * 2)
            elif "wire" in mat_lower:
                quantities[material] = max(1, breakdown.get("outlets", 1) * 25 // 250 + 1)
            elif "outlet" in mat_lower:
                quantities[material] = max(1, breakdown.get("outlets", 1))
            elif "primer" in mat_lower or "paint" in mat_lower:
                quantities[material] = max(1, breakdown.get("panels", 1))
            else:
                quantities[material] = 1

        return quantities

    def schedule_appointment_with_pickup(
        self,
        customer_id: str,
        customer_name: str,
        customer_phone: str,
        trade: str,
        quote_id: str,
        preferred_date: datetime,
        duration_hours: float = 4.0,
    ) -> AppointmentWithMaterials:
        """Schedule an appointment and link materials order."""

        appt_id = self._generate_id("APT")

        # Get materials order if exists
        order_id = self.quote_to_order.get(quote_id)
        materials_order = self.orders.get(order_id) if order_id else None

        # If materials order exists, schedule pickup for morning of appointment
        if materials_order:
            pickup_time = preferred_date.replace(hour=7, minute=0)
            # Update order with pickup time
            materials_order.pickup_time = pickup_time.isoformat()

        appointment = AppointmentWithMaterials(
            appointment_id=appt_id,
            customer_id=customer_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            trade=trade,
            quote_id=quote_id,
            scheduled_date=preferred_date,
            estimated_duration_hours=duration_hours,
            materials_order=materials_order,
            notes=f"Materials pickup at {materials_order.store_name if materials_order else 'N/A'}",
        )

        self.appointments[appt_id] = appointment
        logger.info(f"Scheduled appointment {appt_id} with materials order {order_id}")

        return appointment

    def get_pickup_reminder(self, appointment: AppointmentWithMaterials) -> str:
        """Generate pickup reminder message for contractor."""
        if not appointment.materials_order:
            return "No materials order for this appointment."

        order = appointment.materials_order
        items_text = "\n".join([
            f"  • {item.name} - {item.brand} - ${item.price:.2f} (Aisle {item.aisle_location or 'TBD'})"
            for item in order.items
        ])

        return f"""📦 MATERIALS PICKUP REMINDER

Appointment: {appointment.appointment_id}
Job: {appointment.trade.title()} for {appointment.customer_name}
Date: {appointment.scheduled_date.strftime('%A, %B %d at %I:%M %p')}

Store: {order.store_name}
Address: {order.store_address}
Pickup Time: {order.pickup_time}
Order #: {order.order_id}

ITEMS TO PICK UP:
{items_text}

Subtotal: ${order.subtotal:.2f}
Tax: ${order.tax:.2f}
Total: ${order.total:.2f}

Reply CONFIRM when picked up.
"""

    def get_daily_schedule(self, date: datetime) -> List[AppointmentWithMaterials]:
        """Get all appointments for a given date with pickup info."""
        return [
            apt for apt in self.appointments.values()
            if apt.scheduled_date.date() == date.date()
        ]

    def get_pending_pickups(self) -> List[AppointmentWithMaterials]:
        """Get appointments with materials not yet picked up."""
        return [
            apt for apt in self.appointments.values()
            if apt.materials_order and not apt.materials_confirmed
        ]

    def confirm_pickup(self, appointment_id: str) -> bool:
        """Mark materials as picked up."""
        apt = self.appointments.get(appointment_id)
        if apt:
            apt.materials_confirmed = True
            logger.info(f"Materials confirmed picked up for {appointment_id}")
            return True
        return False


# Singleton instance
_order_manager = OrderManager()


def get_order_manager() -> OrderManager:
    return _order_manager
