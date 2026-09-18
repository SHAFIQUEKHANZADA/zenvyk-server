"""
Service drive trade equity mining.

Reid's design, from his own walkthrough:

    customer arrives for service
        -> text them about their trade value WHILE THEY ARE THERE
        -> "yes"  -> tell them what happens next
        -> "want to look at options while you're here?"
        -> "yes"  -> SALESPERSON ALERT fires, they walk over in the waiting room

The salesperson alert deliberately fires on the SECOND yes, not on check-in.
Reid was explicit about that: we don't send someone over on equity data alone
before the customer has opted in.

WHAT WE CAN'T DO, AND WHY
-------------------------
Reid's mock texts the customer a dollar figure ("$28k equity"). Equity is
market value minus payoff:

    payoff        -> DealerBuilt. Reid: "no DealerBuilt for now."
    market value  -> vAuto / KBB / Black Book / MMR. No API access confirmed.

We have neither, so this build never states a number. The customer is told a
number will be ready when they arrive, and the salesperson gives it in person.
That is also the safer wording — several states regulate who may call something
an "appraisal", so the copy here says "estimated trade value" throughout.

When a valuation feed appears, the only thing that changes is that
`_onsite_message()` gains a figure. Nothing else in this file moves.

WHAT HOLDS STATE
----------------
GHL owns the funnel (tags + opportunity stages). This service is stateless
apart from live salesperson claims, which are in-process and expire in minutes
-- a claim only matters while the customer is physically on site. A Railway
restart drops open claims; the cost is two salespeople could approach the same
customer once, which is the same risk as before any of this existed.

Endpoints:
    POST /mykaarma/equity-screen     eligible? + the on-site text to send
    POST /mykaarma/equity-response   customer answered -> next step / fire alert
    POST /mykaarma/equity-claim      salesperson claims / presents / closes
    GET  /mykaarma/equity-claims     what's live on the board right now
"""

import logging
import os
import re
import time
from datetime import datetime
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter
from pydantic import BaseModel, Field

import mykaarma_client as mk
from config import DEALERS, DEFAULT_DEALER_KEY, get_dealer, DealerNotConfigured

log = logging.getLogger("mykaarma.equity")
router = APIRouter(prefix="/mykaarma", tags=["Equity Mining"])

# Miles a typical retail customer covers in a year — used to judge whether this
# car is low- or high-mileage for its age.
MILES_PER_YEAR = 12_000

# Age window where a service customer most often has real positive equity.
AGE_SWEET_SPOT = (2, 7)

# Reid: "we need to make sure this excludes purchases from past 12 months."
RECENT_PURCHASE_MONTHS = 12

# Someone who said "not today" gets left alone for a quarter. Without this, a
# customer servicing every few months gets re-pitched on every single visit.
DECLINE_COOLDOWN_DAYS = 90

# A lease inside this window is the strongest signal available — the customer
# has to do something about the car, and soon.
LEASE_HOT_MONTHS = 6

# How long a salesperson's claim holds before the lead falls back to the BDC.
# Reid's gap list: "if unclaimed, auto-route to next available or BDC fallback."
CLAIM_TIMEOUT_SECONDS = 5 * 60

HOT, WARM, COLD = "hot", "warm", "cold"

# All McGrath stores are in Illinois.
DEALER_TZ = ZoneInfo("America/Chicago")


# ─────────────────────────────────────────────────────────────────────────────
# PUSHING BACK INTO GHL
#
# GHL's workflow Condition action cannot read a webhook's RESPONSE — the value
# picker only offers contact fields, trigger data and account custom values
# (checked in the St. Charles sub-account, 18 Sep 2026). So a single workflow
# can't ask us "is this customer eligible?" and branch on the answer.
#
# Instead we invert it: the server decides, and only when the answer is yes does
# it POST to a GHL Inbound Webhook trigger. Everything the text needs rides in
# that payload, where GHL exposes it under "Workflow trigger" — which the value
# picker DOES offer.
#
# The URL is per store and lives in the environment, not in config.py: it is a
# credential in all but name, and keeping it out of the repo means adding a
# store never touches committed code.
#
#     EQUITY_WEBHOOK_MCGRATH_HONDA_STCHARLES=https://services.leadconnectorhq.com/...
#     EQUITY_WEBHOOK_DEFAULT=...        (fallback for any store without its own)
# ─────────────────────────────────────────────────────────────────────────────
def _equity_webhook_url(dealer_key: Optional[str], kind: str = "") -> str:
    """
    Which GHL Inbound Webhook to poke, per store and per purpose.

        kind ""      -> the opening text while they're in service
        kind "Q2"    -> the follow-up question after they say yes
        kind "ALERT" -> the salesperson alert

    Three separate GHL workflows rather than one with branches: GHL conditions
    on trigger data are exactly where this got stuck before, and a workflow with
    no branch in it cannot be wired up wrong.
    """
    part = f"_{kind.upper()}" if kind else ""
    key = (dealer_key or DEFAULT_DEALER_KEY).upper()
    return (os.getenv(f"EQUITY_WEBHOOK{part}_{key}")
            or os.getenv(f"EQUITY_WEBHOOK{part}_DEFAULT")
            or "").strip()


