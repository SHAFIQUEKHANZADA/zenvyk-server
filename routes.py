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
import re
from datetime import datetime, timedelta
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from pydantic import BaseModel, Field

import mykaarma_client as mk
from config import MAX_SLOTS, get_dealer, DealerNotConfigured

log = logging.getLogger("mykaarma.routes")
router = APIRouter(prefix="/mykaarma", tags=["myKaarma"])

TRANSFER_NUMBER = "630-797-4570"
DEALER_TZ = ZoneInfo("America/Chicago")  # St. Charles, IL is Central
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")


def parse_appointment_time(raw: str) -> Optional[str]:
    """
    Turn whatever the voice agent sends into myKaarma ISO 'yyyy-MM-ddTHH:mm:ss'.
    Accepts already-ISO strings, or natural language like 'today 6 PM',
    'tomorrow 10 am', 'July 22 at 2pm'. Returns None if we can't parse it.
    """
    if not raw:
        return None
    raw = raw.strip()
    if ISO_RE.match(raw):
        return raw

    from dateutil import parser as dparser  # lazy import

    now = datetime.now(DEALER_TZ)
    base = now
    low = raw.lower()
    if "tomorrow" in low:
        base = now + timedelta(days=1)
        raw = re.sub(r"tomorrow", "", raw, flags=re.I).strip()
    elif "today" in low or "tonight" in low:
        raw = re.sub(r"today|tonight", "", raw, flags=re.I).strip()

    # default minute/second to 0 so "6 PM" -> 18:00:00 (not the current clock minutes)
    default = base.replace(minute=0, second=0, microsecond=0)
    try:
        dt = dparser.parse(raw, default=default, fuzzy=True)
    except Exception:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_day(raw: str) -> Optional[str]:
    """
    Turn whatever the voice agent sends into 'yyyy-MM-dd'.

    The agent is unreliable at date arithmetic and GHL has no {{current_date}}
    variable, so we resolve it server-side instead. Accepts already-formatted
    dates, 'today'/'tomorrow', bare weekday names ('Tuesday' -> the NEXT
    Tuesday, never one in the past), and things like 'July 22'.
    """
    if not raw:
        return None
    raw = str(raw).strip()
    if DATE_RE.match(raw):
        return raw

    today = datetime.now(DEALER_TZ).replace(tzinfo=None).date()
    low = raw.lower().strip()

    if "today" in low:
        return today.isoformat()
    if "tomorrow" in low:
        return (today + timedelta(days=1)).isoformat()

    # bare weekday name -> the next occurrence (today doesn't count)
    for name, idx in WEEKDAYS.items():
        if name in low:
            ahead = (idx - today.weekday()) % 7
            return (today + timedelta(days=ahead or 7)).isoformat()

    from dateutil import parser as dparser  # lazy import

    try:
        dt = dparser.parse(raw, default=datetime.combine(today, datetime.min.time()), fuzzy=True)
    except Exception:
        return None
    d = dt.date()
    if d < today:  # "July 22" when July 22 already passed -> next year
        try:
            d = d.replace(year=d.year + 1)
        except ValueError:
            return None
    return d.isoformat()


