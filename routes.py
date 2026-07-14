"""
The 3 endpoints the GHL Voice AI agent calls as Custom Actions.

  POST /mykaarma/lookup-customer    -> who is calling, what do they drive
  POST /mykaarma/get-slots          -> REAL open appointment times
  POST /mykaarma/book-appointment   -> create the appointment in myKaarma

Design rule: the voice agent must never have to think. Each endpoint takes
simple inputs and returns simple, speakable outputs. All the UUID juggling,
JSON parsing and opcode mapping happens here, in code.
"""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

import mykaarma_client as mk
from config import MAX_SLOTS, get_dealer, DealerNotConfigured

log = logging.getLogger("mykaarma.routes")
router = APIRouter(prefix="/mykaarma", tags=["myKaarma"])

TRANSFER_NUMBER = "630-797-4570"


# ─────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────
class LookupRequest(BaseModel):
    phone: str = Field(..., description="Caller's phone number")
    dealer_key: Optional[str] = None


class SlotsRequest(BaseModel):
    service: str = Field(..., description="Plain English, e.g. 'oil change'")
    dates: List[str] = Field(..., description="['2026-07-16'] — yyyy-MM-dd")
    customer_uuid: Optional[str] = None
    vehicle_uuid: Optional[str] = None
    dealer_key: Optional[str] = None


class BookRequest(BaseModel):
    appointment_time: str = Field(..., description="2026-07-16T09:30:00")
    service: str
    customer_uuid: Optional[str] = None
    vehicle_uuid: Optional[str] = None
    # used only if the customer wasn't found on lookup
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    vin: Optional[str] = None
    comments: Optional[str] = None
    dealer_key: Optional[str] = None


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────
def _speak_time(iso: str) -> str:
    """'2026-07-16T09:30:00' -> 'Thursday, July 16 at 9:30 AM'"""
    try:
        return datetime.fromisoformat(iso).strftime("%A, %B %-d at %-I:%M %p")
    except ValueError:
        # Windows strftime doesn't support %-d / %-I
        try:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
        except Exception:
            return iso


def _fail(message: str, error: str = "error", **extra):
    """Any failure MUST tell the agent to hand off to a human. Never leave a caller stranded."""
    payload = {
        "success": False,
        "error": error,
        "message": message,
        "transfer_to": TRANSFER_NUMBER,
        "agent_instruction": (
            "Apologize, tell the customer you'll connect them with a service advisor, "
            f"and transfer the call to {TRANSFER_NUMBER}."
        ),
    }
    payload.update(extra)
    return payload


# ─────────────────────────────────────────────────────────────
# 1. LOOKUP CUSTOMER  — agent calls this at the START of the call
# ─────────────────────────────────────────────────────────────
@router.post("/lookup-customer")
async def lookup_customer(req: LookupRequest):
    try:
        dealer = get_dealer(req.dealer_key)
    except DealerNotConfigured as e:
        return _fail(str(e), "not_configured")

    try:
        raw = await mk.save_customer(dealer, phone=req.phone)
    except mk.MyKaarmaError as e:
        log.error("lookup failed: %s", e)
        # Not fatal — the agent can still collect details manually.
        return {
            "found": False,
            "customer_uuid": None,
            "vehicles": [],
            "agent_instruction": (
                "No customer record found. Ask for their name and the year, make "
                "and model of the vehicle."
            ),
        }

    c = mk.parse_customer(raw)
    found = bool(c["customer_uuid"] and (c["first_name"] or c["vehicles"]))

    if found and c["vehicles"]:
        labels = " or ".join(v["label"] for v in c["vehicles"])
        instruction = (
            f"Greet {c['first_name']} by name and confirm the vehicle: "
            f"'I see a {labels} on file — is that the vehicle you're bringing in?'"
        )
    elif found:
        instruction = (
            f"Greet {c['first_name']} by name, then ask for the year, make and model "
            "of the vehicle."
        )
    else:
        instruction = (
            "No customer record found. Ask for their name and the year, make and "
            "model of the vehicle."
        )

    return {
        "found": found,
        "customer_uuid": c["customer_uuid"],
        "first_name": c["first_name"],
        "last_name": c["last_name"],
        "vehicles": c["vehicles"],
        "agent_instruction": instruction,
    }


