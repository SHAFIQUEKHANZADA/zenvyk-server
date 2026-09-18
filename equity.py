"""
Service drive trade equity mining.

Reid's design, from his own walkthrough:

    customer books service
        -> 24-48h before the visit, text them about their trade value
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
`_pre_arrival_message()` gains a figure. Nothing else in this file moves.

WHAT HOLDS STATE
----------------
GHL owns the funnel (tags + opportunity stages). This service is stateless
apart from live salesperson claims, which are in-process and expire in minutes
-- a claim only matters while the customer is physically on site. A Railway
restart drops open claims; the cost is two salespeople could approach the same
customer once, which is the same risk as before any of this existed.

Endpoints:
    POST /mykaarma/equity-screen     eligible? + the pre-arrival text to send
    POST /mykaarma/equity-response   customer answered -> next step / fire alert
    POST /mykaarma/equity-claim      salesperson claims / presents / closes
    GET  /mykaarma/equity-claims     what's live on the board right now
"""

import logging
import re
import time
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

import mykaarma_client as mk
from config import get_dealer, DealerNotConfigured

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
    step: str = Field(..., description="value_offer | see_options")
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


def _days_since(iso_date: Optional[str]) -> Optional[int]:
    if not iso_date:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%m/%d/%Y"):
        try:
            return (datetime.now() - datetime.strptime(str(iso_date)[:19], fmt)).days
        except ValueError:
            continue
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


def _pre_arrival_message(first_name: Optional[str], model: Optional[str],
                         day: Optional[str]) -> str:
    name = (first_name or "").strip()
    hi = f"Hi {name} — " if name else "Hi — "
    visit = f" visit {day}" if day else " visit"
    return (
        f"{hi}quick one before your service{visit}. Used {_plural_model(model)} "
        f"are in short supply right now and yours may be worth more than you'd "
        f"expect. Want us to have an estimated trade value ready when you're in? "
        f"No obligation either way. Reply STOP to opt out."
    )


SEE_OPTIONS_MESSAGE = (
    "Great — we'll have an estimated trade value ready for you at your visit. "
    "While you're waiting, would you like to see what your options look like? "
    "No pressure, just a look."
)

CONFIRM_MESSAGE = (
    "Perfect. Someone from our sales team will come find you in the lounge with "
    "your numbers. See you soon!"
)

DECLINE_MESSAGE = (
    "No problem at all — we'll see you at your service appointment."
)

VALUE_ONLY_MESSAGE = (
    "Sounds good — we'll have an estimated trade value ready for you at your "
    "visit. Just ask your service advisor if you'd like to see it."
)


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

    # Fill the vehicle from myKaarma if GHL didn't have it.
    if not label and (req.phone or req.customer_uuid):
        try:
            dealer = get_dealer(req.dealer_key)
            matches = await mk.search_customer(dealer, phone=req.phone)
            if matches:
                c = mk.parse_search_match(matches[0])
                if c["vehicles"]:
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
    return {
        "eligible": True,
        "vehicle": label or None,
        "vehicle_age": pri["age"],
        "priority_score": pri["score"],
        "priority_band": pri["band"],
        "priority_reasons": pri["reasons"],
        "message": _pre_arrival_message(req.first_name, req.vehicle_model,
                                        req.appointment_day),
        "tags": ["equity-eligible", f"equity-{pri['band']}"],
        "send_when": "24-48 hours before the service appointment",
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

    if _opted_out(req.answer):
        log.info("equity opt-out from %s", req.phone)
        return {
            "step": req.step, "answer": "opt_out", "next_message": None,
            "fire_salesperson_alert": False,
            "tags": ["equity-opted-out"],
            "note": "Honour STOP store-wide, not just in this workflow.",
        }

    # ── Question 1: do you want to know what it's worth? ─────────────────────
    if req.step == "value_offer":
        if yes is True:
            return {
                "step": req.step, "answer": "yes",
                "next_message": SEE_OPTIONS_MESSAGE,
                "next_step": "see_options",
                "fire_salesperson_alert": False,
                "tags": ["equity-value-yes"],
            }
        if yes is False:
            return {
                "step": req.step, "answer": "no",
                "next_message": DECLINE_MESSAGE,
                "fire_salesperson_alert": False,
                "tags": ["equity-declined"],
                "note": (f"Suppress for {DECLINE_COOLDOWN_DAYS} days. Set "
                         f"last_declined_date on the contact."),
            }
        return {
            "step": req.step, "answer": "unclear",
            "next_message": None, "fire_salesperson_alert": False,
            "tags": ["equity-reply-unclear"],
            "note": "Reply wasn't a clear yes or no — route to a human, don't guess.",
        }

    # ── Question 2: want to look at options while you're here? ───────────────
    if req.step == "see_options":
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
            return {
                "step": req.step, "answer": "yes",
                "next_message": CONFIRM_MESSAGE,
                "fire_salesperson_alert": True,
                "alert_card": "\n".join(alert),
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
                "step": req.step, "answer": "no",
                "next_message": VALUE_ONLY_MESSAGE,
                "fire_salesperson_alert": False,
                "tags": ["equity-value-yes", "equity-options-no"],
                "note": ("They still want the number — have the advisor hand it "
                         "over. Do not send a salesperson to the lounge."),
            }
        return {
            "step": req.step, "answer": "unclear", "next_message": None,
            "fire_salesperson_alert": False,
            "tags": ["equity-reply-unclear"],
        }

    return {"error": "unknown_step", "step": req.step,
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
