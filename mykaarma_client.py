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
#    Finds an existing customer OR creates one, and returns their vehicles.
#
#    ⚠️  VERIFY THIS PATH against the docs page
#        "Create or update customer with search for duplicates"
#        (the appointment doc links to it but doesn't print the URL).
#        Fix the CUSTOMER_PATH constant below once you confirm it in Postman.
# ─────────────────────────────────────────────────────────────────────────────
CUSTOMER_PATH = "/customer/v2/department/{department_uuid}/customer"


async def save_customer(
    dealer: Dict[str, str],
    phone: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    email: Optional[str] = None,
    vin: Optional[str] = None,
) -> dict:
    url = MYKAARMA_BASE_URL + CUSTOMER_PATH.format(
        department_uuid=dealer["department_uuid"]
    )

    payload: Dict[str, Any] = {"searchForDuplicate": True}
    if phone:
        payload["phoneNumbers"] = [{"number": phone, "type": "CELL"}]
    if first_name:
        payload["firstName"] = first_name
    if last_name:
        payload["lastName"] = last_name
    if email:
        payload["emails"] = [{"address": email}]
    if vin:
        payload["vehicles"] = [{"vin": vin}]

    return await _post(url, dealer, payload, step="save_customer")


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
#    ⚠️  VERIFY THIS PATH against "How to get opcodes and menus".
# ─────────────────────────────────────────────────────────────────────────────
OPCODES_PATH = "/kopcode/v1/dealer/{dealer_uuid}/opcodes"

# dealer_key -> { "oil change": {"uuid": ..., "laborOpCode": ..., "minutes": ...} }
_opcode_cache: Dict[str, Dict[str, dict]] = {}


async def get_opcodes(dealer: Dict[str, str], force: bool = False) -> Dict[str, dict]:
    key = dealer["dealer_key"]
    if not force and key in _opcode_cache:
        return _opcode_cache[key]

    url = MYKAARMA_BASE_URL + OPCODES_PATH.format(dealer_uuid=dealer["dealer_uuid"])
    data = await _get(url, dealer, step="get_opcodes")

    raw = data.get("opcodes") if isinstance(data, dict) else data
    catalog: Dict[str, dict] = {}
    for op in raw or []:
        name = (
            op.get("description")
            or op.get("opCodeName")
            or op.get("laborOpCode")
            or ""
        ).strip().lower()
        if not name:
            continue
        catalog[name] = {
            "uuid": op.get("uuid"),
            "laborOpCode": op.get("laborOpCode"),
            "minutes": op.get("durationInMins"),
            "name": name,
        }

    _opcode_cache[key] = catalog
    log.info("cached %d opcodes for %s", len(catalog), key)
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
) -> List[str]:
    url = MYKAARMA_BASE_URL + AVAILABILITY_PATH.format(
        department_uuid=dealer["department_uuid"]
    )

    payload: Dict[str, Any] = {
        "dates": dates,
        "selectedAvailabilityAttributes": {},  # no advisor/team/transport preference
        "allAvailabilityAttributes": {},
    }
    if customer_uuid:
        payload["customerInformation"] = {"customerUuid": customer_uuid}
    if vehicle_uuid:
        payload["vehicleInformation"] = {"vehicleUuid": vehicle_uuid}
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
    vehicle_uuid: str,
    start: str,  # "2026-07-16T09:30:00"
    service_op: Optional[dict] = None,
    phone: Optional[str] = None,
    email: Optional[str] = None,
    comments: Optional[str] = None,
) -> dict:
    url = MYKAARMA_BASE_URL + APPOINTMENT_PATH.format(dealer_uuid=dealer["dealer_uuid"])

    minutes = (service_op or {}).get("minutes") or DEFAULT_APPOINTMENT_MINUTES
    end_dt = datetime.fromisoformat(start) + timedelta(minutes=int(minutes) - 1, seconds=59)
    end = end_dt.strftime("%Y-%m-%dT%H:%M:%S")

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

    payload = {
        "customerUuid": customer_uuid,
        "vehicleInformation": {"vehicleUuid": vehicle_uuid},
        "appointmentInformation": {
            "appointmentStartDateTime": start,
            "appointmentEndDateTime": end,
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