async def _push_to_ghl(dealer_key: Optional[str], payload: dict,
                       kind: str = "") -> dict:
    """
    Hand the eligible customer to GHL. Never raises: a push that fails must not
    turn into a 500 on the workflow's webhook step, or GHL marks the whole
    action failed and the contact silently drops out of the flow.
    """
    url = _equity_webhook_url(dealer_key, kind)
    if not url:
        log.warning("no EQUITY_WEBHOOK%s_* set for %s — nothing pushed",
                    f"_{kind.upper()}" if kind else "", dealer_key)
        return {"pushed": False, "reason": "no_webhook_configured"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as c:
            r = await c.post(url, json=payload)
        ok = r.status_code < 400
        if not ok:
            log.warning("GHL push failed [%s] %s", r.status_code, r.text[:200])
        return {"pushed": ok, "status": r.status_code}
    except Exception as e:                      # noqa: BLE001 - never propagate
        log.warning("GHL push errored: %s", e)
        return {"pushed": False, "reason": str(e)[:200]}


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────
class ScreenRequest(BaseModel):
    dealer_key: Optional[str] = None
    phone: Optional[str] = None
    customer_uuid: Optional[str] = None
    first_name: Optional[str] = None

    vehicle_year: Optional[str] = None
    vehicle_make: Optional[str] = None
    vehicle_model: Optional[str] = None
    vin: Optional[str] = None
    mileage: Optional[str] = None

    appointment_time: Optional[str] = None
    # "Tuesday" / "Tuesday morning" — how the text refers to their visit.
    appointment_day: Optional[str] = None

    # GHL passes these from the contact record. Both are exclusions.
    last_purchase_date: Optional[str] = None   # YYYY-MM-DD
    last_declined_date: Optional[str] = None   # YYYY-MM-DD
    is_lease: Optional[bool] = None
    lease_months_remaining: Optional[int] = None

    # Already being worked by another campaign (Travis, Esther outbound).
    # Reid's gap list: nothing today stops two campaigns hitting one contact.
    in_active_campaign: Optional[bool] = None


class ResponseRequest(BaseModel):
    dealer_key: Optional[str] = None
    phone: Optional[str] = None
    first_name: Optional[str] = None
    vehicle_year: Optional[str] = None
    vehicle_make: Optional[str] = None
    vehicle_model: Optional[str] = None
    mileage: Optional[str] = None
    appointment_time: Optional[str] = None
    appointment_day: Optional[str] = None

    # Which question they just answered, and what they said.
    # Which question they just answered. GHL doesn't have to work this out --
    # it can send the contact's tags instead and we infer it, which saves a
    # Condition step in the workflow. Conditions are exactly where this build
    # kept getting stuck, so the fewer of them the better.
    step: Optional[str] = Field(None, description="value_offer | see_options")
    # GHL may send this as "a, b" or ["a", "b"], or not at all.
    tags: Optional[object] = None
    answer: Optional[str] = None      # free text: "yes", "sure", "no thanks", "STOP"

    is_lease: Optional[bool] = None
    lease_months_remaining: Optional[int] = None
    channel: str = Field(default="sms", description="sms | voice")


class ClaimRequest(BaseModel):
    dealer_key: Optional[str] = None
    phone: str
    salesperson: Optional[str] = None
    # claim  -> I'm walking over
    # release-> I can't take it after all
    # presented -> I showed them options   (Reid's accountability funnel)
    # sold   -> it became a deal
    action: str = Field(default="claim", description="claim | release | presented | sold")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _year(value) -> Optional[int]:
    if value is None:
        return None
    m = re.search(r"(19|20)\d{2}", str(value))
    return int(m.group(0)) if m else None


def _age(model_year: Optional[int]) -> Optional[int]:
    if not model_year:
        return None
    # Model years run ahead of the calendar, so a 2027 sold in 2026 is age 0.
    return max(0, datetime.now().year - model_year)


def _parse_mileage(value) -> Optional[int]:
    """'84,000' / 'about 84k' / 84000 -> 84000."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).lower().replace(",", "").strip()
    m = re.search(r"(\d+(?:\.\d+)?)\s*k\b", text)
    if m:
        return int(float(m.group(1)) * 1000)
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _split_label(label: Optional[str]):
    """
    '2020 Honda Accord Sport' -> (2020, 'Honda', 'Accord Sport').
    myKaarma hands vehicles back as one string; the SMS copy needs the model on
    its own. Returns (None, None, None) for anything we can't split.
    """
    if not label:
        return None, None, None
    parts = str(label).split()
    year = _year(parts[0]) if parts else None
    if year is None:
        return None, None, " ".join(parts) or None
    make = parts[1] if len(parts) > 1 else None
    model = " ".join(parts[2:]) if len(parts) > 2 else None
    return year, make, model


def _days_since(raw: Optional[str]) -> Optional[int]:
    """
    Days since a date GHL handed us, or None if there isn't one.

    GHL is loose about date custom fields — the same field comes through as
    '2026-06-20', '2026-06-20T00:00:00.000Z', '06/20/2026', or epoch
    milliseconds, depending on how it was set. A format we can't read returns
    None, which means the exclusion SILENTLY DOESN'T APPLY and a customer who
    bought last month gets the trade text. So anything non-empty that fails to
    parse is logged loudly rather than shrugged off.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    # Epoch milliseconds (or seconds) — GHL sends these from some field types.
    if re.fullmatch(r"\d{10}|\d{13}", text):
        stamp = int(text)
        seconds = stamp / 1000 if len(text) == 13 else stamp
        return (datetime.now() - datetime.fromtimestamp(seconds)).days

    # Trim a trailing Z or +00:00 so fromisoformat takes it on any Python.
    iso = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", text)
    try:
        parsed = datetime.fromisoformat(iso)
        return (datetime.now() - parsed.replace(tzinfo=None)).days
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y", "%d/%m/%Y",
                "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y"):
        try:
            return (datetime.now() - datetime.strptime(iso[:19], fmt)).days
        except ValueError:
            continue

    log.warning(
        "could not read date %r — exclusion NOT applied for this contact", text
    )
    return None