def _clamp_business(dt: datetime) -> datetime:
    """Move dt into business hours (8:00–16:59), skip Sundays, never in the past."""
    now = datetime.now(DEALER_TZ).replace(tzinfo=None)
    if dt < now:
        dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if dt.hour < 8:
        dt = dt.replace(hour=8, minute=0, second=0, microsecond=0)
    if dt.hour >= 17:
        dt = (dt + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    if dt.weekday() == 6:  # Sunday
        dt = (dt + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
    return dt


def candidate_times(start_iso: str, count: int = 16):
    """Yield bookable candidate times from start, +1hr steps, within 8–5, weekdays+Sat."""
    dt = _clamp_business(datetime.fromisoformat(start_iso))
    for _ in range(count):
        yield dt.strftime("%Y-%m-%dT%H:%M:%S")
        dt = _clamp_business(dt + timedelta(hours=1))


# ─────────────────────────────────────────────────────────────
# Request models
# ─────────────────────────────────────────────────────────────
class LookupRequest(BaseModel):
    phone: str = Field(..., description="Caller's phone number")
    dealer_key: Optional[str] = None


class SlotsRequest(BaseModel):
    service: str = Field(..., description="Plain English, e.g. 'oil change'")
    # The agent may send a real date OR plain words ("tomorrow", "Tuesday").
    # GHL has no {{current_date}} variable and voice agents are bad at date
    # arithmetic, so we resolve it here instead. Accepts a list or one string.
    dates: Optional[List[str]] = None
    day: Optional[str] = Field(None, description="'tomorrow' | 'Tuesday' | '2026-07-22'")
    customer_uuid: Optional[str] = None
    vehicle_uuid: Optional[str] = None
    dealer_key: Optional[str] = None


class BookRequest(BaseModel):
    appointment_time: str = Field(..., description="ISO like 2026-07-16T09:30:00, or natural like 'today 6 PM'")
    service: str
    customer_uuid: Optional[str] = None
    vehicle_uuid: Optional[str] = None
    # used only if the customer wasn't found on lookup
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    vin: Optional[str] = None
    # vehicle from the call (when there's no VIN / no record on file)
    vehicle_year: Optional[str] = None
    vehicle_make: Optional[str] = None
    vehicle_model: Optional[str] = None
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

    # Resolve whatever the agent sent into real yyyy-MM-dd dates.
    raw_days = list(req.dates or [])
    if req.day:
        raw_days.append(req.day)
    dates = [d for d in (parse_day(x) for x in raw_days) if d]
    if not dates:
        # No usable day — offer the next business day rather than dead-ending.
        dates = [_clamp_business(
            datetime.now(DEALER_TZ).replace(tzinfo=None) + timedelta(days=1)
        ).strftime("%Y-%m-%d")]

    # Match the service to an opcode if we can. If we can't (the sandbox only
    # has DUMMYOPCODE), still return real availability rather than dead-ending
    # the call — book_appointment already books without a service line.
    op = None
    try:
        catalog = await mk.get_opcodes(dealer)
        op = mk.match_service(catalog, req.service)
    except mk.MyKaarmaError:
        op = None

    try:
        slots = await mk.get_availability(
            dealer,
            dates=dates,
            customer_uuid=req.customer_uuid,
            vehicle_uuid=req.vehicle_uuid,
            operation_uuid=op["uuid"] if op else None,
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
        "date": dates[0],
        "slots": top,                                   # ISO — send one of these back to /book
        "spoken_slots": [_speak_time(s) for s in top],  # what the agent reads out
        "operation_uuid": op["uuid"] if op else None,
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

    # 0. Normalise the requested time to ISO (accepts "today 6 PM" etc.).
    #    If it's vague/unparseable ("asap", "soon"), default to the next business slot
    #    and let the auto-slot-finder book the first opening.
    start = parse_appointment_time(req.appointment_time)
    if not start:
        default_dt = _clamp_business(
            datetime.now(DEALER_TZ).replace(tzinfo=None) + timedelta(days=1)
        )
        start = default_dt.strftime("%Y-%m-%dT%H:%M:%S")

    customer_uuid = req.customer_uuid
    vehicle_uuid = req.vehicle_uuid

    # 1. Create/find the customer if we don't already have a customer_uuid.
    #    myKaarma can book with just the customerUuid — a vehicle UUID is NOT required
    #    (verified live: customerUuid + empty vehicleInformation books fine).
    if not customer_uuid:
        try:
            raw = await mk.save_customer(
                dealer,
                phone=req.phone,
                first_name=req.first_name,
                last_name=req.last_name,
                email=req.email,
                vin=req.vin,
                vehicle_year=req.vehicle_year,
                vehicle_make=req.vehicle_make,
                vehicle_model=req.vehicle_model,
            )
        except mk.MyKaarmaError:
            return _fail("Could not create the customer record.", "customer_failed")

        c = mk.parse_customer(raw)
        customer_uuid = c["customer_uuid"]
        if not vehicle_uuid and c["vehicles"]:
            vehicle_uuid = c["vehicles"][0]["vehicle_uuid"]

    if not customer_uuid:
        return _fail("I couldn't set up the customer record.", "missing_customer")

    # 2. Try to resolve the service to a real opcode. If it doesn't match
    #    (e.g. sandbox only has DUMMYOPCODE), book WITHOUT a service line — don't fail.
    op = None
    try:
        catalog = await mk.get_opcodes(dealer)
        op = mk.match_service(catalog, req.service)
    except mk.MyKaarmaError:
        op = None

    # 3. Book it. If the exact time is full, AUTO-ADVANCE to the next open slot
    #    (the sandbox has no availability API, so we find an open slot by trying).
    booked_time = None
    result = None
    last_err = None
    for cand in candidate_times(start, count=16):
        try:
            result = await mk.create_appointment(
                dealer,
                customer_uuid=customer_uuid,
                vehicle_uuid=vehicle_uuid,
                vin=req.vin,
                start=cand,
                service_op=op,
                phone=req.phone,
                email=req.email,
                comments=req.comments or (f"Service requested: {req.service}"),
            )
            booked_time = cand
            break
        except mk.MyKaarmaError as e:
            last_err = e
            body = e.body or ""
            if "SLOT_UNAVAILABLE" in body or "NO_TIME_INTERVAL" in body:
                continue  # that slot is full — try the next one
            log.error("booking failed (non-slot error): %s", e)
            return _fail("The appointment could not be booked.", "booking_failed")

    if not booked_time:
        log.error("no open slot found near %s: %s", start, last_err)
        return _fail(
            "I couldn't find an open time near then. Let me have an advisor call you back.",
            "no_open_slot",
        )

    spoken = _speak_time(booked_time)
    log.info("BOOKED %s for customer %s", booked_time, customer_uuid)

    return {
        "success": True,
        "appointment_time": booked_time,
        "spoken_time": spoken,
        "requested_time": start,
        "customer_uuid": customer_uuid,
        "vehicle_uuid": vehicle_uuid,
        "agent_instruction": (
            f"The appointment is booked for {spoken}. Tell the customer: "
            f"'You're all set for {spoken}.' If that's different from what they asked, "
            "briefly mention it was the closest opening. Then let them know a "
            "confirmation is on the way."
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
