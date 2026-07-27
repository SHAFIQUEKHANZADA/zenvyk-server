"""
Closed-RO → GHL review-request bridge.

WHY THIS EXISTS
    When a repair order closes in myKaarma, McGrath wants the customer to get a
    "how was your service? leave us a review" message from GHL. myKaarma does not
    (as far as we know) push us a webhook on close, so we POLL the order-search
    endpoint on a schedule, find the ROs that closed since we last looked, and POST
    each customer's details to a GHL Inbound Webhook. GHL then creates/updates the
    contact and fires its own review workflow.

FLOW
    myKaarma order search (orderStatus=C)  ->  this module  ->  GHL inbound webhook

WHAT GHL RECEIVES (matches the fields the workflow maps):
    first_name, last_name, phone, email,
    vehicle_year, vehicle_make, vehicle_model, vin,
    service_performed, ro_number, ro_close_date, source, event

DEDUP / STATE
    We must never text the same customer twice for the same RO. For now we keep an
    in-memory set of RO numbers we've already pushed. That resets if Railway
    restarts, so the poll window is kept small (a short lookback) to limit repeats.
    PRODUCTION TODO: move `seen_ros` + `last_run` into Supabase so restarts don't
    re-send. Left in memory deliberately so this can ship and be tested first.
"""

import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

import httpx

from config import MYKAARMA_BASE_URL, headers
import mykaarma_client as mk

log = logging.getLogger("mykaarma.review_sync")

ORDER_SEARCH_PATH = "/order/v2/department/{department_uuid}/order/specificSearch"
TIMEOUT = httpx.Timeout(45.0, connect=10.0)

# RO numbers we've already forwarded to GHL this process lifetime.
_seen_ros: Set[str] = set()


async def _search_closed_orders(
    dealer: Dict[str, str], from_date: str, to_date: str
) -> List[dict]:
    """Return closed repair orders whose CLOSE date falls in [from_date, to_date]."""
    url = MYKAARMA_BASE_URL + ORDER_SEARCH_PATH.format(
        department_uuid=dealer["department_uuid"]
    )
    payload = {
        "orderType": "RO",
        "orderStatus": "C",            # C = Closed
        "dateFilterType": "CLOSE_DATE",
        "fromOrderDate": from_date,
        "toOrderDate": to_date,
        "pageNo": "0",
        "size": "150",
    }
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(url, headers=headers(dealer), json=payload)
    if r.status_code >= 400:
        log.error("order search -> %s %s", r.status_code, r.text[:300])
        raise mk.MyKaarmaError(r.status_code, r.text, "order_search")

    data = r.json() if r.text else {}
    # ORDER_NOT_FOUND is a normal "nothing matched", not an error.
    return data.get("orders") or []


def _order_to_payload(order: dict) -> Optional[dict]:
    """Flatten one order into the flat JSON the GHL webhook expects."""
    customer = order.get("customer") or {}
    vehicle = order.get("vehicle") or {}

    phone = order.get("phone") or customer.get("phone")
    if phone:
        phone = mk.normalize_phone(phone)

    # Service description: prefer explicit line items, fall back to whatever's there.
    services = order.get("services") or order.get("orderLines") or []
    if services:
        service_performed = ", ".join(
            str(s.get("title") or s.get("description") or s.get("laborOpCode") or "").strip()
            for s in services
            if (s.get("title") or s.get("description") or s.get("laborOpCode"))
        )
    else:
        service_performed = order.get("serviceDescription") or ""

    return {
        "first_name": customer.get("firstName") or "",
        "last_name": customer.get("lastName") or "",
        "phone": phone or "",
        "email": customer.get("email") or order.get("email") or "",
        "vehicle_year": str(vehicle.get("year") or ""),
        "vehicle_make": vehicle.get("make") or "",
        "vehicle_model": vehicle.get("model") or "",
        "vin": vehicle.get("vin") or order.get("vin") or "",
        "service_performed": service_performed,
        "ro_number": order.get("orderNumber") or order.get("orderUuid") or "",
        "ro_close_date": order.get("closeDate") or order.get("updatedDate") or "",
        "source": "mykaarma",
        "event": "ro_closed",
    }


async def _post_to_ghl(webhook_url: str, payload: dict) -> bool:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(webhook_url, json=payload)
    ok = r.status_code < 400
    if not ok:
        log.error("GHL webhook -> %s %s", r.status_code, r.text[:200])
    return ok


async def sync_closed_ros(
    dealer: Dict[str, str],
    webhook_url: str,
    lookback_minutes: int = 20,
) -> dict:
    """
    Find ROs closed in the last `lookback_minutes` and push each new one to GHL.
    Runs on a schedule (see routes: POST /mykaarma/sync-reviews). Idempotent within
    a process via `_seen_ros`; a customer is pushed at most once per RO.
    """
    # myKaarma's close-date filter is a calendar date (not a timestamp), so we search
    # today plus yesterday to cover ROs closed near midnight, and rely on RO-number
    # dedup (`_seen_ros`) for the finer window. lookback_minutes is kept for future
    # use once we move to timestamp filtering / persisted state.
    today = datetime.utcnow().date()
    dates = {today.isoformat(), (today - timedelta(days=1)).isoformat()}

    pushed, skipped, failed = 0, 0, 0
    for d in sorted(dates):
        try:
            orders = await _search_closed_orders(dealer, d, d)
        except mk.MyKaarmaError as e:
            if "ORDER_NOT_FOUND" in (e.body or ""):
                continue
            raise

        for order in orders:
            ro_no = order.get("orderNumber") or order.get("orderUuid")
            if not ro_no or ro_no in _seen_ros:
                skipped += 1
                continue
            payload = _order_to_payload(order)
            if not payload.get("phone"):
                log.info("RO %s has no phone — skipping review request", ro_no)
                _seen_ros.add(ro_no)
                skipped += 1
                continue
            if await _post_to_ghl(webhook_url, payload):
                _seen_ros.add(ro_no)
                pushed += 1
            else:
                failed += 1

    result = {"pushed": pushed, "skipped": skipped, "failed": failed,
              "seen_total": len(_seen_ros)}
    log.info("review sync: %s", result)
    return result
