"""
Thin client for the myKaarma API.

Every function here maps to ONE myKaarma call. The router (routes.py) chains them.

Docs: https://docs.mykaarma.com
Auth: Authorization: Basic base64(username:password)
Base: https://api.mykaarma.com
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

from config import MYKAARMA_BASE_URL, DEFAULT_APPOINTMENT_MINUTES, headers

log = logging.getLogger("mykaarma.client")

TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class MyKaarmaError(Exception):
    """Raised when myKaarma returns an error. Carries the status + body."""

    def __init__(self, status: int, body: str, step: str):
        self.status = status
        self.body = body
        self.step = step
        super().__init__(f"myKaarma {step} failed [{status}]: {body[:300]}")


async def _post(url: str, dealer: Dict[str, str], payload: dict, step: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(url, headers=headers(dealer), json=payload)
    if r.status_code >= 400:
        log.error("%s -> %s %s", step, r.status_code, r.text[:500])
        raise MyKaarmaError(r.status_code, r.text, step)
    return r.json() if r.text else {}


async def _get(url: str, dealer: Dict[str, str], step: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.get(url, headers=headers(dealer))
    if r.status_code >= 400:
        log.error("%s -> %s %s", step, r.status_code, r.text[:500])
        raise MyKaarmaError(r.status_code, r.text, step)
    return r.json() if r.text else {}


# ─────────────────────────────────────────────────────────────────────────────
# 1. CUSTOMER  —  Save Customer with searchForDuplicate=true
#    Finds an existing customer OR creates one.
#
#    VERIFIED LIVE against the sandbox 2026-07-16. Notes learned the hard way:
#      * everything goes inside a "customer" object; "vehicles" is TOP level
#      * phone MUST be E.164:  +16305550147   (bare 6305550147 is rejected)
#      * phone/email objects need a "label" (CELL / HOME) or they're dropped
#      * VIN must pass the real VIN checksum or it's silently rejected
#      * the response does NOT contain vehicle UUIDs — only customerUuid.
#        Pass the VIN to the appointment call instead (myKaarma resolves it).
# ─────────────────────────────────────────────────────────────────────────────
CUSTOMER_PATH = "/customer/v2/department/{department_uuid}/customer"


async def save_customer(
    dealer: Dict[str, str],
    phone: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    email: Optional[str] = None,
    vin: Optional[str] = None,
    vehicle_year: Optional[str] = None,
    vehicle_make: Optional[str] = None,
    vehicle_model: Optional[str] = None,
) -> dict:
    url = MYKAARMA_BASE_URL + CUSTOMER_PATH.format(
        department_uuid=dealer["department_uuid"]
    )

    customer: Dict[str, Any] = {}
    if first_name:
        customer["firstName"] = first_name
    if last_name:
        customer["lastName"] = last_name
    if email:
        customer["emails"] = [
            {"emailAddress": email, "label": "HOME", "okToEmail": True, "isPreferred": True}
        ]
    if phone:
        customer["phoneNumbers"] = [
            {
                "phoneNumber": normalize_phone(phone),
                "label": "CELL",
                "okToCall": True,
                "okToText": True,
                "isPreferred": True,
            }
        ]

    payload: Dict[str, Any] = {"customer": customer, "searchForDuplicate": True}

    vehicle: Dict[str, Any] = {}
    if vin:
        vehicle["vin"] = vin
    if vehicle_year:
        vehicle["vehicleYear"] = str(vehicle_year)
    if vehicle_make:
        vehicle["vehicleMake"] = vehicle_make
    if vehicle_model:
        vehicle["vehicleModel"] = vehicle_model
    if vehicle:
        payload["vehicles"] = [vehicle]

    return await _post(url, dealer, payload, step="save_customer")


def normalize_phone(phone: str) -> str:
    """
    myKaarma rejects anything that isn't E.164 (+1XXXXXXXXXX).

    This is a US dealership, so a bare 10-digit number is assumed to be US.
    We deliberately do NOT slap a "+" on anything else: an earlier version turned
    "03464365890" into "+03464365890", which is not a valid E.164 number. myKaarma
    and GHL both accepted it as a distinct value, which created duplicate customer
    records and broke returning-customer lookup. Better to pass the number through
    untouched and let it fail loudly than to invent a plausible-looking wrong one.
    """
    raw = str(phone).strip()
    if raw.startswith("+"):
        return raw

    digits = "".join(c for c in raw if c.isdigit())
    if len(digits) == 10:                                  # 6305550147 -> US
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):       # 16305550147 -> US
        return f"+{digits}"
    if len(digits) == 11 and digits.startswith("0"):
        # National format with a trunk prefix (e.g. 03464365890). We can't know the
        # country, so don't guess — strip the trunk 0 and return it unprefixed.
        return digits[1:]
    return digits or raw


def parse_customer(data: dict) -> dict:
    """Flatten myKaarma's customer response into something the voice agent can speak."""
    customer = data.get("customer") or data
    customer_uuid = customer.get("uuid") or customer.get("customerUuid")

    vehicles: List[dict] = []
    for v in customer.get("vehicles") or []:
        label = " ".join(
            str(x) for x in (v.get("year"), v.get("make"), v.get("model")) if x
        ).strip()
        vehicles.append(
            {
                "vehicle_uuid": v.get("uuid") or v.get("vehicleUuid"),
                "label": label or v.get("vin") or "Vehicle",
                "vin": v.get("vin"),
            }
        )

    return {
        "customer_uuid": customer_uuid,
        "first_name": customer.get("firstName"),
        "last_name": customer.get("lastName"),
        "vehicles": vehicles,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 2. OPCODES  —  the service catalogue.
#    The caller says "oil change"; myKaarma needs an operationUuid.
#
#    VERIFIED LIVE 2026-07-16. It is a POST (not GET) to /opcodes/v1/dealers/...
#    NOTE: do NOT send onlineSchedulerVisibility:true — in the sandbox that
#    filters everything out and returns an empty list.
# ─────────────────────────────────────────────────────────────────────────────
OPCODES_PATH = "/opcodes/v1/dealers/{dealer_uuid}/operations/searches"

# dealer_key -> { "oil change": {"uuid": ..., "laborOpCode": ..., "minutes": ...} }
_opcode_cache: Dict[str, Dict[str, dict]] = {}


async def get_opcodes(dealer: Dict[str, str], force: bool = False) -> Dict[str, dict]:
    key = dealer["dealer_key"]
    if not force and key in _opcode_cache:
        return _opcode_cache[key]

    url = MYKAARMA_BASE_URL + OPCODES_PATH.format(dealer_uuid=dealer["dealer_uuid"])
    data = await _post(
        url,
        dealer,
        {"resultSize": 50, "startPosition": 0, "getTotalCount": True},
        step="get_opcodes",
    )

    catalog: Dict[str, dict] = {}
    for op in data.get("operationDTOList") or []:
        # index by every name we might match on
        names = {
            (op.get("description") or "").strip().lower(),
            (op.get("opCodeName") or "").strip().lower(),
            (op.get("laborOpCode") or "").strip().lower(),
        }
        entry = {
            "uuid": op.get("uuid"),
            "laborOpCode": op.get("laborOpCode"),
            "minutes": op.get("opCodeDurationInMinutes"),
            "name": op.get("description") or op.get("opCodeName"),
        }
        for n in names:
            if n:
                catalog[n] = entry

    _opcode_cache[key] = catalog
    log.info("cached %d opcode names for %s", len(catalog), key)
    return catalog


def match_service(catalog: Dict[str, dict], service: str) -> Optional[dict]:
    """
    Map plain English -> an opcode.
    Deliberately conservative: if we can't match it, return None and the
    caller transfers to a human. We do NOT guess a service.
    """
    s = (service or "").strip().lower()
    if not s:
        return None
    if s in catalog:
        return catalog[s]
    for name, op in catalog.items():
        if s in name or name in s:
            return op
    # loose word overlap as a last resort
    words = set(s.split())
    for name, op in catalog.items():
        if words & set(name.split()):
            return op
    return None


# ─────────────────────────────────────────────────────────────────────────────
# 3. AVAILABILITY
#    POST /appointment/v2/department/{departmentUUID}/availability?refreshSelectionState=true
#    myKaarma warns: fetch this RIGHT BEFORE booking — slots fill from walk-ins,
#    advisors, and other apps.
# ─────────────────────────────────────────────────────────────────────────────
AVAILABILITY_PATH = (
    "/appointment/v2/department/{department_uuid}/availability?refreshSelectionState=true"
)


async def get_availability(
    dealer: Dict[str, str],
    dates: List[str],
    customer_uuid: Optional[str] = None,
    vehicle_uuid: Optional[str] = None,
    operation_uuid: Optional[str] = None,
    vin: Optional[str] = None,
    start_time: str = "08:00:00",
    end_time: str = "19:00:00",
) -> List[str]:
    """
    VERIFIED LIVE 2026-07-16 — structure below matches the docs exactly.
    IMPORTANT: customerInformation/vehicleInformation use the key "uuid",
    NOT "customerUuid"/"vehicleUuid" (that mistake returns a 500).
    """
    url = MYKAARMA_BASE_URL + AVAILABILITY_PATH.format(
        department_uuid=dealer["department_uuid"]
    )

    empty_attrs = {
        "dealerAssociateUuidList": [],
        "transportOptionUuidList": [],
        "teamUuidList": [],
        "subTransportOptionUuidList": [],
    }

    payload: Dict[str, Any] = {
        "platform": {"name": "Web"},
        "dates": dates,
        "startTime": start_time,
        "endTime": end_time,
        "selectedAvailabilityAttributes": dict(empty_attrs),
        "allAvailabilityAttributes": dict(empty_attrs),
        "fetchAvailability": True,
    }
    if customer_uuid:
        payload["customerInformation"] = {"uuid": customer_uuid}
    if vehicle_uuid or vin:
        vi: Dict[str, Any] = {}
        if vehicle_uuid:
            vi["uuid"] = vehicle_uuid
        if vin:
            vi["vin"] = vin
        payload["vehicleInformation"] = vi
    if operation_uuid:
        payload["selectedOperationUuidSet"] = [operation_uuid]

    data = await _post(url, dealer, payload, step="get_availability")
    return _extract_open_slots(data, dates, start_time, end_time)


SLOT_MINUTES = 30  # myKaarma's grid ("02:00 PM - 02:29 PM" in its warnings)


def _extract_open_slots(
    data: dict,
    dates: List[str],
    start_time: str = "08:00:00",
    end_time: str = "17:00:00",
) -> List[str]:
    """
    Work out the OPEN times for the requested dates.

    CRITICAL, and we got this backwards for a while (fixed 2026-07-20):
    `availabilityInfoMap` is the list of **UNAVAILABLE** slots, not the
    available ones. The docs are explicit — "a map showing which combination
    of Dealer Associate, Transport Option and Team is *unavailable* when and
    for what reason." A near-empty map means the store is WIDE OPEN, not full.

    So: build the grid from the dealer's hours of operation, then subtract
    every slot the response marks blocked (vacant == false).

    Other gotchas, both verified live:
      * keys come back SPACE-separated — "2026-07-22 14:00:00" — not ISO.
        create_appointment wants ISO, so we normalise on the way out.
      * a slot counts as open if ANY DA/TO/TEAM combination is vacant.
    """
    info_map = (data or {}).get("availabilityInfoMap") or {}

    # Intersect our booking window with the dealer's real hours — never offer a
    # time outside either. (The sandbox reports 06:00–18:59; we don't want the
    # voice agent offering a 6 AM oil change.)
    dealer_start = (data or {}).get("dealerHoursOfOperationStartTime")
    dealer_end = (data or {}).get("dealerHoursOfOperationEndTime")
    if dealer_start:
        start_time = max(start_time, dealer_start)
    if dealer_end:
        end_time = min(end_time, dealer_end)

    blocked = set()
    for slot_key, inner in info_map.items():
        if slot_key == "ALL_DATE_TIME" or not isinstance(inner, dict):
            continue
        iso = slot_key.replace(" ", "T")
        if "T" not in iso:
            continue  # date-only key, not a time slot
        # open if at least one combination is vacant; otherwise it's blocked
        if not any(isinstance(v, dict) and v.get("vacant") for v in inner.values()):
            blocked.add(iso)

    open_slots: List[str] = []
    for day in dates:
        try:
            t = datetime.strptime(f"{day} {start_time}", "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(f"{day} {end_time}", "%Y-%m-%d %H:%M:%S")
        except ValueError:
            log.warning("bad date/hours for availability grid: %s", day)
            continue
        while t <= end:
            iso = t.strftime("%Y-%m-%dT%H:%M:%S")
            if iso not in blocked:
                open_slots.append(iso)
            t += timedelta(minutes=SLOT_MINUTES)

    return sorted(open_slots)


# ─────────────────────────────────────────────────────────────────────────────
# 4. CREATE APPOINTMENT
#    POST /appointment/v2/dealer/{dealerUuid}/appointment
#
#    Minimum required: customerUuid, vehicleInformation, appointmentStartDateTime.
#    assignedUser / transportOption / teamUuid are sent as null -> myKaarma
#    auto-assigns an advisor. Keeps V1 simple.
# ─────────────────────────────────────────────────────────────────────────────
APPOINTMENT_PATH = "/appointment/v2/dealer/{dealer_uuid}/appointment"


async def create_appointment(
    dealer: Dict[str, str],
    customer_uuid: str,
    start: str,  # "2026-07-16T09:30:00"
    vehicle_uuid: Optional[str] = None,
    vin: Optional[str] = None,
    service_op: Optional[dict] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    comments: Optional[str] = None,
) -> dict:
    """
    VERIFIED LIVE 2026-07-16 — the endpoint accepts and validates this payload.
    Either vehicle_uuid OR vin must be provided. Since save_customer does NOT
    return vehicle UUIDs, we normally pass the VIN and let myKaarma resolve it.
    """
    url = MYKAARMA_BASE_URL + APPOINTMENT_PATH.format(dealer_uuid=dealer["dealer_uuid"])

    # Per myKaarma support (2026-07-16): do NOT send appointmentEndDateTime.
    # Sending it caused NO_TIME_INTERVAL_EXISTS. myKaarma determines the end
    # time automatically from the service duration.
    service_list = []
    if service_op:
        service_list = [
            {
                "title": service_op.get("laborOpCode"),
                "operationUuid": service_op.get("uuid"),
                "operationType": "OPCODE",
                "isCustomConcern": False,
            }
        ]

    vehicle_info: Dict[str, Any] = {}
    if vehicle_uuid:
        vehicle_info["vehicleUuid"] = vehicle_uuid
    if vin:
        vehicle_info["vin"] = vin

    # confirmationPhoneNumber must be E.164 too — save_customer normalises its copy,
    # but this one used to go through raw ("8154539364"), so myKaarma had nothing
    # routable to text.
    confirmation_phone = normalize_phone(phone) if phone else None

    payload = {
        "customerUuid": customer_uuid,
        "vehicleInformation": vehicle_info,
        "appointmentInformation": {
            "appointmentStartDateTime": start,
            "transportOption": None,
            "assignedUser": None,   # myKaarma picks the advisor
            "creatorUser": None,
            "appointmentKey": None,
            "comments": comments or "Booked by Zenvyk AI Service Coordinator",
            "internalNotes": "",
            "serviceList": service_list,
            "customerAppointmentPreference": {
                "notifyCustomer": True,
                "textConfirmation": bool(phone),
                "emailConfirmation": bool(email),
                "textReminder": bool(phone),
                "emailReminder": bool(email),
                "confirmationPhoneNumber": confirmation_phone,
                "confirmationEmail": email,
                "sendCommunicationToDA": True,
            },
            "status": None,
            "recall": False,
            "reminderCount": 0,
            "pushToDms": True,      # push it into the DMS
        },
    }

    return await _post(url, dealer, payload, step="create_appointment")