def _is_yes(answer: Optional[str]) -> Optional[bool]:
    """
    True / False / None for "they replied something we can't read".
    Checked against how people actually reply to a text, not a form.
    """
    if answer is None:
        return None
    s = str(answer).strip().lower()
    if not s:
        return None
    if re.search(r"\b(stop|unsubscribe|opt ?out|quit|remove)\b", s):
        return False
    # "no thanks", "not now", "not interested", "nope", "n"
    if re.search(r"\b(no|nope|nah|not (now|today|interested|right now)|pass|later)\b", s):
        return False
    if re.search(r"\b(y|ya|yes|yeah|yep|yup|sure|ok|okay|please|absolutely|"
                 r"definitely|sounds good|go ahead|i guess|why not)\b", s):
        return True
    return None


def _vehicle_label(req) -> str:
    return " ".join(
        str(x) for x in (req.vehicle_year, req.vehicle_make, req.vehicle_model) if x
    ).strip()


def _opted_out(answer: Optional[str]) -> bool:
    return bool(answer) and bool(
        re.search(r"\b(stop|unsubscribe|opt ?out|quit|remove)\b", str(answer).lower())
    )


# ─────────────────────────────────────────────────────────────────────────────
# Priority — which customers the desk should walk to first.
#
# This is NOT equity and NOT a valuation. It is a ranking, so that when three
# people say yes in the same hour the salesperson knows who to see first.
# ─────────────────────────────────────────────────────────────────────────────
def _priority(req) -> dict:
    score = 0
    reasons: List[str] = []

    age = _age(_year(req.vehicle_year))
    miles = _parse_mileage(req.mileage)

    if age is not None:
        low, high = AGE_SWEET_SPOT
        if low <= age <= high:
            score += 30
            reasons.append(f"{age} years old — prime trade age")
        elif age < low:
            score += 12
            reasons.append(f"Only {age} years old — likely still upside down")
        elif age <= high + 2:
            score += 15
            reasons.append(f"{age} years old — older, still worth a number")
        else:
            reasons.append(f"{age} years old — limited book value")

    if miles is not None and age:
        expected = age * MILES_PER_YEAR
        ratio = miles / expected if expected else 1.0
        if ratio < 0.7:
            score += 35
            reasons.append(f"{miles:,} miles — well under average for its age")
        elif ratio < 0.9:
            score += 26
            reasons.append(f"{miles:,} miles — below average for its age")
        elif ratio <= 1.1:
            score += 16
            reasons.append(f"{miles:,} miles — about average")
        elif ratio <= 1.4:
            score += 7
            reasons.append(f"{miles:,} miles — above average")
        else:
            reasons.append(f"{miles:,} miles — high for its age")
    elif miles is not None:
        score += 12
        reasons.append(f"{miles:,} miles")
    elif age is not None:
        # We usually DON'T know the mileage: myKaarma doesn't return it on the
        # customer record and GHL has no field for it. Treating unknown as zero
        # scored almost every real customer "cold" and made the band useless --
        # a 5-year-old Accord came back 30/100. Unknown means unknown, so assume
        # typical mileage for the age rather than the worst case, and say so on
        # the card so the salesperson knows the number is a guess.
        score += 16
        reasons.append("Mileage unknown — assumed average for its age")

    if req.is_lease:
        score += 20
        reasons.append("Leased")
        if req.lease_months_remaining is not None and \
                req.lease_months_remaining <= LEASE_HOT_MONTHS:
            score += 25
            reasons.append(
                f"Lease matures in ~{req.lease_months_remaining} months — has to act"
            )

    score = max(0, min(100, score))
    band = HOT if score >= 70 else WARM if score >= 45 else COLD
    return {"score": score, "band": band, "reasons": reasons, "age": age,
            "miles": miles}


