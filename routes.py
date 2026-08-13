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


# Service-lane hours per Reid, 2026-07-24. Monday=0 … Sunday=6.
# (open_hour, close_hour) in 24h; close_hour 24 means midnight.
# The store is open 7 days — an earlier version skipped Sunday entirely, which
# made Esther refuse a day the dealership is actually open.
DEALER_HOURS = {
    0: (6, 24),   # Monday    6:00 AM – midnight
    1: (0, 24),   # Tuesday   24 hours
    2: (0, 24),   # Wednesday 24 hours
    3: (0, 24),   # Thursday  24 hours
    4: (0, 24),   # Friday    24 hours
    5: (0, 16),   # Saturday  midnight – 4:00 PM
    6: (8, 16),   # Sunday    8:00 AM – 4:00 PM
}

# We never OFFER a 3 AM appointment even on a 24-hour day — myKaarma's own
# configured hours cap this anyway, and nobody wants a call offering 2 AM.
SPEAKABLE_START = 8
SPEAKABLE_END = 17

# Never offer a slot that starts sooner than this many minutes from now (dealer-
# local). Without this, a same-day call in the evening is still offered that
# morning's times — myKaarma returns the whole day's grid regardless of the clock.
SLOT_LEAD_MINUTES = 30


def day_hours(dt: datetime) -> tuple:
    """Bookable (open_hour, close_hour) for that weekday, clamped to sane times."""
    open_h, close_h = DEALER_HOURS.get(dt.weekday(), (8, 17))
    return max(open_h, SPEAKABLE_START), min(close_h, SPEAKABLE_END)


def _clamp_business(dt: datetime) -> datetime:
    """Move dt into the dealership's real hours for that day, never in the past."""
    now = datetime.now(DEALER_TZ).replace(tzinfo=None)
    if dt < now:
        dt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    for _ in range(8):  # at most a week of rolling forward
        open_h, close_h = day_hours(dt)
        if dt.hour < open_h:
            dt = dt.replace(hour=open_h, minute=0, second=0, microsecond=0)
        if dt.hour >= close_h:
            dt = dt + timedelta(days=1)
            open_h, _ = day_hours(dt)
            dt = dt.replace(hour=open_h, minute=0, second=0, microsecond=0)
            continue
        return dt
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
    # what the caller chose in the transport step ("shuttle"/"loaner"/"waiting"/"drop off")
    transport: Optional[str] = None
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
    # what the caller said in the transport step: "waiting" / "dropping it off" / "shuttle"
    transport: Optional[str] = None
    # RESCHEDULE: when the caller is moving an existing appointment, pass its UUID
    # (from lookup-customer's existing_appointment.appointment_uuid). We then UPDATE
    # that appointment in place instead of creating a new one — no duplicate.
    reschedule_appointment_uuid: Optional[str] = None
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


