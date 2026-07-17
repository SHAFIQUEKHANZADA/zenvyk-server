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
    """myKaarma rejects anything that isn't E.164 (+1XXXXXXXXXX)."""
    digits = "".join(c for c in str(phone) if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone if str(phone).startswith("+") else f"+{digits}"


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
    return _extract_open_slots(data)


def _extract_open_slots(data: dict) -> List[str]:
    """
    availabilityInfoMap:
        outer key   = date | date-time | "ALL_DATE_TIME"
        inner key   = "DA={uuid},TO={uuid},TEAM={uuid}"
        value       = VacancyInfo { vacant: bool, warningMap: {...} }
    We keep any date-time where at least one combination is vacant.
    """
    slots: List[str] = []
    info_map = (data or {}).get("availabilityInfoMap") or {}

    for slot_key, inner in info_map.items():
        if slot_key == "ALL_DATE_TIME" or "T" not in slot_key:
            continue  # skip aggregate + date-only keys; we want date-times
        if isinstance(inner, dict) and any(
            isinstance(v, dict) and v.get("vacant") for v in inner.values()
        ):
            slots.append(slot_key)

    return sorted(slots)


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
                "confirmationPhoneNumber": phone,
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