# ─────────────────────────────────────────────────────────────────────────────
# The customer-facing copy.
#
# Wording rules baked in deliberately:
#   * "estimated trade value", never "appraisal" — several states regulate who
#     may call a number an appraisal, and this is an estimate.
#   * no dollar figure — we have no valuation source, and a made-up number in
#     front of a customer is the one thing we genuinely cannot do.
#   * opt-out on the first message, because this is a MARKETING text, not the
#     transactional appointment confirmation it rides behind.
# ─────────────────────────────────────────────────────────────────────────────
# Characters that are ordinary in prose but force an SMS out of the GSM-7
# alphabet and into UCS-2 — which cuts the per-segment limit from 160 characters
# to 70. Measured on the pre-arrival text: one em dash turned a 2-segment
# message into a 4-segment one, doubling the carrier cost of every send. At
# seven stores' worth of service appointments that is real money for a dash
# nobody reads.
_GSM_SWAPS = {
    "—": "-", "–": "-",            # em / en dash
    "’": "'", "‘": "'",            # curly apostrophes
    "“": '"', "”": '"',            # curly quotes
    "…": "...", "→": "->",
    " ": " ",                            # non-breaking space
}


def _sms_safe(text: Optional[str]) -> Optional[str]:
    """Keep outbound text inside GSM-7 so it bills as 160-character segments."""
    if not text:
        return text
    for bad, good in _GSM_SWAPS.items():
        text = text.replace(bad, good)
    return text


def _plural_model(model: Optional[str]) -> str:
    """
    'CR-V' -> 'CR-Vs'. The text reads as a person wrote it, so it names the
    MODEL, not the whole '2022 Honda CR-V' label — saying the full label twice
    in two sentences is the tell that a machine sent it.
    """
    m = (model or "").strip()
    if not m:
        return "vehicles like yours"
    if m.lower().endswith(("s", "x", "z", "ch", "sh")):
        return f"{m}s"  # Lexus -> Lexuss reads wrong, but no McGrath brand hits this
    return f"{m}s"