def _speak_datetime(raw: str) -> str:
    """
    '2026-08-15 10:30:00' -> 'tomorrow, Friday, August 15 at 10:30 AM'.
    Prepends today/tomorrow when it applies (Reid's feedback: say 'today', not
    just the bare date) and otherwise reads the day naturally.
    """
    if not raw:
        return "your scheduled time"
    dt = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(raw, fmt)
            break
        except (ValueError, TypeError):
            continue
    if dt is None:
        return raw
    try:
        body = dt.strftime("%A, %B %-d at %-I:%M %p")
    except ValueError:  # Windows
        body = dt.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
    today = datetime.now(DEALER_TZ).replace(tzinfo=None).date()
    delta = (dt.date() - today).days
    if delta == 0:
        return f"today, {body}"
    if delta == 1:
        return f"tomorrow, {body}"
    return body


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

    # READ-ONLY search. This used to call save_customer(), which is a WRITE —
    # it created a fresh blank customer on every lookup instead of finding the
    # existing one, so returning callers were never recognised.
    try:
        matches = await mk.search_customer(dealer, phone=req.phone)
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

    if not matches:
        return {
            "found": False,
            "customer_uuid": None,
            "first_name": None,
            "last_name": None,
            "vehicles": [],
            "agent_instruction": (
                "No customer record found. Ask for their name and the year, make "
                "and model of the vehicle."
            ),
        }

    c = mk.parse_search_match(matches[0])
    found = bool(c["customer_uuid"] and (c["first_name"] or c["vehicles"]))

    # Does this caller already have an upcoming appointment? If so, the agent
    # should offer to confirm/reschedule instead of silently booking a duplicate.
    existing = None
    if c["customer_uuid"]:
        try:
            appts = await mk.get_customer_appointments(dealer, c["customer_uuid"])
            now_local = datetime.now(DEALER_TZ).replace(tzinfo=None)
            upcoming = mk.upcoming_appointments(appts, now_local)
            if upcoming:
                existing = upcoming[0]
        except mk.MyKaarmaError as e:
            log.warning("appointment read failed for %s: %s", c["customer_uuid"], e)

    if existing:
        when = _speak_datetime(existing.get("start_time"))
        svc = existing["services"][0] if existing.get("services") else "service"
        veh = existing.get("vehicle")
        veh_txt = f" for the {veh}" if veh else ""
        instruction = (
            f"Greet {c['first_name'] or 'the caller'} by name. They ALREADY have an "
            f"upcoming appointment{veh_txt} on {when} for {svc}. Do NOT book a new "
            f"appointment yet — first tell them about this existing appointment and "
            f"ask if they're calling to confirm it, reschedule it, or book something "
            f"different. Only create a new appointment if they clearly want an "
            f"additional/different one."
        )
    elif found and c["vehicles"]:
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
        "has_existing_appointment": bool(existing),
        # Flattened to the TOP level so the agent can pass it straight into
        # book_appointment's reschedule_appointment_uuid without digging into a
        # nested object (models drop nested fields across tool calls).
        "existing_appointment_uuid": existing["appointment_uuid"] if existing else None,
        "existing_appointment": existing,
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

    # Use that specific day's opening hours — Saturday and Sunday close at 4 PM.
    _d = datetime.strptime(dates[0], "%Y-%m-%d")
    _open, _close = day_hours(_d)

    # myKaarma cert: send the caller's chosen transport so availability matches what
    # the create-appointment call will book (avoids create failures).
    transport_uuid = await mk.resolve_transport(dealer, req.transport)

    try:
        slots = await mk.get_availability(
            dealer,
            dates=dates,
            customer_uuid=req.customer_uuid,
            vehicle_uuid=req.vehicle_uuid,
            operation_uuid=op["uuid"] if op else None,
            transport_option_uuid=transport_uuid,
            start_time=f"{_open:02d}:00:00",
            end_time=f"{_close:02d}:00:00",
        )
    except mk.MyKaarmaError:
        return _fail("Could not check the schedule.", "availability_failed")

    # Drop any slot already in the past (or too soon) in the dealer's local time.
    # myKaarma hands back the whole day's grid with no regard for the current clock,
    # so a 10 PM "today" request would otherwise be offered 8 AM–4 PM slots that have
    # all passed. Future dates are untouched — every one of their slots is > now.
    now_local = datetime.now(DEALER_TZ).replace(tzinfo=None)
    earliest = now_local + timedelta(minutes=SLOT_LEAD_MINUTES)
    slots = [s for s in slots if datetime.fromisoformat(s) > earliest]

    # Nothing on the requested day? Don't dead-end the call — look ahead and offer
    # the next day that HAS openings. Callers routinely ask "when's the first
    # available?", and a same-day request late in the day always comes back empty.
    searched_ahead = False
    if not slots:
        probe = datetime.strptime(dates[0], "%Y-%m-%d")
        for _ in range(14):  # up to two weeks out
            probe += timedelta(days=1)
            nxt = probe.strftime("%Y-%m-%d")
            try:
                found = await mk.get_availability(
                    dealer,
                    dates=[nxt],
                    customer_uuid=req.customer_uuid,
                    vehicle_uuid=req.vehicle_uuid,
                    operation_uuid=op["uuid"] if op else None,
                    transport_option_uuid=transport_uuid,
                )
            except mk.MyKaarmaError:
                continue
            if found:
                slots, dates, searched_ahead = found, [nxt], True
                break

    if not slots:
        return {
            "success": True,
            "date": dates[0],
            "slots": [],
            "spoken_slots": [],
            "agent_instruction": (
                "There are no openings in the next two weeks. Apologise, take a message, "
                "and let the customer know an advisor will call them back."
            ),
        }

    top = slots[:MAX_SLOTS]
    spoken_day = datetime.strptime(dates[0], "%Y-%m-%d").strftime("%A, %B %d").replace(" 0", " ")

    if searched_ahead:
        instruction = (
            f"The day the customer asked for has no more available times (fully booked, "
            f"or it's already too late in the day). The next day with openings is "
            f"{spoken_day}. Let them know that day isn't available, then offer ONLY the "
            f"times in 'spoken_slots' for {spoken_day}. Do NOT invent times. Do NOT "
            "transfer — keep helping. Once they choose, call book_appointment with the "
            "exact matching value from 'slots'."
        )
    else:
        instruction = (
            "Offer ONLY these times. Do NOT invent or guess any other time. "
            "Once the customer chooses one, call book_appointment with the exact "
            "matching value from 'slots'."
        )

    return {
        "success": True,
        "date": dates[0],
        "spoken_date": spoken_day,
        "searched_ahead": searched_ahead,
        "slots": top,                                   # ISO — send one of these back to /book
        "spoken_slots": [_speak_time(s) for s in top],  # what the agent reads out
        "operation_uuid": op["uuid"] if op else None,
        "agent_instruction": instruction,
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

    # ── ENFORCE ONE APPOINTMENT PER CUSTOMER ──────────────────────────────────
    # Business rule: a customer may only have ONE active/upcoming appointment. If
    # they already have one, we RESCHEDULE (update) it instead of creating a
    # duplicate — even if the agent didn't explicitly flag a reschedule. Missed /
    # no-show / past appointments don't count (upcoming_appointments filters them),
    # so those correctly fall through to a brand-new booking.
    reschedule_uuid = req.reschedule_appointment_uuid
    if not reschedule_uuid:
        check_uuid = customer_uuid
        if not check_uuid and req.phone:
            try:
                _ms = await mk.search_customer(dealer, phone=req.phone)
                if _ms:
                    check_uuid = mk.parse_search_match(_ms[0])["customer_uuid"]
            except mk.MyKaarmaError:
                check_uuid = None
        if check_uuid:
            try:
                _now = datetime.now(DEALER_TZ).replace(tzinfo=None)
                _existing = mk.upcoming_appointments(
                    await mk.get_customer_appointments(dealer, check_uuid), _now
                )
                if _existing:
                    reschedule_uuid = _existing[0]["appointment_uuid"]
                    log.info(
                        "auto-reschedule: customer %s already has an upcoming "
                        "appointment (%s) — updating it instead of duplicating",
                        check_uuid, reschedule_uuid,
                    )
            except mk.MyKaarmaError as e:
                log.warning("auto-reschedule check failed: %s", e)

    # 1. ALWAYS save the customer with the full details we collected on the call.
    #    Earlier this only ran when no customer_uuid was passed — but lookup_customer
    #    creates a PHONE-ONLY record and hands its uuid to this step, so booking used
    #    to attach to a nameless record ("customer not in myKaarma by name"). Running
    #    save_customer with searchForDuplicate=true here matches that same phone record
    #    and enriches it with the name, email, and vehicle. myKaarma can book with just
    #    a customerUuid, so we only skip the save if we have literally nothing to add.
    # On a RESCHEDULE we're just moving an appointment that already exists — we do
    # NOT need to save/enrich the customer or re-resolve the vehicle by phone. That
    # phone-based save/lookup is what trips over duplicate customer records, so skip
    # it entirely when rescheduling and go straight to the update.
    have_details = any([
        req.first_name, req.last_name, req.email, req.vin,
        req.vehicle_year, req.vehicle_make, req.vehicle_model, req.phone,
    ])
    if have_details and not reschedule_uuid:
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
            if not customer_uuid:
                return _fail("Could not create the customer record.", "customer_failed")
            raw = None

        if raw:
            c = mk.parse_customer(raw)
            # Prefer the freshly-saved (named) record's uuid over a bare lookup uuid.
            customer_uuid = c["customer_uuid"] or customer_uuid
            if not vehicle_uuid and c["vehicles"]:
                vehicle_uuid = c["vehicles"][0]["vehicle_uuid"]

    if not customer_uuid and not reschedule_uuid:
        return _fail("I couldn't set up the customer record.", "missing_customer")

    # 1b. Get the vehicle UUID so it ATTACHES to the appointment.
    #     save_customer creates the vehicle but does NOT return its uuid, so the
    #     appointment used to book with an empty vehicle → "Not selected at booking"
    #     in the DMS/dispatch. The customer search DOES return vehicle uuids, so look
    #     the customer back up and grab the vehicle that matches what they told us.
    if not vehicle_uuid and req.phone and not reschedule_uuid:
        try:
            matches = await mk.search_customer(dealer, phone=req.phone)
        except mk.MyKaarmaError:
            matches = []
        # ONLY use a vehicle that belongs to the SAME customer we're booking under.
        # This phone may have duplicate customer records; a vehicle from a different
        # record fails with VEHICLE_UUID_NOT_FOUND.
        for m in matches:
            if m.get("uuid") != customer_uuid:
                continue
            for v in mk.parse_search_match(m)["vehicles"]:
                label = (v.get("label") or "").lower()
                want = " ".join(
                    str(x) for x in (req.vehicle_year, req.vehicle_make, req.vehicle_model) if x
                ).lower()
                if not want or all(w in label for w in want.split() if w):
                    vehicle_uuid = v["vehicle_uuid"]
                    break
            break

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
    # Always write the caller's transport choice into the notes. The structured
    # transportOption field needs UUIDs we can't name yet (scope pending), so
    # without this the advisor has no idea the customer said they'd be waiting.
    note = req.comments or f"Service requested: {req.service}"
    if req.transport:
        note = f"{note} | Transport: {req.transport}"

    booked_time = None
    result = None
    last_err = None
    is_reschedule = bool(reschedule_uuid)
    transport_uuid = await mk.resolve_transport(dealer, req.transport)
    for cand in candidate_times(start, count=16):
        try:
            if is_reschedule:
                # Move the ONE existing appointment in place — no duplicate.
                result = await mk.update_appointment(
                    dealer,
                    reschedule_uuid,
                    start=cand,
                    vehicle_uuid=vehicle_uuid,
                    vin=req.vin,
                    service_op=op,
                    transport_option=transport_uuid,
                    comments=note,
                )
            else:
                result = await mk.create_appointment(
                    dealer,
                    customer_uuid=customer_uuid,
                    vehicle_uuid=vehicle_uuid,
                    vin=req.vin,
                    start=cand,
                    service_op=op,
                    phone=req.phone,
                    email=req.email,
                    comments=note,
                    transport_option=transport_uuid,
                )
            booked_time = cand
            break
        except mk.MyKaarmaError as e:
            last_err = e
            body = e.body or ""
            if "SLOT_UNAVAILABLE" in body or "NO_TIME_INTERVAL" in body:
                continue  # that slot is full — try the next one
            if "VEHICLE_UUID_NOT_FOUND" in body and vehicle_uuid:
                # The vehicle didn't belong to this customer — book WITHOUT it rather
                # than failing the whole appointment. Better a booking with no vehicle
                # attached than no booking at all.
                log.warning("vehicle %s rejected; retrying without it", vehicle_uuid)
                vehicle_uuid = None
                continue
            log.error("booking failed (non-slot error): %s", e)
            return _fail("The appointment could not be booked.", "booking_failed")

    if not booked_time:
        log.error("no open slot found near %s: %s", start, last_err)
        return _fail(
            "I couldn't find an open time near then. Let me have an advisor call you back.",
            "no_open_slot",
        )

    spoken = _speak_datetime(booked_time)
    verb = "rescheduled to" if is_reschedule else "booked for"
    log.info(
        "%s %s for customer %s",
        "RESCHEDULED" if is_reschedule else "BOOKED",
        booked_time,
        customer_uuid,
    )

    return {
        "success": True,
        "appointment_time": booked_time,
        "spoken_time": spoken,
        "requested_time": start,
        "customer_uuid": customer_uuid,
        "vehicle_uuid": vehicle_uuid,
        "rescheduled": is_reschedule,
        "agent_instruction": (
            f"The appointment is {verb} {spoken}. Tell the customer: "
            f"'You're all set for {spoken}.' If that's different from what they asked, "
            "briefly mention it was the closest opening. Then let them know a "
            "confirmation is on the way."
        ),
        "mykaarma": result,
    }


# ─────────────────────────────────────────────────────────────
# REVIEW SYNC — poll myKaarma for closed ROs, push each to a GHL webhook
# so GHL can fire its review-request workflow. Call this on a schedule
# (e.g. an external cron / Railway cron hitting it every 15 minutes).
# ─────────────────────────────────────────────────────────────
@router.post("/sync-reviews")
async def sync_reviews(dealer_key: Optional[str] = None, webhook_url: Optional[str] = None):
    import os
    import review_sync

    try:
        dealer = get_dealer(dealer_key)
    except DealerNotConfigured as e:
        return _fail(str(e), "not_configured")

    url = webhook_url or os.getenv("GHL_REVIEW_WEBHOOK_URL")
    if not url:
        return _fail("No GHL review webhook URL configured.", "no_webhook")

    try:
        result = await review_sync.sync_closed_ros(dealer, url)
    except mk.MyKaarmaError as e:
        log.error("review sync failed: %s", e)
        return _fail("Could not read closed repair orders.", "order_search_failed")

    return {"success": True, **result}


# ─────────────────────────────────────────────────────────────
# Utility: refresh the cached opcode catalogue
# ─────────────────────────────────────────────────────────────
@router.post("/refresh-opcodes")
async def refresh_opcodes(dealer_key: Optional[str] = None):
    dealer = get_dealer(dealer_key)
    catalog = await mk.get_opcodes(dealer, force=True)
    return {"cached": len(catalog), "services": sorted(catalog.keys())[:50]}
