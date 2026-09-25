"""
The endpoints the GHL Voice AI agent calls as Custom Actions.

  POST /mykaarma/lookup-customer    -> who is calling, what do they drive
  POST /mykaarma/get-slots          -> REAL open appointment times
  POST /mykaarma/book-appointment   -> create (or reschedule) the appointment
  POST /mykaarma/cancel-appointment -> cancel the caller's open appointment

Design rule: the voice agent must never have to think. Each endpoint takes
simple inputs and returns simple, speakable outputs. All the UUID juggling,
JSON parsing and opcode mapping happens here, in code.
"""

import asyncio
import time
from contextvars import ContextVar
import logging
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from pydantic import BaseModel, Field

import mykaarma_client as mk
from config import DEALERS, MAX_SLOTS, get_dealer, DealerNotConfigured

log = logging.getLogger("mykaarma.routes")
router = APIRouter(prefix="/mykaarma", tags=["myKaarma"])

TRANSFER_NUMBER = "630-797-4570"

# The store THIS request is for, so a failure hands the caller to that store's own
# line. TRANSFER_NUMBER used to be the only number for every store, so an Acura
# Libertyville caller whose lookup or booking failed was told to transfer to a
# 630 (St. Charles) line. Set at the top of each handler; per-request safe.
_REQUEST_DEALER: ContextVar[Optional[str]] = ContextVar("request_dealer", default=None)


def _transfer_number() -> str:
    """This request's store transfer line; the old shared number if none is set."""
    store = DEALERS.get(_REQUEST_DEALER.get() or "") or {}
    return store.get("transfer_number") or TRANSFER_NUMBER
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

    # "day after tomorrow" CONTAINS the substring "tomorrow", so it must be
    # matched FIRST — otherwise it falls through to the tomorrow branch and books
    # one day too early (Reid call, Shafi's 2017 Civic: asked day-after-tomorrow,
    # got booked tomorrow).
    if "day after tomorrow" in low or "day after next" in low or "overmorrow" in low:
        return (today + timedelta(days=2)).isoformat()
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
    6: None,      # Sunday    CLOSED — the service department does not open
}

# We never OFFER a 3 AM appointment even on a 24-hour day — myKaarma's own
# configured hours cap this anyway, and nobody wants a call offering 2 AM.
SPEAKABLE_START = 8
SPEAKABLE_END = 17

# Never offer a slot that starts sooner than this many minutes from now (dealer-
# local). Without this, a same-day call in the evening is still offered that
# morning's times — myKaarma returns the whole day's grid regardless of the clock.
SLOT_LEAD_MINUTES = 30

# ── ADVISOR CUTOFF (LEARNED) ────────────────────────────────────────────────
# myKaarma's availability API tells us which slots are FULL. It never tells us
# which hours a service advisor is actually on the drive, and it reports store
# hours as 00:00:00–23:44:59, so we cannot read the real cutoff from anywhere.
# The result: we offered a caller 4:00 PM on a Monday at St. Charles, she said
# yes, and create_appointment came back NO_SA_AVAILABLE — "No Service Advisor
# Available for the day". She was transferred, never booked, and went home
# thinking she had an appointment.
#
# Since there is no field to read, we LEARN it. The first time a store refuses
# a time on a given weekday, we stop offering that time and anything later on
# that weekday. Each store teaches us its own hours, once, from a real refusal.
#
# Deliberately in memory: a redeploy simply relearns it, and the booking-failure
# recovery below still catches any cutoff we haven't met yet. Nothing breaks if
# this map is empty — it only ever REMOVES times we already know are unbookable.
_NO_ADVISOR_FROM: Dict[Tuple[str, int], str] = {}


def _advisor_cutoff(dealer_key: Optional[str], day: str) -> Optional[str]:
    """Earliest 'HH:MM:SS' known to be refused for this store on this weekday."""
    try:
        weekday = datetime.strptime(day, "%Y-%m-%d").weekday()
    except (ValueError, TypeError):
        return None
    return _NO_ADVISOR_FROM.get((dealer_key or "", weekday))


# ── EXACT TIMES A STORE HAS JUST REFUSED ──────────────────────────
# myKaarma's availability API and its booking API disagree: get_slots offers a
# time, then create/update answers SLOT_UNAVAILABLE for that very time.
#
# Measured live 2026-09-25 at Honda St. Charles. A caller asked to move her
# appointment to Monday evening, was offered 4:00 PM, was refused it, was shown
# the morning instead, said again that she wanted an evening — and get_slots
# handed back 4:00 PM, because nothing told it the booking had just been turned
# down. She was refused 4:00 PM three times in two minutes, then the call did
# the same thing on Tuesday, and she hung up without an appointment.
#
# So remember the exact slot and stop offering it.
#
# PER STORE, not per caller: if 4:00 PM Monday will not take a booking it will
# not take one from the next caller either, and they must not walk into the
# same wall.
#
# Keyed on the exact date+time with a SHORT ttl, deliberately NOT as an hour
# cutoff. Probed the same store with an empty schedule and 4:00 PM and 4:30 PM
# both booked without complaint, so the hour is fine — that afternoon was
# simply full. Real capacity moves: a cancellation reopens the time, so we must
# not blacklist an hour the store genuinely sells.
_REFUSED_SLOTS: Dict[Tuple[str, str], float] = {}
_REFUSED_SLOT_TTL = 1800.0   # 30 minutes


def _remember_refused(dealer_key: Optional[str], iso: str) -> None:
    now = time.monotonic()
    for k, seen in list(_REFUSED_SLOTS.items()):
        if now - seen > _REFUSED_SLOT_TTL:
            _REFUSED_SLOTS.pop(k, None)
    _REFUSED_SLOTS[(dealer_key or "", iso)] = now
    log.info("%s refused %s — not offering it again for 30 min", dealer_key, iso)


def _drop_refused(slots: List[str], dealer_key: Optional[str]) -> List[str]:
    """Remove times this store refused a booking for in the last half hour."""
    now = time.monotonic()
    kept = []
    for s in slots:
        seen = _REFUSED_SLOTS.get((dealer_key or "", s))
        if seen is not None and now - seen <= _REFUSED_SLOT_TTL:
            continue
        kept.append(s)
    return kept


def _remember_no_advisor(dealer_key: Optional[str], iso: str) -> None:
    """Record a refusal so we never offer that time (or later) on this weekday."""
    try:
        when = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return
    key = (dealer_key or "", when.weekday())
    clock = when.strftime("%H:%M:%S")
    if clock < _NO_ADVISOR_FROM.get(key, "99:99:99"):
        _NO_ADVISOR_FROM[key] = clock
        log.warning(
            "LEARNED: %s has no advisor from %s on %s — no longer offering it",
            dealer_key, clock, when.strftime("%A"),
        )


def _drop_after_cutoff(slots: List[str], dealer_key: Optional[str]) -> List[str]:
    """Remove slots at/after a cutoff we've already been refused on that weekday."""
    kept = []
    for s in slots:
        cutoff = _advisor_cutoff(dealer_key, s[:10])
        if cutoff and s[11:] >= cutoff:
            continue
        kept.append(s)
    return kept


