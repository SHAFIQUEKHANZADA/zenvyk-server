"""
Writing equity events to the Esther dashboard's database.

Reid, 19 Sep: "Yes and add to dash board Appraisal Scheduled".

The dashboard (dashboard.gomcgrath.com) reads everything from Supabase. Most of
its numbers arrive via a 15-minute ingest that pulls GHL and myKaarma. This one
does not go through that ingest -- it is written here, the moment the customer
answers, because Reid watches this card while the customer is still sitting in
the service lounge. A count that lags fifteen minutes is a count he can't act
on.

Writes are fire-and-forget: a dashboard that is down, misconfigured or slow must
never stop a customer's text thread. Every function here swallows its own
errors and says what happened in the return value.

Configuration (Railway):

    SUPABASE_URL          https://xxxx.supabase.co
    SUPABASE_SERVICE_KEY  the service_role key (NOT the anon key -- these rows
                          are written on behalf of the store, not a signed-in
                          user, so they must bypass RLS)

With neither set, every call here no-ops quietly and the rest of the equity
flow is unaffected. That is deliberate: the counter is an extra, not a
dependency.
"""

import logging
import os
from datetime import datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger("mykaarma.dashboard")

DEALER_TZ = ZoneInfo("America/Chicago")
TABLE = "esther_equity_appraisals"

# dealer_key -> store_id (uuid). esther_stores is a handful of rows that change
# perhaps twice a year, so it is read once per process rather than per reply.
_STORE_IDS: Dict[str, Optional[str]] = {}


def _config() -> Optional[tuple]:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_KEY") or "").strip()
    if not url or not key:
        return None
    return url, key


def _headers(key: str, prefer: str = "") -> Dict[str, str]:
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def local_date() -> str:
    return datetime.now(DEALER_TZ).date().isoformat()


async def _store_id(dealer_key: Optional[str]) -> Optional[str]:
    """
    Map our dealer_key onto the dashboard's store uuid.

    The dashboard filters every card by store_id, so a row written without one
    is invisible -- it would count toward nothing and look like the feature
    silently failed. The row is still written (the count is recoverable later by
    dealer_key), but this is logged loudly.
    """
    if not dealer_key:
        return None
    if dealer_key in _STORE_IDS:
        return _STORE_IDS[dealer_key]

    cfg = _config()
    if not cfg:
        return None
    url, key = cfg
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as c:
            r = await c.get(
                f"{url}/rest/v1/esther_stores",
                params={"select": "id", "mykaarma_dealer_key": f"eq.{dealer_key}",
                        "limit": "1"},
                headers=_headers(key),
            )
        rows = r.json() if r.status_code < 400 else []
        store_id = rows[0]["id"] if rows else None
    except Exception as e:                      # noqa: BLE001 - never propagate
        log.warning("store lookup failed for %s: %s", dealer_key, e)
        return None                             # not cached: retry next time

    if not store_id:
        log.warning("no esther_stores row with mykaarma_dealer_key=%r — the "
                    "appraisal will be recorded but won't show on any store's "
                    "dashboard until that row exists", dealer_key)
    _STORE_IDS[dealer_key] = store_id
    return store_id


async def record_appraisal(
    dealer_key: Optional[str],
    phone: Optional[str],
    *,
    customer_name: Optional[str] = None,
    vehicle: Optional[str] = None,
    priority_score: Optional[int] = None,
    priority_band: Optional[str] = None,
    appointment_time: Optional[str] = None,
) -> dict:
    """
    The customer said yes to a trade value. One row per customer per day.

    Upserted, not inserted: a customer who replies "yes" twice on one visit is
    one appraisal, not two. Reid is measured on this number, so it has to be
    the count of people, not the count of messages.
    """
    cfg = _config()
    if not cfg:
        return {"written": False, "reason": "supabase_not_configured"}
    if not phone:
        return {"written": False, "reason": "no_phone"}
    url, key = cfg

    row = {
        "dealer_key": dealer_key,
        "store_id": await _store_id(dealer_key),
        "phone": phone,
        "customer_name": customer_name,
        "vehicle": vehicle,
        "priority_score": priority_score,
        "priority_band": priority_band,
        "appointment_time": appointment_time,
        "local_date": local_date(),
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as c:
            r = await c.post(
                f"{url}/rest/v1/{TABLE}",
                params={"on_conflict": "dealer_key,phone,local_date"},
                headers=_headers(key, "resolution=merge-duplicates,return=minimal"),
                json=row,
            )
        if r.status_code >= 400:
            log.warning("appraisal write failed [%s] %s", r.status_code, r.text[:300])
            return {"written": False, "status": r.status_code}
        return {"written": True}
    except Exception as e:                      # noqa: BLE001 - never propagate
        log.warning("appraisal write errored: %s", e)
        return {"written": False, "reason": str(e)[:200]}


async def mark_wants_options(dealer_key: Optional[str],
                             phone: Optional[str]) -> dict:
    """
    They also agreed to talk to someone. Updates the same row rather than
    adding another, so the dashboard can answer the question Reid will ask
    next: of everyone who wanted a number, how many let us walk over?
    """
    cfg = _config()
    if not cfg or not phone:
        return {"written": False, "reason": "not_configured_or_no_phone"}
    url, key = cfg
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=4.0)) as c:
            r = await c.patch(
                f"{url}/rest/v1/{TABLE}",
                params={"dealer_key": f"eq.{dealer_key}", "phone": f"eq.{phone}",
                        "local_date": f"eq.{local_date()}"},
                headers=_headers(key, "return=minimal"),
                json={"wants_options": True, "options_at": "now()"},
            )
        return {"written": r.status_code < 400, "status": r.status_code}
    except Exception as e:                      # noqa: BLE001
        log.warning("wants_options update errored: %s", e)
        return {"written": False, "reason": str(e)[:200]}