def _onsite_message(first_name: Optional[str], model: Optional[str],
                    store: Optional[str] = None) -> str:
    """
    The opening text, sent WHILE the customer is at the dealership.

    This used to go out 24-48h ahead, which quietly broke the whole thing: the
    customer said yes from their sofa, the salesperson alert fired a day early,
    and there was nobody in the lounge to walk over to. Reid's own demo opens
    "While you're in service today" for exactly that reason.

    Says "estimated trade value", never "appraisal" — his demo used the latter,
    and several states regulate who may call a number an appraisal.
    """
    name = (first_name or "").strip()
    who = f"it's {store}. " if store else ""
    hi = f"Hi {name}, " if name else "Hi, "
    return _sms_safe(
        f"{hi}{who}While you're in for service today - used "
        f"{_plural_model(model)} are in short supply and yours may be worth more "
        f"than you'd expect. Want us to put a free estimated trade value on it "
        f"while you wait? No obligation either way. Reply STOP to opt out."
    )


SEE_OPTIONS_MESSAGE = (
    "Great - we'll get that number put together for you now. While you're "
    "waiting, would you like to see what your options look like? No pressure, "
    "just a look."
)

CONFIRM_MESSAGE = (
    "Perfect. Someone from our sales team will come find you in the lounge with "
    "your numbers. See you soon!"
)

DECLINE_MESSAGE = (
    "No problem at all - we'll see you at your service appointment."
)

VALUE_ONLY_MESSAGE = (
    "Sounds good - we'll have that number ready for you. Just ask your service "
    "advisor before you head out if you'd like to see it."
)


# ─────────────────────────────────────────────────────────────────────────────
# WHICH QUESTION IS THIS REPLY ANSWERING?
#
# GHL's "Customer replied" webhook can send the message body, but this account's
# value picker offers no merge field for the contact's tags — so the workflow
# cannot tell us whether the reply in hand answers question one or question two.
#
# So we remember it ourselves. When we push the second question out, we note
# that this phone number now owes us an answer to it. The whole exchange happens
# while the customer sits in the service lounge, so a short memory is enough, and
# a Railway restart mid-conversation just drops them back to question one — they
# get asked if they want a value again, which is harmless.
#
# If GHL ever does hand us tags, those win: they are the real state.
# ─────────────────────────────────────────────────────────────────────────────
_AWAITING: Dict[str, float] = {}
AWAITING_TTL_SECONDS = 4 * 60 * 60      # one service visit, generously


def _expect_second_answer(phone: Optional[str]) -> None:
    if phone:
        _AWAITING[phone] = time.time()


def _resolve_step(phone: Optional[str], tags=None) -> str:
    # Tags, if GHL gave us any, in whatever shape it sent them.
    if tags:
        text = ", ".join(tags) if isinstance(tags, (list, tuple)) else str(tags)
        if "equity-value-yes" in text.lower():
            return "see_options"

    started = _AWAITING.get(phone or "")
    if started and time.time() - started < AWAITING_TTL_SECONDS:
        return "see_options"
    if started:
        _AWAITING.pop(phone or "", None)    # stale, start again
    return "value_offer"


# ─────────────────────────────────────────────────────────────────────────────
# Live claims board.
#
# Keyed by phone. Short-lived on purpose: a claim only matters while the
# customer is on site. Nothing here is a system of record — GHL is.
# ─────────────────────────────────────────────────────────────────────────────
_CLAIMS: Dict[str, dict] = {}


def _clean_claims() -> None:
    now = time.time()
    for key, claim in list(_CLAIMS.items()):
        if claim.get("status") == "claimed" and \
                now - claim["at"] > CLAIM_TIMEOUT_SECONDS:
            claim["status"] = "timed_out"
            log.info("claim on %s timed out — falling back to BDC", key)
        # Anything finished or stale for an hour drops off the board.
        if now - claim["at"] > 3600:
            _CLAIMS.pop(key, None)