def is_closed(dt: datetime) -> bool:
    return DEALER_HOURS.get(dt.weekday(), (8, 17)) is None


def day_hours(dt: datetime) -> tuple:
    """Bookable (open_hour, close_hour) for that weekday, clamped to sane times.
    A closed day returns a zero-width window, so _clamp_business rolls past it."""
    hours = DEALER_HOURS.get(dt.weekday(), (8, 17))
    if hours is None:
        return SPEAKABLE_START, SPEAKABLE_START
    open_h, close_h = hours
    return max(open_h, SPEAKABLE_START), min(close_h, SPEAKABLE_END)


def next_open_day(dt: datetime) -> datetime:
    """Roll a date forward to the next day the shop is actually open."""
    for _ in range(8):
        if not is_closed(dt):
            return dt
        dt = dt + timedelta(days=1)
    return dt


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
    # Caller's TIME-of-day preference, in their own words: "12 PM", "after 2",
    # "before 11", "morning", "afternoon", "evening". We return slots that match it.
    time: Optional[str] = Field(None, description="e.g. '12 PM', 'after 2', 'morning', 'evening'")
    customer_uuid: Optional[str] = None
    vehicle_uuid: Optional[str] = None
    # RESCHEDULE: if the customer is updating an existing appointment, include the
    # appointment UUID so myKaarma returns the correct reschedule slots instead of
    # behaving like a new booking.
    existing_appointment_uuid: Optional[str] = None
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


class CancelRequest(BaseModel):
    # Everything is optional: the agent may know the UUID from lookup-customer, or
    # it may only have the caller's phone. We resolve the appointment server-side
    # either way (same safety pattern as book_appointment's reschedule lookup) so a
    # forgotten/garbled UUID never cancels the wrong thing — or nothing at all.
    appointment_uuid: Optional[str] = Field(
        None, description="From lookup-customer's existing_appointment_uuid"
    )
    phone: Optional[str] = None
    customer_uuid: Optional[str] = None
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


def _build_appointment_note(comments: Optional[str], service: str, transport: Optional[str]) -> str:
    """Assemble the appointment note. Multiple customer concerns are listed line-by-
    line ("line out the concerns" per Reid) so the advisor/tech sees each one clearly,
    instead of everything mashed into a single sentence. The agent separates concerns
    with semicolons or new lines; we split on those and itemise them as bullets."""
    lines: List[str] = []
    raw = (comments or "").strip()
    if raw:
        parts = [
            seg.strip(" -•\t")
            for seg in raw.replace(";", "\n").split("\n")
            if seg.strip(" -•\t")
        ]
        if len(parts) > 1:
            lines.append("Customer concerns:")
            lines.extend(f"- {p}" for p in parts)
        elif parts:
            lines.append(f"Customer concern: {parts[0]}")
    else:
        lines.append(f"Service requested: {service}")
    if transport:
        lines.append(f"Transport: {transport}")
    return "\n".join(lines)


def _fail(message: str, error: str = "error", **extra):
    """Any failure MUST tell the agent to hand off to a human. Never leave a caller stranded."""
    number = _transfer_number()
    payload = {
        "success": False,
        "error": error,
        "message": message,
        "transfer_to": number,
        "agent_instruction": (
            "Apologize, tell the customer you'll connect them with a service advisor, "
            f"and transfer the call to {number}."
        ),
    }
    payload.update(extra)
    return payload