# ─────────────────────────────────────────────────────────────
# 2. GET SLOTS  — agent calls this once it knows the service + the day
# ─────────────────────────────────────────────────────────────
@router.post("/get-slots")
async def get_slots(req: SlotsRequest):
    try:
        dealer = get_dealer(req.dealer_key)
    except DealerNotConfigured as e:
        return _fail(str(e), "not_configured")

    try:
        catalog = await mk.get_opcodes(dealer)
    except mk.MyKaarmaError as e:
        return _fail("Could not load the service list.", "opcode_fetch_failed")

    op = mk.match_service(catalog, req.service)
    if not op:
        # We do NOT guess a service. Hand off.
        return _fail(
            f"'{req.service}' is not a service I can schedule automatically.",
            "service_not_recognized",
        )

    try:
        slots = await mk.get_availability(
            dealer,
            dates=req.dates,
            customer_uuid=req.customer_uuid,
            vehicle_uuid=req.vehicle_uuid,
            operation_uuid=op["uuid"],
        )
    except mk.MyKaarmaError:
        return _fail("Could not check the schedule.", "availability_failed")

    if not slots:
        return {
            "success": True,
            "slots": [],
            "spoken_slots": [],
            "agent_instruction": (
                "There are no openings on that day. Ask if another day works, then "
                "check again."
            ),
        }

    top = slots[:MAX_SLOTS]
    return {
        "success": True,
        "slots": top,                                   # ISO — send one of these back to /book
        "spoken_slots": [_speak_time(s) for s in top],  # what the agent reads out
        "operation_uuid": op["uuid"],
        "agent_instruction": (
            "Offer ONLY these times. Do NOT invent or guess any other time. "
            "Once the customer chooses one, call book_appointment with the exact "
            "matching value from 'slots'."
        ),
    }


# ─────────────────────────────────────────────────────────────
# 3. BOOK APPOINTMENT  — agent calls this after the customer picks a time
# ─────────────────────────────────────────────────────────────
@router.post("/book-appointment")
async def book_appointment(req: BookRequest):
    try:
        dealer = get_dealer(req.dealer_key)
    except DealerNotConfigured as e:
        return _fail(str(e), "not_configured")

    customer_uuid = req.customer_uuid
    vehicle_uuid = req.vehicle_uuid

    # 1. Create/find the customer if we don't already have them
    if not customer_uuid or not vehicle_uuid:
        try:
            raw = await mk.save_customer(
                dealer,
                phone=req.phone,
                first_name=req.first_name,
                last_name=req.last_name,
                email=req.email,
                vin=req.vin,
            )
        except mk.MyKaarmaError:
            return _fail("Could not create the customer record.", "customer_failed")

        c = mk.parse_customer(raw)
        customer_uuid = customer_uuid or c["customer_uuid"]
        if not vehicle_uuid and c["vehicles"]:
            vehicle_uuid = c["vehicles"][0]["vehicle_uuid"]

    if not customer_uuid or not vehicle_uuid:
        return _fail(
            "I couldn't confirm the customer or vehicle on file.",
            "missing_customer_or_vehicle",
        )

    # 2. Resolve the service
    try:
        catalog = await mk.get_opcodes(dealer)
    except mk.MyKaarmaError:
        return _fail("Could not load the service list.", "opcode_fetch_failed")

    op = mk.match_service(catalog, req.service)
    if not op:
        return _fail(
            f"'{req.service}' is not a service I can schedule automatically.",
            "service_not_recognized",
        )

    # 3. Re-check the slot is STILL free (myKaarma warns slots fill from other sources)
    day = req.appointment_time.split("T")[0]
    try:
        still_open = await mk.get_availability(
            dealer, [day], customer_uuid, vehicle_uuid, op["uuid"]
        )
    except mk.MyKaarmaError:
        still_open = []  # don't block the booking on a failed re-check

    if still_open and req.appointment_time not in still_open:
        alt = still_open[:MAX_SLOTS]
        return {
            "success": False,
            "error": "slot_taken",
            "message": "That time was just taken.",
            "slots": alt,
            "spoken_slots": [_speak_time(s) for s in alt],
            "agent_instruction": (
                "Apologize — that time was just booked. Offer ONLY these alternatives."
            ),
        }

    # 4. Book it
    try:
        result = await mk.create_appointment(
            dealer,
            customer_uuid=customer_uuid,
            vehicle_uuid=vehicle_uuid,
            start=req.appointment_time,
            service_op=op,
            phone=req.phone,
            email=req.email,
            comments=req.comments,
        )
    except mk.MyKaarmaError as e:
        log.error("booking failed: %s", e)
        return _fail("The appointment could not be booked.", "booking_failed")

    spoken = _speak_time(req.appointment_time)
    log.info("BOOKED %s for customer %s", req.appointment_time, customer_uuid)

    return {
        "success": True,
        "appointment_time": req.appointment_time,
        "spoken_time": spoken,
        "customer_uuid": customer_uuid,
        "vehicle_uuid": vehicle_uuid,
        "agent_instruction": (
            f"Confirm to the customer: 'You're all set for {spoken}.' Then read the "
            "date and time back once more, and let them know a confirmation is on "
            "the way."
        ),
        "mykaarma": result,
    }


# ─────────────────────────────────────────────────────────────
# Utility: refresh the cached opcode catalogue
# ─────────────────────────────────────────────────────────────
@router.post("/refresh-opcodes")
async def refresh_opcodes(dealer_key: Optional[str] = None):
    dealer = get_dealer(dealer_key)
    catalog = await mk.get_opcodes(dealer, force=True)
    return {"cached": len(catalog), "services": sorted(catalog.keys())[:50]}