# ─────────────────────────────────────────────────────────────────────────────
# POST /mykaarma/equity-screen
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/equity-screen")
async def equity_screen(req: ScreenRequest):
    """
    Runs when a service appointment is booked. Says whether this customer should
    get the pre-arrival text at all, and hands back the exact message to send.
    """
    label = _vehicle_label(req)

    # One myKaarma round trip fills in whatever GHL didn't send: the vehicle,
    # the first name, and — the important one — WHEN the appointment actually
    # is. GHL fires this workflow the moment the appointment is booked, which
    # can be days ahead of the visit, and the text says "while you're in for
    # service today". Without the real appointment time it would go out on the
    # day they booked. myKaarma is the only place that time exists.
    if req.phone or req.customer_uuid:
        try:
            dealer = get_dealer(req.dealer_key)
            matches = await mk.search_customer(dealer, phone=req.phone)
            if matches:
                c = mk.parse_search_match(matches[0])
                if not req.appointment_time and c.get("customer_uuid"):
                    try:
                        appts = await mk.get_customer_appointments(
                            dealer, c["customer_uuid"])
                        now_local = datetime.now(DEALER_TZ).replace(tzinfo=None)
                        upcoming = mk.upcoming_appointments(appts, now_local)
                        if upcoming:
                            req.appointment_time = upcoming[0].get("start_time")
                    except mk.MyKaarmaError as e:
                        log.warning("appointment read failed: %s", e)
                if not label and c["vehicles"]:
                    label = c["vehicles"][0]["label"]
                    # Split it out, not just the year. The text names the MODEL
                    # ("Used CR-Vs are in short supply"), so without this every
                    # myKaarma-sourced customer gets the generic "vehicles like
                    # yours" wording instead.
                    y, make, model = _split_label(label)
                    req.vehicle_year = str(y or "")
                    req.vehicle_make = req.vehicle_make or make
                    req.vehicle_model = req.vehicle_model or model
                if not req.first_name:
                    req.first_name = c.get("first_name")
        except (DealerNotConfigured, mk.MyKaarmaError) as e:
            log.warning("equity-screen lookup failed (%s)", e)

    def blocked(reason: str, tag: str):
        log.info("equity-screen SKIP %s — %s", req.phone, reason)
        return {"eligible": False, "reason": reason, "vehicle": label or None,
                "message": None, "tags": [tag]}

    # ── Exclusions, in the order Reid listed them ────────────────────────────
    bought_days = _days_since(req.last_purchase_date)
    if bought_days is not None and bought_days < RECENT_PURCHASE_MONTHS * 30:
        return blocked(
            f"Bought from us {bought_days} days ago — inside the "
            f"{RECENT_PURCHASE_MONTHS}-month exclusion",
            "equity-skip-recent-purchase",
        )

    declined_days = _days_since(req.last_declined_date)
    if declined_days is not None and declined_days < DECLINE_COOLDOWN_DAYS:
        return blocked(
            f"Said no {declined_days} days ago — inside the "
            f"{DECLINE_COOLDOWN_DAYS}-day cooldown",
            "equity-skip-cooldown",
        )

    if req.in_active_campaign:
        # Reid's gap list: nothing stops Travis and this hitting one contact.
        return blocked("Already in another campaign", "equity-skip-collision")

    age = _age(_year(req.vehicle_year))
    if age is not None and age > AGE_SWEET_SPOT[1] + 5:
        return blocked(f"Vehicle is {age} years old — limited book value",
                       "equity-skip-old-vehicle")

    pri = _priority(req)
    store = (DEALERS.get(req.dealer_key or DEFAULT_DEALER_KEY) or {}).get("name")
    message = _onsite_message(req.first_name, req.vehicle_model, store)

    # Hand it straight to GHL — see the PUSHING BACK INTO GHL note above for why
    # the workflow can't just read this response and branch on it.
    push = await _push_to_ghl(req.dealer_key, {
        "phone": req.phone,
        "first_name": req.first_name,
        "vehicle": label,
        "equity_message": message,
        "equity_priority_band": pri["band"],
        "equity_priority_score": pri["score"],
        "equity_priority_reasons": "; ".join(pri["reasons"]),
        "appointment_day": req.appointment_day,
        "appointment_time": req.appointment_time,
        "send_at": req.appointment_time,
    })

    return {
        "eligible": True,
        "vehicle": label or None,
        "vehicle_age": pri["age"],
        "priority_score": pri["score"],
        "priority_band": pri["band"],
        "priority_reasons": pri["reasons"],
        "message": message,
        "tags": ["equity-eligible", f"equity-{pri['band']}"],
        "send_when": "when the customer arrives for their service appointment",
        "ghl": push,
    }