# ─────────────────────────────────────────────────────────────
# 1. LOOKUP CUSTOMER  — agent calls this at the START of the call
# ─────────────────────────────────────────────────────────────
@router.post("/lookup-customer")
async def lookup_customer(req: LookupRequest):
    _REQUEST_DEALER.set(req.dealer_key)
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
            "has_existing_appointment": False,
            "agent_instruction": (
                "No customer record found, and NO appointment on file for this "
                "number. If they asked to reschedule or cancel one, say plainly: "
                "\"I'm not seeing an appointment under this number.\" Then ask for "
                "their name and the year, make and model of the vehicle. Never tell "
                "the caller that details or information did not come back."
            ),
        }

    if not matches:
        return {
            "found": False,
            "customer_uuid": None,
            "first_name": None,
            "last_name": None,
            "vehicles": [],
            "has_existing_appointment": False,
            "agent_instruction": (
                "No customer record found, and NO appointment on file for this "
                "number. If they asked to reschedule or cancel one, say plainly: "
                "\"I'm not seeing an appointment under this number.\" Then ask for "
                "their name and the year, make and model of the vehicle. Never tell "
                "the caller that details or information did not come back."
            ),
        }

    c = mk.parse_search_match(matches[0])
    # Cap the list HERE, where it gets spoken and handed to the agent. Booking
    # needs the full list to find the car the caller named.
    c["vehicles"] = c["vehicles"][:mk.MAX_VEHICLES]
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
        # ONE vehicle, the newest. Reading the whole list out loud is what made
        # callers ask for a human ("I see a 2018 Honda Pilot or a 2006 Honda
        # Pilot on file. Which one?"). parse_search_match already sorted these
        # newest first, so [0] is the one to offer.
        newest = c["vehicles"][0]["label"]
        others = [v["label"] for v in c["vehicles"][1:]]
        instruction = (
            f"Greet {c['first_name']} by name and confirm ONE vehicle only — the "
            f"newest: 'I see a {newest} on file — is that the vehicle you're "
            f"bringing in?' Do NOT read out a list of vehicles."
        )
        if others:
            instruction += (
                " Only if they say it is not that one, offer the rest one at a "
                f"time, newest first: {', '.join(others)}. If none of them match, "
                "ask for the year, make and model."
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

    # NOTHING ON FILE — SAY SO, IF THEY ASKED TO MOVE ONE.
    #
    # Measured live 2026-09-25: a caller opened with "reschedule my appointment",
    # had no upcoming appointment on file, and because this instruction said
    # nothing at all about that case the model improvised — "I can help
    # reschedule, but I don't see the appointment details yet", then "I don't have
    # your appointment time and the details that came back". It narrated our API
    # at the customer and never recovered. Whether there is an appointment is
    # something only this response knows, so it has to say so outright.
    if not existing:
        instruction += (
            " There is NO upcoming appointment on file for this number. If they "
            "ask to reschedule, move, or cancel one, say plainly: \"I'm not seeing "
            "an appointment on file for this number.\" Then offer to book one. "
            "Never tell the caller that details or information did not come back."
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


def _looks_like_uuid(v: Optional[str]) -> bool:
    """A real myKaarma appointment UUID is a long token (~43 chars, no spaces).
    The voice model sometimes maps the spoken day ("today"/"tomorrow") into the
    existing_appointment_uuid field — reject those so we never send garbage."""
    return bool(v) and len(v) >= 20 and " " not in v


def _spoken_hhmm(pref: str) -> Optional[int]:
    """Spoken clock time -> minutes past midnight. "5 PM" -> 1020. Returns None when
    the phrase names no clock time at all ("morning", "whenever")."""
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?", pref)
    if not m:
        return None
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    ap = (m.group(3) or "").replace(".", "")
    if ap == "pm" and hh != 12:
        hh += 12
    elif ap == "am" and hh == 12:
        hh = 0
    elif not ap and 1 <= hh <= 7:      # bare "2" at a dealership almost always means 2 PM
        hh += 12
    return hh * 60 + mm


# Roughly where each spoken phrase points. Used ONLY to order the fallback list
# once the preference has already matched nothing -- never to decide what is
# bookable.
_PREF_ANCHOR = {"morning": 9 * 60, "noon": 12 * 60, "midday": 12 * 60,
                "afternoon": 14 * 60, "evening": 17 * 60, "night": 17 * 60}


def _boundary_phrase(day_slots: List[str], pref: Optional[str]) -> str:
    """How to describe the closest openings to a caller whose time we don't have.

    "the latest I have that day is 4:30" is something a caller can act on.
    Saying that to someone who asked for 8 AM is nonsense, so which end of the
    day they ran off decides the wording.
    """
    if not day_slots:
        return ""
    p = (pref or "").lower()
    mins_of = lambda s: int(s[11:13]) * 60 + int(s[14:16])
    clock = lambda s: _speak_time(s).split(" at ")[-1]   # "4:30 PM", not the full date
    target = _spoken_hhmm(p)
    if target is None:
        for word, hour in _PREF_ANCHOR.items():
            if word in p:
                target = hour
                break
    if target is None:
        return ""
    if target > mins_of(day_slots[-1]):
        return f"the latest I have that day is {clock(day_slots[-1])}"
    if target < mins_of(day_slots[0]):
        return f"the earliest I have that day is {clock(day_slots[0])}"
    # They asked for a time inside the day's range that simply isn't open. There
    # is no boundary to quote -- the offered times speak for themselves.
    return ""


def _nearest_to_pref(slots: List[str], pref: Optional[str], limit: int) -> List[str]:
    """The `limit` openings CLOSEST to what the caller asked for, chronological.

    This exists because of a loop the stores reported. The caller asks for 5 PM,
    the last opening is 4:30, and the fallback handed back the first three slots
    of the day -- 10:00, 10:30, 11:00. The caller says "no, something later", the
    agent searches again, gets the same three, says unavailable again, and the
    call goes round until someone hangs up.

    Offering the END of the day instead makes the answer useful even when it is
    no: "the latest I have is 4:30" is a decision the caller can act on.
    """
    if not pref or len(slots) <= limit:
        return slots[:limit]
    p = pref.lower()
    mins_of = lambda s: int(s[11:13]) * 60 + int(s[14:16])
    target = _spoken_hhmm(p)
    if target is None:
        neg = ("not" in p) or ("n't" in p) or ("avoid" in p) or ("except" in p)
        for word, hour in _PREF_ANCHOR.items():
            if word in p:
                # "not the morning" points at the other end of the day, not 9 AM.
                target = ((mins_of(slots[-1]) if hour < 12 * 60 else mins_of(slots[0]))
                          if neg else hour)
                break
    if target is None:
        return slots[:limit]
    return sorted(sorted(slots, key=lambda s: abs(mins_of(s) - target))[:limit])


def _filter_by_time_pref(slots: List[str], pref: Optional[str]) -> List[str]:
    """Filter ISO slot strings by the caller's spoken time-of-day preference:
      keywords: 'morning' / 'afternoon' / 'evening' / 'noon'
      relative: 'after 2', 'before 11', 'past noon'
      specific: '12 PM', '2:30', 'around 3'
    Returns the matching slots (chronological). Empty pref → unchanged. A given
    pref that matches nothing → [] (caller-day fallback is handled in get_slots)."""
    if not pref or not slots:
        return slots
    p = pref.lower()
    hh_of = lambda s: int(s[11:13])
    mins_of = lambda s: int(s[11:13]) * 60 + int(s[14:16])

    # "not the morning", "can't do evenings", "avoid afternoon" → invert the range.
    neg = ("not" in p) or ("n't" in p) or ("avoid" in p) or ("except" in p)

    if "morning" in p:
        return [s for s in slots if hh_of(s) >= 12] if neg else [s for s in slots if hh_of(s) < 12]
    if "evening" in p or "night" in p:
        return [s for s in slots if hh_of(s) < 16] if neg else [s for s in slots if hh_of(s) >= 16]
    if "afternoon" in p:
        return ([s for s in slots if not (12 <= hh_of(s) < 17)] if neg
                else [s for s in slots if 12 <= hh_of(s) < 17])
    if "noon" in p or "midday" in p or "mid day" in p or "mid-day" in p:
        return [s for s in slots if 11 <= hh_of(s) <= 13]

    target = _spoken_hhmm(p)
    if target is None:
        return slots

    if any(w in p for w in ("before", "earlier", "by")):
        return [s for s in slots if mins_of(s) <= target]
    # "after", "past", "from", or a bare specific time → that time and the next few
    return [s for s in slots if mins_of(s) >= target]


# ─────────────────────────────────────────────────────────────
# 2. GET SLOTS  — agent calls this once it knows the service + the day
# ─────────────────────────────────────────────────────────────
@router.post("/get-slots")
async def get_slots(req: SlotsRequest):
    _REQUEST_DEALER.set(req.dealer_key)
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

    # If the caller asked for a day we're closed (Sunday), quote the next open day
    # instead of returning an empty schedule.
    dates = [
        next_open_day(datetime.strptime(d, "%Y-%m-%d")).strftime("%Y-%m-%d")
        for d in dates
    ]

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

    # myKaarma cert (reschedule): when this caller already has an upcoming appointment,
    # myKaarma needs the REAL existingAppointmentUuid on the fetch-slots call so it
    # returns reschedule slots instead of treating it as a fresh booking. Resolve it
    # SERVER-SIDE from the customer's own appointment — NOT from req.existing_appointment_uuid,
    # because the voice model sometimes maps the spoken day ("today") into that field.
    # The agent's value is only a last-resort fallback, and only if it looks like a UUID.
    existing_appt_uuid = None
    if req.customer_uuid:
        try:
            _now = datetime.now(DEALER_TZ).replace(tzinfo=None)
            _existing = mk.upcoming_appointments(
                await mk.get_customer_appointments(dealer, req.customer_uuid), _now
            )
            if _existing:
                existing_appt_uuid = _existing[0]["appointment_uuid"]
        except mk.MyKaarmaError as e:
            log.warning("get_slots reschedule lookup failed: %s", e)
    if not existing_appt_uuid and _looks_like_uuid(req.existing_appointment_uuid):
        existing_appt_uuid = req.existing_appointment_uuid
    if req.existing_appointment_uuid and not _looks_like_uuid(req.existing_appointment_uuid):
        log.info("ignored malformed existing_appointment_uuid from agent: %r",
                 req.existing_appointment_uuid)
    log.info("get_slots existingAppointmentUuid -> %s", existing_appt_uuid)

    try:
        slots = await mk.get_availability(
            dealer,
            dates=dates,
            customer_uuid=req.customer_uuid,
            vehicle_uuid=req.vehicle_uuid,
            operation_uuid=op["uuid"] if op else None,
            transport_option_uuid=transport_uuid,
            existing_appointment_uuid=existing_appt_uuid,
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
    slots = _drop_after_cutoff(slots, req.dealer_key)
    slots = _drop_refused(slots, req.dealer_key)

    # Honour the caller's time-of-day preference ("12 PM", "after 2", "evening"…).
    # Keep the day's raw openings so we can fall back if the preference matches nothing.
    day_fallback = list(slots)
    slots = _filter_by_time_pref(slots, req.time)

    # Nothing on the requested day? Don't dead-end the call — look ahead and offer
    # the next day that HAS openings. Callers routinely ask "when's the first
    # available?", and a same-day request late in the day always comes back empty.
    searched_ahead = False

    # The caller's TIME isn't open, but their DAY is. Offer that day's real
    # openings before considering any other day — the day they asked for is the
    # stronger preference, and silently moving them to another day is what we
    # are trying to stop. Only a day with nothing at all falls through to the
    # look-ahead below.
    time_pref_missed = False
    if not slots and day_fallback:
        slots = day_fallback
        time_pref_missed = bool(req.time)

    if not slots:
        probe = datetime.strptime(dates[0], "%Y-%m-%d")
        # First look-ahead day that has ANY openings, kept aside in case the
        # caller's TIME matches nothing for a fortnight. Without this, "5 PM" on
        # a fully-booked day walked all 14 probes and told the caller there was
        # nothing for two weeks -- when every one of those days was open.
        any_day: Optional[Tuple[List[str], str]] = None
        for _ in range(14):  # up to two weeks out
            probe += timedelta(days=1)
            if is_closed(probe):
                continue
            nxt = probe.strftime("%Y-%m-%d")
            # Same bookable window as the requested day. This call used to omit it, so
            # look-ahead days fell back to get_availability's 08:00-19:00 default and
            # offered 5:00 and 5:30 PM, past SPEAKABLE_END with no advisor on shift. An
            # Acura Libertyville caller asking for "today" was told 5 PM tomorrow was
            # "the earliest"; the booking was refused and they were moved a day later.
            ahead_open, ahead_close = day_hours(probe)
            try:
                found = await mk.get_availability(
                    dealer,
                    dates=[nxt],
                    customer_uuid=req.customer_uuid,
                    vehicle_uuid=req.vehicle_uuid,
                    operation_uuid=op["uuid"] if op else None,
                    transport_option_uuid=transport_uuid,
                    existing_appointment_uuid=existing_appt_uuid,
                    start_time=f"{ahead_open:02d}:00:00",
                    end_time=f"{ahead_close:02d}:00:00",
                )
            except mk.MyKaarmaError:
                continue
            found = _drop_after_cutoff(found, req.dealer_key)
            found = _drop_refused(found, req.dealer_key)
            if found and any_day is None:
                any_day = (list(found), nxt)
            found = _filter_by_time_pref(found, req.time)
            if found:
                slots, dates, searched_ahead = found, [nxt], True
                break
        else:
            if any_day:
                slots, dates = any_day[0], [any_day[1]]
                searched_ahead, time_pref_missed = True, bool(req.time)

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

    # When the caller's time missed, hand back the openings nearest what they
    # asked for. slots[:MAX_SLOTS] would give the first three of the day, which
    # is how a 5 PM request ended up being offered 10 AM.
    top = (_nearest_to_pref(slots, req.time, MAX_SLOTS) if time_pref_missed
           else slots[:MAX_SLOTS])
    spoken_day = datetime.strptime(dates[0], "%Y-%m-%d").strftime("%A, %B %d").replace(" 0", " ")

    _boundary = _boundary_phrase(slots, req.time) if time_pref_missed else ""

    no_repeat = (
        " Searching this day again for a different time returns this SAME list,"
        " so do NOT call get_slots again for another time on this day. If none of"
        " these work, ask what OTHER DAY suits them and search that day instead."
    )

    if searched_ahead and time_pref_missed:
        instruction = (
            f"The day the customer asked for has no openings, and the TIME they asked "
            f"for isn't open on {spoken_day} either. Tell them both plainly, then offer "
            f"ONLY the times in 'spoken_slots' — these are the closest we have to what "
            f"they wanted. Do NOT invent a time and do NOT transfer." + no_repeat
        )
    elif searched_ahead:
        instruction = (
            f"The day the customer asked for has no more available times (fully booked, "
            f"or it's already too late in the day). The next day with openings is "
            f"{spoken_day}. Let them know that day isn't available, then offer ONLY the "
            f"times in 'spoken_slots' for {spoken_day}. Do NOT invent times. Do NOT "
            "transfer — keep helping. Once they choose, call book_appointment with the "
            "exact matching value from 'slots'."
        )
    elif time_pref_missed:
        instruction = (
            f"The time the customer asked for is NOT available on {spoken_day}. Say "
            f"so plainly first — \"I don't have anything then\" — then offer ONLY the "
            f"times in 'spoken_slots'. These are the openings CLOSEST to what they "
            f"asked for. Do NOT agree to the "
            f"time they asked for and do NOT invent another one. Once they choose, "
            f"call book_appointment with the exact matching value from 'slots'."
            + (f" Give them the real boundary in your own words: "
               f"\"{_boundary}\"." if _boundary else "")
            + no_repeat
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
        "requested_time_unavailable": time_pref_missed,
        "slots": top,                                   # ISO — send one of these back to /book
        "spoken_slots": [_speak_time(s) for s in top],  # what the agent reads out
        "operation_uuid": op["uuid"] if op else None,
        "agent_instruction": instruction,
        # diagnostic: the appointment UUID actually sent to myKaarma on this fetch-slots
        # call (resolved server-side). Confirms reschedules send a real, well-formed UUID
        # regardless of what the voice model passed. Safe to remove after certification.
        "existing_appointment_uuid_sent": existing_appt_uuid,
    }


# ─────────────────────────────────────────────────────────────
# 3. BOOK APPOINTMENT  — agent calls this after the customer picks a time
# ─────────────────────────────────────────────────────────────
async def _slot_taken(
    req: "BookRequest",
    wanted: str,
    customer_uuid: Optional[str],
    vehicle_uuid: Optional[str],
    *,
    no_advisor: bool = False,
) -> dict:
    """The caller's chosen time was refused at booking. Hand back what's actually open
    and let THEM pick — booking a different time on their behalf is how callers ended up
    on another day. Nothing is booked when this returns.

    no_advisor=True means myKaarma answered NO_SA_AVAILABLE: the time isn't a real
    opening at all (nobody is on the service drive then), rather than a slot someone
    else grabbed first. Same recovery, but we must not tell the caller it "was taken"."""
    spoken_wanted = _speak_datetime(wanted)
    _remember_refused(req.dealer_key, wanted)
    slots: List[str] = []
    spoken: List[str] = []
    try:
        alt = await get_slots(SlotsRequest(
            service=req.service,
            dates=[wanted[:10]],
            customer_uuid=customer_uuid,
            vehicle_uuid=vehicle_uuid,
            transport=req.transport,
            dealer_key=req.dealer_key,
        ))
        for raw, say in zip(alt.get("slots") or [], alt.get("spoken_slots") or []):
            # get_slots already drops anything this store just refused.
            if raw != wanted:
                slots.append(raw)
                spoken.append(say)
    except Exception as e:  # never let the fallback lookup break the response
        log.warning("alternatives lookup failed after slot rejection: %s", e)

    gone = "isn't an available time" if no_advisor else "was taken before we could book it"
    say_gone = (
        "we don't have an opening then" if no_advisor else "that time was just taken"
    )
    if spoken:
        instruction = (
            f"NOTHING IS BOOKED. {spoken_wanted} {gone}. Do NOT book any other time on "
            f"your own. Tell the caller: \"I'm sorry — {say_gone}. I do have "
            f"{' or '.join(spoken)}. Which would you prefer?\" Then call "
            f"book_appointment again with the exact time they choose."
        )
    else:
        instruction = (
            f"NOTHING IS BOOKED. {spoken_wanted} {gone}, and every other time that day "
            f"has already been tried. STOP offering times on that day. Do NOT book "
            f"another time on your own. Tell the caller: \"I'm sorry — that day "
            f"isn't working. What other day would suit you?\" Then call get_slots for "
            f"the day they name."
        )

    return {
        "success": False,
        "booked": False,
        "slot_taken": True,
        "requested_time": wanted,
        "spoken_requested": spoken_wanted,
        "slots": slots,
        "spoken_slots": spoken,
        "agent_instruction": instruction,
    }


def _squash(text: str) -> str:
    """Lowercase and strip everything that isn't a letter or a digit.

    myKaarma writes the model as "CR-V" while the voice model hands us "CRV" or
    "CR V", so a literal comparison said the caller's own car was not on file.
    Squashing both sides makes all three the same string."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _vehicle_matches(label: str, year, make, model) -> bool:
    """Is `label` the car the caller described?

    Every part the caller named has to appear. A caller who said "2024 Acura MDX"
    must NOT match a 2023 Honda Pilot — booking someone onto the wrong car is
    worse than booking them onto none."""
    hay = _squash(label)
    parts = [_squash(str(x)) for x in (year, make, model) if x]
    return bool(parts) and all(part and part in hay for part in parts)


# asyncio only keeps a WEAK reference to a running task, so a fire-and-forget
# create_task() can be garbage-collected before it finishes. Hold the reference
# until it is done.
_BACKGROUND_TASKS: set = set()


async def _attach_stated_vehicle(
    dealer: Dict[str, str],
    appointment_uuid: str,
    customer_uuid: str,
    phone: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    year: Optional[str],
    make: Optional[str],
    model: Optional[str],
) -> None:
    """Put the car the caller named ON the customer, then ON the appointment.

    A caller who says "no, it's my 2018 Honda Accord" has told us what they are
    bringing in. Until now we refused to attach anything that wasn't already on
    file and wrote it into the notes instead, so the appointment reached the
    drive as "Vehicle TBD" — which is exactly what the store complained about.

    Measured live 2026-09-26 at Honda St. Charles:
      * save_customer with the phone MATCHES the existing record and adds the
        vehicle to it — it does not create a second customer.
      * the new vehicle takes about 8 seconds to become searchable.
      * sending year/make/model on the appointment itself is IGNORED: myKaarma
        substitutes its "No Vehicle Selected" placeholder.
      * attaching it afterwards with a vehicle-only PATCH works and leaves the
        service list intact.

    That 8 seconds is why this runs AFTER we have answered. Adding it to the
    booking call would have left the caller listening to silence on top of the
    time the booking already takes. If any of it fails the appointment still
    stands and the note still names the car, so the advisor is no worse off."""
    # SAVE UNDER THE NAME THE RECORD ALREADY CARRIES, NOT THE ONE JUST SPOKEN.
    #
    # myKaarma matches duplicates on name AND phone, so save_customer only adds
    # the vehicle to an existing record when the name matches it too. Measured
    # live 2026-09-26: a caller booked under a record stored as "Server Test",
    # gave a different name on the call, and myKaarma created a SECOND record in
    # that name and put the vehicle there. The appointment stayed on the first
    # record with no vehicle, and the poll below searched for a car that was
    # never going to appear.
    #
    # So look up what this record is actually called and save under that. Only
    # fall back to the spoken name when the record has none.
    on_file_first, on_file_last = first_name, last_name
    try:
        for m in await mk.search_customer(dealer, phone=phone):
            if m.get("uuid") != customer_uuid:
                continue
            if (m.get("fname") or "").strip() or (m.get("lname") or "").strip():
                on_file_first = (m.get("fname") or "").strip() or None
                on_file_last = (m.get("lname") or "").strip() or None
            break
    except mk.MyKaarmaError as e:
        log.warning("could not read the name on %s: %s", customer_uuid, e)

    try:
        await mk.save_customer(
            dealer,
            phone=phone,
            first_name=on_file_first,
            last_name=on_file_last,
            vehicle_year=year,
            vehicle_make=make,
            vehicle_model=model,
        )
    except mk.MyKaarmaError as e:
        log.warning("could not save stated vehicle for %s: %s", customer_uuid, e)
        return

    # Poll until myKaarma indexes it. ~8s measured; allow generous headroom.
    for _ in range(12):
        await asyncio.sleep(2)
        try:
            matches = await mk.search_customer(dealer, phone=phone)
        except mk.MyKaarmaError:
            continue
        for m in matches:
            # ONLY a vehicle on the customer we actually booked under. myKaarma
            # rejects a uuid from any other record with VEHICLE_UUID_NOT_FOUND.
            if m.get("uuid") != customer_uuid:
                continue
            for v in mk.parse_search_match(m)["vehicles"]:
                if not _vehicle_matches(v.get("label"), year, make, model):
                    continue
                try:
                    await mk.update_appointment(
                        dealer, appointment_uuid, vehicle_uuid=v["vehicle_uuid"]
                    )
                    log.info(
                        "attached %s to appointment %s", v.get("label"), appointment_uuid
                    )
                except mk.MyKaarmaError as e:
                    log.warning("could not attach %s: %s", v.get("label"), e)
                return
    log.warning(
        "stated vehicle %s %s %s never became searchable for %s",
        year, make, model, customer_uuid,
    )


@router.post("/book-appointment")
async def book_appointment(req: BookRequest):
    _REQUEST_DEALER.set(req.dealer_key)
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
    # We resolve the reschedule target SERVER-SIDE (authoritative) rather than
    # trusting the agent's existing_appointment_uuid — the voice model sometimes
    # passes a truncated/blank copy, and a bad UUID makes the update fail (→ the
    # caller gets transferred). Since a customer may only have ONE appointment, we
    # look up their real upcoming appointment and update THAT. The agent's value is
    # only a fallback if we can't resolve one ourselves.
    reschedule_uuid = None
    _existing = None      # stays None if we never got to look the appointments up
    check_uuid = customer_uuid
    if not check_uuid and req.phone:
        try:
            _ms = await mk.search_customer(dealer, phone=req.phone)
            if _ms:
                check_uuid = mk.parse_search_match(_ms[0])["customer_uuid"]
        except mk.MyKaarmaError:
            check_uuid = None
    # THE PHONE SEARCH ABOVE ALREADY FOUND THIS CALLER — BOOK UNDER THAT RECORD.
    #
    # That search ran only to find an appointment to move, and its result was then
    # thrown away: customer_uuid stayed empty, so step 1 below called save_customer
    # and myKaarma minted ANOTHER record for a customer we had just located.
    # Measured live 2026-09-25: one test phone number ended up carrying FOUR
    # records, and two bookings made minutes apart landed on two different
    # brand-new ones — which is why the follow-up reschedule found nothing to move.
    # A phone match IS the caller. Use it.
    if check_uuid and not customer_uuid:
        customer_uuid = check_uuid
        log.info(
            "customer resolved by phone -> %s (no new record written)", check_uuid
        )
    if check_uuid:
        try:
            _now = datetime.now(DEALER_TZ).replace(tzinfo=None)
            _existing = mk.upcoming_appointments(
                await mk.get_customer_appointments(dealer, check_uuid), _now
            )
            if _existing:
                reschedule_uuid = _existing[0]["appointment_uuid"]
                log.info(
                    "reschedule target resolved server-side for customer %s -> %s",
                    check_uuid, reschedule_uuid,
                )
        except mk.MyKaarmaError as e:
            log.warning("reschedule lookup failed: %s", e)
    # The agent's UUID is a LAST RESORT and is only usable if it is genuinely one
    # of THIS customer's upcoming appointments.
    #
    # Measured live 2026-09-25: the voice model sent get_slots' `operation_uuid`
    # (the OPCODE — 7de0x0Jm80MgtO0dvxoeIc2P_Ye9DqLAfg96U9x5aWY) as
    # reschedule_appointment_uuid. Every myKaarma uuid is the same 43-char shape,
    # so nothing caught it, and we PATCHed an opcode as though it were an
    # appointment -> INCORRECT_APPOINTMENT, "No appointment found for given
    # appointment uuid". The caller was told the booking failed.
    #
    # If we resolved the customer's appointments above, the agent's value must
    # appear in that list. If it doesn't, it is not an appointment — drop it and
    # book fresh rather than PATCHing something arbitrary.
    # _existing is None only when we never managed to look (no customer resolved,
    # or myKaarma errored). An EMPTY list is a real answer: this customer has no
    # upcoming appointment, so whatever the agent sent cannot be one.
    if not reschedule_uuid and req.reschedule_appointment_uuid:
        if _existing is None:
            reschedule_uuid = req.reschedule_appointment_uuid
        elif req.reschedule_appointment_uuid in {
            a["appointment_uuid"] for a in _existing
        }:
            reschedule_uuid = req.reschedule_appointment_uuid
        else:
            log.warning(
                "ignoring agent reschedule_appointment_uuid %s — not an upcoming "
                "appointment on customer %s; booking fresh instead",
                req.reschedule_appointment_uuid, check_uuid,
            )

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
    # IMPORTANT: only WRITE a customer when we don't already have one. If lookup
    # already identified the caller (req.customer_uuid is set), we skip save_customer
    # entirely — that per-booking write is what spawned duplicate records, because
    # myKaarma's duplicate-matching isn't phone-only and would sometimes create a new
    # twin instead of matching. A known customer books straight under their record.
    have_details = any([
        req.first_name, req.last_name, req.email, req.vin,
        req.vehicle_year, req.vehicle_make, req.vehicle_model, req.phone,
    ])
    # Gate on the RESOLVED customer_uuid, not on req.customer_uuid. The agent
    # doesn't always send one, but a phone match above is just as good — and
    # writing over it is what produced the duplicates.
    _just_created = False
    if have_details and not reschedule_uuid and not customer_uuid:
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
            # If the agent already identified the customer (lookup handed us a uuid),
            # book under THAT record. Do NOT let save_customer's duplicate-matching
            # switch us to a different twin — that's how a "Shafique" lookup ended up
            # booking under a stale "Ronald" duplicate. Only adopt the saved record's
            # uuid/vehicle for a brand-new customer we had no uuid for.
            if not req.customer_uuid:
                customer_uuid = c["customer_uuid"] or customer_uuid
                _just_created = True
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
        want = " ".join(
            str(x) for x in (req.vehicle_year, req.vehicle_make, req.vehicle_model) if x
        ).lower()
        # myKaarma's customer search is eventually consistent: a JUST-created customer +
        # vehicle often isn't indexed for a second or two, so the first search comes back
        # empty and the appointment books with no vehicle ("Not selected"). Retry briefly
        # until the vehicle appears. ONLY use a vehicle on the SAME customer we're booking
        # under (this phone may have duplicate records — a vehicle from a different record
        # fails with VEHICLE_UUID_NOT_FOUND), and SKIP the auto-created "No Vehicle
        # Selected" placeholder — always prefer a real vehicle.
        for attempt in range(4):
            try:
                matches = await mk.search_customer(dealer, phone=req.phone)
            except mk.MyKaarmaError:
                matches = []
            for m in matches:
                if m.get("uuid") != customer_uuid:
                    continue
                real = [
                    v for v in mk.parse_search_match(m)["vehicles"]
                    if "no vehicle selected" not in (v.get("label") or "").lower()
                ]
                # prefer the vehicle matching what the caller told us
                for v in real:
                    if want and _vehicle_matches(
                        v.get("label"), req.vehicle_year, req.vehicle_make,
                        req.vehicle_model,
                    ):
                        vehicle_uuid = v["vehicle_uuid"]
                        break
                # Fall back to the first vehicle on file ONLY when the caller never
                # named one. If they did and it isn't on file, attaching a different
                # car is worse than none: a caller who said "2024 Acura MDX" was
                # booked on a 2023 Honda. myKaarma accepts an appointment with no
                # vehicle, and the stated vehicle goes into the notes below.
                if not vehicle_uuid and real and not want:
                    vehicle_uuid = real[0]["vehicle_uuid"]
                break
            # Only a customer we just CREATED needs time to index. A known caller's
            # vehicles are already searchable, so waiting would only add dead air.
            if vehicle_uuid or not _just_created:
                break
            await asyncio.sleep(1.3)  # let myKaarma index the new customer/vehicle

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
    note = _build_appointment_note(req.comments, req.service, req.transport)
    stated_vehicle = " ".join(
        str(x) for x in (req.vehicle_year, req.vehicle_make, req.vehicle_model) if x
    ).strip()
    if stated_vehicle and not vehicle_uuid:
        # The caller named a car we couldn't attach. Put it where the advisor sees it.
        note = "\n".join(x for x in (note, f"Vehicle (per caller, not on file): {stated_vehicle}") if x)

    booked_time = None
    result = None
    last_err = None
    is_reschedule = bool(reschedule_uuid)
    transport_uuid = await mk.resolve_transport(dealer, req.transport)

    # Book the time the customer actually CHOSE — never quietly substitute another one.
    # myKaarma's availability API sometimes offers a slot its booking API then rejects
    # (confirmed: get-slots returns 09:30, create/update answers SLOT_UNAVAILABLE). We
    # used to auto-advance an hour at a time from there, which silently moved callers to
    # a different time — and, when a Saturday only had a few slots, to a different DAY.
    # Now a rejected slot comes back as `slot_taken` with the real remaining options so
    # the agent can ask the caller instead of deciding for them.
    wanted = next(candidate_times(start, count=1))

    # Extra passes only ever run to DROP something myKaarma refused — a bad
    # vehicle_uuid, or a transport option the store has no advisor for.
    dropped_transport = None
    for _attempt in range(3):
        try:
            if is_reschedule:
                # Move the ONE existing appointment in place — no duplicate.
                # NEVER send service_op or a rebuilt note on an UPDATE.
                # Measured against live myKaarma 2026-09-25: a PATCH carrying
                # serviceList comes back 200 with no warning and leaves the
                # appointment with an EMPTY service list — the opcode set at
                # creation is destroyed. A PATCH with only the start time keeps
                # it. The same applies to comments: _build_appointment_note
                # always produces text, so passing it overwrote the advisor's
                # original concern note ("Customer concern: brake noise") with a
                # generic "Service requested: oil change" every time a caller
                # moved their appointment. A reschedule changes the TIME. Only
                # send what the caller actually changed.
                result = await mk.update_appointment(
                    dealer,
                    reschedule_uuid,
                    start=wanted,
                    vehicle_uuid=vehicle_uuid,
                    vin=req.vin,
                    transport_option=transport_uuid,
                    comments=note if req.comments else None,
                )
            else:
                result = await mk.create_appointment(
                    dealer,
                    customer_uuid=customer_uuid,
                    vehicle_uuid=vehicle_uuid,
                    vin=req.vin,
                    start=wanted,
                    service_op=op,
                    phone=req.phone,
                    email=req.email,
                    comments=note,
                    transport_option=transport_uuid,
                )
            booked_time = wanted
            break
        except mk.MyKaarmaError as e:
            last_err = e
            body = e.body or ""
            if "VEHICLE_UUID_NOT_FOUND" in body and vehicle_uuid:
                # The vehicle didn't belong to this customer — book WITHOUT it rather
                # than failing the whole appointment. Better a booking with no vehicle
                # attached than no booking at all. Same time, one more try.
                log.warning("vehicle %s rejected; retrying without it", vehicle_uuid)
                vehicle_uuid = None
                continue
            # NO ADVISOR FOR THE TRANSPORT THE CALLER ASKED FOR.
            #
            # Measured live 2026-09-25 at Honda St. Charles: a LOANER appointment
            # is refused NO_SA_AVAILABLE at every time of day — 9 AM, 1 PM,
            # 3 PM, 4 PM — while the very same slot books instantly with
            # shuttle, will-wait, or no transport at all. The store lists Loaner
            # as a transport option but has no advisor configured to take one,
            # and the availability API knows nothing about it: it returns an
            # identical open grid whether or not the loaner is selected.
            #
            # So a caller who wanted a loaner was walked around the schedule being
            # refused every time she picked, because each refusal looked to us like
            # a busy slot. It never was — no time of day would ever have worked.
            #
            # Book it WITHOUT the structured transport field. The note already
            # carries "Transport: loaner", so the advisor still sees the request
            # and can sort the car out. An appointment with a loaner request on it
            # beats no appointment at all — but we must NOT let the agent tell
            # the caller the loaner is confirmed, so we flag it below.
            if "NO_SA_AVAILABLE" in body and transport_uuid:
                log.warning(
                    "%s refuses %s with transport %r — rebooking without the "
                    "structured transport option; the note still carries it",
                    req.dealer_key, wanted, req.transport,
                )
                dropped_transport = req.transport
                transport_uuid = None
                continue
            if (
                "SLOT_UNAVAILABLE" in body
                or "NO_TIME_INTERVAL" in body
                or "NO_SA_AVAILABLE" in body
                # "Appointment request violates capacity planning constraints" —
                # the shop is booked out for that time. Measured live 2026-09-25:
                # this fell through to the generic failure below, so a caller who
                # picked a full slot was told the appointment "could not be booked"
                # and offered nothing. It means exactly what a taken slot means.
                or "CAPACITY_PLANNING" in body
            ):
                no_sa = "NO_SA_AVAILABLE" in body
                if no_sa:
                    _remember_no_advisor(req.dealer_key, wanted)
                log.info(
                    "slot %s rejected by myKaarma (%s) — offering alternatives",
                    wanted, "no advisor on shift" if no_sa else "slot taken",
                )
                return await _slot_taken(
                    req, wanted, customer_uuid, vehicle_uuid, no_advisor=no_sa
                )
            log.error("booking failed (non-slot error): %s", e)
            return _fail(
                "The appointment could not be booked.",
                "booking_failed",
                debug={
                    "step": "reschedule" if is_reschedule else "create",
                    "status": e.status,
                    "body": (e.body or "")[:400],
                    "reschedule_uuid": reschedule_uuid,
                    "cand": wanted,
                },
            )

    if not booked_time:
        log.error("could not book %s: %s", wanted, last_err)
        return _fail(
            "I couldn't get that time booked. Let me have an advisor call you back.",
            "booking_failed",
            debug={
                "step": "reschedule" if is_reschedule else "create",
                "start": start,
                "last_err": str(last_err)[:400] if last_err else None,
                "reschedule_uuid": reschedule_uuid,
            },
        )

    # THE CAR THE CALLER NAMED GOES ON THE APPOINTMENT, ON FILE OR NOT.
    #
    # If we could not attach a vehicle, but the caller told us exactly what they
    # are bringing in, add it to their record and attach it. This runs in the
    # background because the new vehicle takes ~8 seconds to become searchable in
    # myKaarma, and the caller is on the phone — see _attach_stated_vehicle.
    if not vehicle_uuid and stated_vehicle and customer_uuid and req.phone:
        _appt_uuid = reschedule_uuid or (result or {}).get("appointmentUuid")
        if _appt_uuid:
            _task = asyncio.create_task(_attach_stated_vehicle(
                dealer, _appt_uuid, customer_uuid, req.phone,
                req.first_name, req.last_name,
                req.vehicle_year, req.vehicle_make, req.vehicle_model,
            ))
            _BACKGROUND_TASKS.add(_task)
            _task.add_done_callback(_BACKGROUND_TASKS.discard)
        else:
            log.warning("no appointment uuid returned; cannot attach %s", stated_vehicle)

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
        "transport_confirmed": not dropped_transport,
        "agent_instruction": (
            f"The appointment is {verb} {spoken}. Tell the customer: "
            f"'You're all set for {spoken}.' If that's different from what they asked, "
            "briefly mention it was the closest opening. Then let them know a "
            "confirmation is on the way."
            + (
                f" The {dropped_transport} could NOT be reserved for this time. Do "
                f"NOT tell them a {dropped_transport} is confirmed. Say: 'I've put "
                f"a note on for a {dropped_transport} and the advisor will confirm "
                f"that with you.'"
                if dropped_transport else ""
            )
        ),
        "mykaarma": result,
    }


# ─────────────────────────────────────────────────────────────
# CANCEL AN APPOINTMENT
# The agent normally already knows the UUID (lookup-customer returns it as
# existing_appointment_uuid). But we still resolve server-side from the phone when
# it's missing or garbled, so "cancel my appointment" works even if the voice model
# never passed the UUID through. Finding nothing is a NORMAL answer, not an error.
# ─────────────────────────────────────────────────────────────
@router.post("/cancel-appointment")
async def cancel_appointment(req: CancelRequest):
    _REQUEST_DEALER.set(req.dealer_key)
    try:
        dealer = get_dealer(req.dealer_key)
    except DealerNotConfigured as e:
        return _fail(str(e), "not_configured")

    # 1) Which appointment? ALWAYS resolve this server-side first.
    #    We used to trust req.appointment_uuid whenever it "looked like" a UUID.
    #    But a myKaarma CUSTOMER uuid looks identical to an APPOINTMENT uuid (43
    #    chars, no spaces), and the voice model does hand us the customer one by
    #    mistake — it did on 11 Sep, so we PATCHed a customer uuid, myKaarma
    #    answered INCORRECT_APPOINTMENT, and the caller was transferred instead of
    #    getting their appointment cancelled. The customer's own upcoming
    #    appointment is authoritative; the agent's value is only a last resort.
    #    (Same server-side-first pattern book_appointment uses for reschedules.)
    appt_uuid = None
    appt = None

    customer_uuid = req.customer_uuid
    if not customer_uuid and req.phone:
        try:
            matches = await mk.search_customer(dealer, phone=req.phone)
        except mk.MyKaarmaError as e:
            log.error("cancel lookup failed: %s", e)
            return _fail("I couldn't pull up your appointment.", "lookup_failed")
        if matches:
            customer_uuid = mk.parse_search_match(matches[0])["customer_uuid"]

    if customer_uuid:
        try:
            appts = await mk.get_customer_appointments(dealer, customer_uuid)
            now_local = datetime.now(DEALER_TZ).replace(tzinfo=None)
            upcoming = mk.upcoming_appointments(appts, now_local)
            if upcoming:
                appt = upcoming[0]
                appt_uuid = appt["appointment_uuid"]
        except mk.MyKaarmaError as e:
            log.warning("cancel appointment read failed: %s", e)

    # Fallback: only now consider what the agent sent — and never if it is the
    # customer uuid we just resolved, which is the exact mix-up above.
    if not appt_uuid and _looks_like_uuid(req.appointment_uuid):
        if req.appointment_uuid == customer_uuid:
            log.warning(
                "agent sent the CUSTOMER uuid as appointment_uuid (%s) — ignoring",
                req.appointment_uuid,
            )
        else:
            appt_uuid = req.appointment_uuid
            log.info("cancel falling back to agent-supplied uuid %s", appt_uuid)

    # 2) Nothing on the books. Normal outcome — tell the agent to say so plainly.
    if not appt_uuid:
        return {
            "success": False,
            "cancelled": False,
            "no_appointment": True,
            "agent_instruction": (
                "There is NO open appointment on file for this caller. Say: \"I'm not "
                "seeing any open appointment on file for this number.\" Then ask if "
                "they'd like to book one. Do NOT say anything was cancelled."
            ),
        }

    # 3) Cancel it.
    try:
        await mk.cancel_appointment(dealer, appt_uuid)
    except mk.MyKaarmaError as e:
        log.error("cancel failed for %s: %s", appt_uuid, e)
        return _fail(
            "I couldn't cancel that appointment.",
            "cancel_failed",
            debug={
                "status": e.status,
                "body": (e.body or "")[:400],
                "appointment_uuid": appt_uuid,
            },
        )

    when = _speak_datetime(appt.get("start_time")) if appt else None
    when_txt = f" on {when}" if when else ""
    log.info("CANCELLED %s", appt_uuid)

    return {
        "success": True,
        "cancelled": True,
        "appointment_uuid": appt_uuid,
        "spoken_time": when,
        "agent_instruction": (
            f"The appointment{when_txt} is cancelled. Tell the caller: \"That's "
            "cancelled for you.\" Then ask if they'd like to reschedule for another day."
        ),
    }


# ─────────────────────────────────────────────────────────────
# PER-STORE ROUTES (Method B) — the store is baked into the URL path, so each
# GHL agent just points to its own endpoint and never has to send a dealer_key
# in the body. These simply set dealer_key from the path and delegate to the
# same handlers above. The original body-key routes (Method A) still work.
#   POST /mykaarma/{dealer_key}/lookup-customer
#   POST /mykaarma/{dealer_key}/get-slots
#   POST /mykaarma/{dealer_key}/book-appointment
# e.g. /mykaarma/mcgrath_honda_elgin/get-slots
# ─────────────────────────────────────────────────────────────
@router.post("/{dealer_key}/lookup-customer")
async def lookup_customer_by_path(dealer_key: str, req: LookupRequest):
    req.dealer_key = dealer_key
    return await lookup_customer(req)


@router.post("/{dealer_key}/get-slots")
async def get_slots_by_path(dealer_key: str, req: SlotsRequest):
    req.dealer_key = dealer_key
    return await get_slots(req)


@router.post("/{dealer_key}/book-appointment")
async def book_appointment_by_path(dealer_key: str, req: BookRequest):
    req.dealer_key = dealer_key
    return await book_appointment(req)


@router.post("/{dealer_key}/cancel-appointment")
async def cancel_appointment_by_path(dealer_key: str, req: CancelRequest):
    req.dealer_key = dealer_key
    return await cancel_appointment(req)


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
    # De-dupe (catalog is keyed by multiple names -> same entry) and list every
    # unique opcode with its code + description, so we can see the FULL service menu
    # (e.g. confirm B1/A16 are actually present) and build any needed mappings.
    uniq = {op["uuid"]: op for op in catalog.values()}.values()
    opcodes = sorted(
        ({"code": op.get("laborOpCode"), "name": op.get("name")} for op in uniq),
        key=lambda x: (x.get("code") or ""),
    )
    return {
        "cached_names": len(catalog),
        "unique_opcodes": len(opcodes),
        "opcodes": opcodes,
    }