# ─────────────────────────────────────────────────────────────────────────────
# POST /mykaarma/equity-response
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/equity-response")
async def equity_response(req: ResponseRequest):
    """
    The customer replied. Returns what to send back, and — on the second yes —
    tells GHL to fire the salesperson alert.

    Reid was specific that the alert waits for the SECOND yes. Wanting to know
    what the car is worth is not the same as wanting to be approached.
    """
    label = _vehicle_label(req)
    yes = _is_yes(req.answer)
    pri = _priority(req)

    # Work out which question this reply answers, if GHL didn't say. The tag
    # equity-value-yes is added when they say yes to the first one, so its
    # presence means the reply in hand is the answer to the second.
    step = req.step or _resolve_step(req.phone, req.tags)

    if _opted_out(req.answer):
        log.info("equity opt-out from %s", req.phone)
        return {
            "step": step, "answer": "opt_out", "next_message": None,
            "fire_salesperson_alert": False,
            "tags": ["equity-opted-out"],
            "note": "Honour STOP store-wide, not just in this workflow.",
        }

    # ── Question 1: do you want to know what it's worth? ─────────────────────
    if step == "value_offer":
        if yes is True:
            _expect_second_answer(req.phone)
            push = await _push_to_ghl(req.dealer_key, {
                "phone": req.phone,
                "first_name": req.first_name,
                "equity_message": SEE_OPTIONS_MESSAGE,
            }, kind="Q2")
            return {
                "step": step, "answer": "yes",
                "next_message": SEE_OPTIONS_MESSAGE,
                "next_step": "see_options",
                "fire_salesperson_alert": False,
                "tags": ["equity-value-yes"],
                "ghl": push,
            }
        if yes is False:
            return {
                "step": step, "answer": "no",
                "next_message": DECLINE_MESSAGE,
                "fire_salesperson_alert": False,
                "tags": ["equity-declined"],
                "note": (f"Suppress for {DECLINE_COOLDOWN_DAYS} days. Set "
                         f"last_declined_date on the contact."),
            }
        return {
            "step": step, "answer": "unclear",
            "next_message": None, "fire_salesperson_alert": False,
            "tags": ["equity-reply-unclear"],
            "note": "Reply wasn't a clear yes or no — route to a human, don't guess.",
        }

    # ── Question 2: want to look at options while you're here? ───────────────
    if step == "see_options":
        _AWAITING.pop(req.phone or "", None)
        if yes is True:
            _clean_claims()
            if req.phone:
                _CLAIMS[req.phone] = {
                    "phone": req.phone,
                    "name": req.first_name or "Customer",
                    "vehicle": label or "their vehicle",
                    "dealer_key": req.dealer_key,
                    "appointment_time": req.appointment_time,
                    "priority_score": pri["score"],
                    "priority_band": pri["band"],
                    "reasons": pri["reasons"],
                    "status": "unclaimed",
                    "salesperson": None,
                    "at": time.time(),
                }
            alert = [
                f"{req.first_name or 'Customer'} — {label or 'vehicle'}",
                f"In for service: {req.appointment_time or 'today'}",
                "Wants to see options — walk over now",
                f"Priority {pri['score']}/100 ({pri['band'].upper()})",
            ] + [f"- {r}" for r in pri["reasons"]]
            log.info("equity alert %s %s priority=%s", req.first_name, label,
                     pri["score"])
            card = _sms_safe("\n".join(alert))
            push = await _push_to_ghl(req.dealer_key, {
                "phone": req.phone,
                "first_name": req.first_name,
                "equity_message": CONFIRM_MESSAGE,
                "alert_card": card,
                "vehicle": label,
                "equity_priority_band": pri["band"],
                "equity_priority_score": pri["score"],
                "appointment_time": req.appointment_time,
            }, kind="ALERT")
            return {
                "step": step, "answer": "yes",
                "next_message": CONFIRM_MESSAGE,
                "fire_salesperson_alert": True,
                # The desk gets this as an SMS too, so it takes the same
                # GSM-7 treatment as the customer copy.
                "alert_card": _sms_safe("\n".join(alert)),
                "priority_score": pri["score"],
                "priority_band": pri["band"],
                "claim_timeout_seconds": CLAIM_TIMEOUT_SECONDS,
                "tags": ["equity-wants-options", "equity-alert-sent"],
                "note": (
                    "Do NOT state a value in any message. The salesperson gives "
                    "the estimated range in person. If nobody claims within "
                    f"{CLAIM_TIMEOUT_SECONDS // 60} minutes, fall back to the BDC."
                ),
            }
        if yes is False:
            return {
                "step": step, "answer": "no",
                "next_message": VALUE_ONLY_MESSAGE,
                "fire_salesperson_alert": False,
                "tags": ["equity-value-yes", "equity-options-no"],
                "note": ("They still want the number — have the advisor hand it "
                         "over. Do not send a salesperson to the lounge."),
            }
        return {
            "step": step, "answer": "unclear", "next_message": None,
            "fire_salesperson_alert": False,
            "tags": ["equity-reply-unclear"],
        }

    return {"error": "unknown_step", "step": step,
            "note": "step must be 'value_offer' or 'see_options'"}


# ─────────────────────────────────────────────────────────────────────────────
# POST /mykaarma/equity-claim
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/equity-claim")
async def equity_claim(req: ClaimRequest):
    """
    The salesperson side of Reid's alert card. Claiming locks the lead so two
    people don't walk up to the same customer in the lounge.
    """
    _clean_claims()
    claim = _CLAIMS.get(req.phone)
    if not claim:
        return {"success": False, "error": "not_found",
                "message": "No live equity alert for that number."}

    if req.action == "claim":
        if claim["status"] == "claimed" and claim["salesperson"] != req.salesperson:
            return {
                "success": False, "error": "already_claimed",
                "claimed_by": claim["salesperson"],
                "message": f"{claim['salesperson']} already claimed this one.",
            }
        claim.update(status="claimed", salesperson=req.salesperson, at=time.time())
        return {"success": True, "status": "claimed",
                "message": f"{claim['name']} is yours — {claim['vehicle']}.",
                "expires_in_seconds": CLAIM_TIMEOUT_SECONDS,
                "tags": ["equity-claimed"]}

    if req.action == "release":
        claim.update(status="unclaimed", salesperson=None, at=time.time())
        return {"success": True, "status": "unclaimed",
                "message": "Released — back on the board.",
                "tags": ["equity-released"]}

    if req.action == "presented":
        # Reid's accountability funnel needs this, and there is no automatic
        # signal that a conversation happened on the lot — it has to be logged.
        claim.update(status="presented", at=time.time())
        return {"success": True, "status": "presented",
                "message": "Logged — options presented.",
                "tags": ["equity-presented"]}

    if req.action == "sold":
        claim.update(status="sold", at=time.time())
        return {"success": True, "status": "sold",
                "message": "Logged — deal.",
                "tags": ["equity-sold"]}

    return {"success": False, "error": "unknown_action",
            "message": "action must be claim, release, presented or sold"}


# ─────────────────────────────────────────────────────────────────────────────
# GET /mykaarma/equity-claims
# ─────────────────────────────────────────────────────────────────────────────
@router.get("/equity-claims")
async def equity_claims(dealer_key: Optional[str] = None):
    """
    What's live on the board right now, highest priority first. Feeds the
    salesperson dashboard. In-process only — see the module docstring.
    """
    _clean_claims()
    rows = [c for c in _CLAIMS.values()
            if not dealer_key or c.get("dealer_key") == dealer_key]
    rows.sort(key=lambda c: (-c["priority_score"], c["at"]))
    return {
        "count": len(rows),
        "unclaimed": sum(1 for c in rows if c["status"] == "unclaimed"),
        "timed_out": sum(1 for c in rows if c["status"] == "timed_out"),
        "claims": [
            {k: v for k, v in c.items() if k != "at"} | {
                "waiting_seconds": int(time.time() - c["at"])
            } for c in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# PER-STORE ROUTES — matches the pattern the booking endpoints already use.
# ─────────────────────────────────────────────────────────────────────────────
@router.post("/{dealer_key}/equity-screen")
async def equity_screen_by_path(dealer_key: str, req: ScreenRequest):
    req.dealer_key = dealer_key
    return await equity_screen(req)


@router.post("/{dealer_key}/equity-response")
async def equity_response_by_path(dealer_key: str, req: ResponseRequest):
    req.dealer_key = dealer_key
    return await equity_response(req)


@router.post("/{dealer_key}/equity-claim")
async def equity_claim_by_path(dealer_key: str, req: ClaimRequest):
    req.dealer_key = dealer_key
    return await equity_claim(req)
