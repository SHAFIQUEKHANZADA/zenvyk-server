r"""
WE OWE / YOU OWE scan extraction.

The chain Mitch is waiting on:

    salesperson scans the form
        -> \\elfile01\weowescanning          (the copier drops it here)
        -> Google Drive                       (weowe_bridge copies it across)
        -> THIS FILE                          (reads the form, returns fields)
        -> tracker sheet + GHL follow-up

WHY THIS IS A VISION JOB AND NOT A PDF PARSE
--------------------------------------------
All three samples have a text layer of exactly zero characters -- the copier
writes one flat image per page. pypdf, pdfplumber and friends return "" every
time. So the page is rendered and read visually.

The forms themselves are TYPED, not handwritten (only the signatures are ink),
which is what makes this reliable. Do not assume that holds forever: if a store
starts filling them in by hand the confidence will drop and the review queue is
what catches it.

TWO LAYOUTS
-----------
    Elgin              letterhead block, NO e-mail field, says "SALESPERSON"
    St. Charles H/K    no letterhead, HAS e-mail, says "SALESMAN", Miles footer

Both are handled by the same prompt rather than two templates, because a third
layout will turn up the moment we hard-code for two.

THE ONE THAT MUST NOT BE TRACKED
--------------------------------
The Kia sample (stksales_202609071811) has an empty item table and the
preprinted line "Nothing else written or verbal owed." Nothing is owed. It is
still scanned, still filed, and it must NOT become a row the BDC chases -- a
tracker full of empty obligations is worse than no tracker. `has_open_items`
is what keeps it out.

The "YOU OWE" half is preprinted: items 1-8 are on every single form. They only
count as real when a date has been written in the "TO BE RECEIVED BY DATE"
column next to them.
"""

import base64
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

# --- store lookup -----------------------------------------------------------
# The copier names the file after the store and the scan time, e.g.
#     ElHondasales_260907_122420.pdf   -> Elgin Honda,       2026-09-07 12:24:20
#     sthsales_260903_203526.pdf       -> St. Charles Honda, 2026-09-03 20:35:26
#     stksales_202609071811.pdf        -> St. Charles Kia,   2026-09-07 18:11
# Note the third uses a different timestamp format. Both are parsed; neither is
# trusted over the DATE printed on the form itself.

STORE_PREFIXES = [
    ("elhonda", "mcgrath_honda_elgin", "McGrath Honda of Elgin"),
    ("sth", "mcgrath_honda_stcharles", "McGrath Honda of St. Charles"),
    ("stk", "mcgrath_kia_stcharles", "McGrath Kia of St. Charles"),
]

# Stock prefixes seen so far. A useful cross-check on the store, not a rule --
# if it disagrees with the filename we keep both and let a human look.
STOCK_PREFIX_HINT = {"E": "Elgin", "S": "St. Charles Honda", "T": "trade/used"}

WE_OWE_VALID_DAYS = 30

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_MODEL = os.getenv("WEOWE_MODEL", "claude-sonnet-5")
RENDER_DPI = int(os.getenv("WEOWE_DPI", "200"))


class WeOweItem(BaseModel):
    qty: Optional[str] = None
    item: str
    part: Optional[str] = None
    labor: Optional[str] = None


class YouOweItem(BaseModel):
    item: str
    due_date: Optional[str] = None


class WeOweRecord(BaseModel):
    """One scanned form, normalised."""

    source_file: str
    store_key: Optional[str] = None
    store_name: Optional[str] = None
    scanned_at: Optional[str] = None

    customer_name: Optional[str] = None
    customer_name_source: Optional[str] = None  # "form" | "email" | None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    zip_code: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None

    stock_no: Optional[str] = None
    condition: Optional[str] = None  # new | used | both | None
    year: Optional[str] = None
    make: Optional[str] = None
    model: Optional[str] = None
    vin: Optional[str] = None

    salesperson: Optional[str] = None
    delivery_date: Optional[str] = None
    form_date: Optional[str] = None
    expires_on: Optional[str] = None

    we_owe_items: List[WeOweItem] = Field(default_factory=list)
    you_owe_items: List[YouOweItem] = Field(default_factory=list)

    has_open_items: bool = False
    needs_review: bool = False
    review_reasons: List[str] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict)


# --- rendering --------------------------------------------------------------

def render_pages(pdf_bytes: bytes, dpi: int = RENDER_DPI, max_pages: int = 2) -> List[bytes]:
    """PDF -> PNG bytes per page.

    200 DPI is the floor that keeps a VIN readable after a copier scan. Below
    that the 1/I and 0/O confusions start, and a wrong VIN is worse than a
    missing one because nobody notices.
    """
    import pymupdf  # imported here so the rest of the service starts without it

    out: List[bytes] = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in list(doc)[:max_pages]:
            out.append(page.get_pixmap(dpi=dpi).tobytes("png"))
    return out


# --- filename -> store ------------------------------------------------------

def store_from_filename(filename: str) -> Dict[str, Optional[str]]:
    stem = os.path.basename(filename or "").lower()
    for prefix, key, name in STORE_PREFIXES:
        if stem.startswith(prefix):
            return {"store_key": key, "store_name": name}
    return {"store_key": None, "store_name": None}


def scanned_at_from_filename(filename: str) -> Optional[str]:
    """Pull the copier timestamp out of the filename.

    Two formats in the wild:
        _260907_122420   YYMMDD_HHMMSS
        _202609071811    YYYYMMDDHHMM
    """
    stem = os.path.basename(filename or "")
    m = re.search(r"_(\d{6})_(\d{6})(?:\D|$)", stem)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%y%m%d%H%M%S").isoformat()
        except ValueError:
            pass
    m = re.search(r"_(\d{12})(?:\D|$)", stem)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d%H%M").isoformat()
        except ValueError:
            pass
    return None


# --- normalisation ----------------------------------------------------------

_PHONE_RE = re.compile(r"\D+")


def normalise_phone(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    digits = _PHONE_RE.sub("", value)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    return f"+1{digits}"


_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y", "%b %d, %Y")


def normalise_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def name_from_email(email: Optional[str]) -> Optional[str]:
    """Recover a name when the NAME line was left blank.

    The St. Charles sample has an empty NAME but the e-mail
    gallagher.shannon.r@gmail.com, and the signature reads Shannon Rae
    Gallagher. Worth a guess, but it is flagged as a guess -- addressing a
    customer by a name scraped out of their e-mail handle is a bad look if the
    handle isn't their name.
    """
    if not email or "@" not in email:
        return None
    local = email.split("@", 1)[0]
    parts = [p for p in re.split(r"[._\-]+", local) if p and not p.isdigit()]
    words = [p.capitalize() for p in parts if len(p) > 1]
    if len(words) < 2:
        return None
    # gallagher.shannon.r -> Shannon Gallagher (surname is written first here)
    return f"{words[1]} {words[0]}"


# --- the prompt -------------------------------------------------------------

EXTRACTION_PROMPT = """You are reading a scanned automotive "WE OWE / YOU OWE" form from a McGrath dealership.

Return ONLY a JSON object. No prose, no markdown fence.

{
  "store_name": string|null,          // from the letterhead, if there is one
  "customer_name": string|null,
  "address": string|null,
  "city": string|null,
  "state": string|null,
  "zip_code": string|null,
  "phone": string|null,
  "email": string|null,
  "stock_no": string|null,
  "condition": "new"|"used"|"both"|null,   // the NEW / USED checkboxes
  "year": string|null,
  "make": string|null,
  "model": string|null,
  "vin": string|null,                 // labelled SERIAL NO., 17 characters
  "salesperson": string|null,         // labelled SALESPERSON or SALESMAN
  "delivery_date": string|null,       // DEL. DATE
  "form_date": string|null,           // the DATE next to the customer signature
  "we_owe_items": [ {"qty": string|null, "item": string, "part": string|null, "labor": string|null} ],
  "you_owe_items": [ {"item": string, "due_date": string|null} ],
  "customer_signed": true|false,
  "manager_approved": true|false,
  "nothing_owed_noted": true|false,
  "handwritten": true|false,
  "unreadable_fields": [string]
}

RULES

1. A blank line is null. Never invent a value, never carry one over from
   another field, never complete a partial value from context.

2. we_owe_items: ONLY rows a human typed or wrote into the item table. The
   table has six empty rows on every form -- empty rows are not items.
   The preprinted lines "Nothing else written or verbal owed." and
   "No refunds or exchanges" are NOT items; if you see either, set
   nothing_owed_noted true and do not list them.

3. you_owe_items: the numbered list 1-8 ("Title to Trade In Vehicle",
   "All Monies", "Valid Insurance Card", "Other") is PREPRINTED on every
   form. Include an entry ONLY when a date has been written in the
   "TO BE RECEIVED BY DATE" column beside it. If that column is empty for
   every row, return an empty list.

4. Dates exactly as printed, e.g. "09/07/2026".

5. vin: 17 characters. If you cannot read every character with confidence,
   return null and add "vin" to unreadable_fields. A wrong VIN is worse than
   a missing one.

6. handwritten: true only if the FILLED-IN values are handwritten. Signatures
   are always ink -- they do not make the form handwritten.

7. Anything you cannot read goes in unreadable_fields by its field name.
"""


async def _call_vision(images: List[bytes], api_key: str) -> Dict[str, Any]:
    content: List[Dict[str, Any]] = []
    for png in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(png).decode("ascii"),
            },
        })
    content.append({"type": "text", "text": EXTRACTION_PROMPT})

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            ANTHROPIC_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": content}],
            },
        )
    resp.raise_for_status()
    body = resp.json()
    text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
    return _loads_loose(text)


def _loads_loose(text: str) -> Dict[str, Any]:
    """Parse JSON even if the model wrapped it in a fence or a sentence."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start:end + 1])
        raise


# --- assembly ---------------------------------------------------------------

def build_record(raw: Dict[str, Any], filename: str) -> WeOweRecord:
    store = store_from_filename(filename)
    reasons: List[str] = []

    we_items = [WeOweItem(**i) for i in (raw.get("we_owe_items") or []) if i.get("item")]
    you_items = [
        YouOweItem(**i) for i in (raw.get("you_owe_items") or [])
        if i.get("item") and i.get("due_date")
    ]

    name = (raw.get("customer_name") or "").strip() or None
    name_source = "form" if name else None
    email = (raw.get("email") or "").strip() or None
    if not name:
        guessed = name_from_email(email)
        if guessed:
            name, name_source = guessed, "email"
            reasons.append("NAME was blank on the form; name guessed from the e-mail address")
        else:
            reasons.append("No customer name on the form and none recoverable")

    phone = normalise_phone(raw.get("phone"))
    if raw.get("phone") and not phone:
        reasons.append(f"Phone not usable: {raw.get('phone')!r}")

    form_date = normalise_date(raw.get("form_date"))
    expires_on = None
    if form_date:
        expires_on = (datetime.fromisoformat(form_date) + timedelta(days=WE_OWE_VALID_DAYS)).date().isoformat()
    else:
        reasons.append("No form date, so the 30-day expiry can't be calculated")

    vin = (raw.get("vin") or "").strip().upper() or None
    if vin and len(vin) != 17:
        reasons.append(f"VIN is {len(vin)} characters, expected 17")
        vin = None

    if raw.get("handwritten"):
        reasons.append("Form is handwritten - check the fields before trusting them")
    for field in (raw.get("unreadable_fields") or []):
        reasons.append(f"Could not read: {field}")

    has_open = bool(we_items or you_items)
    if not has_open and not raw.get("nothing_owed_noted"):
        # Nothing found AND no "nothing owed" note. Either genuinely empty or
        # we missed the items -- a human decides, we don't silently drop it.
        reasons.append("No items found and no 'nothing owed' note - confirm this form is really empty")

    if not phone and not email:
        reasons.append("No phone and no e-mail - the customer cannot be contacted")

    return WeOweRecord(
        source_file=os.path.basename(filename),
        store_key=store["store_key"],
        store_name=store["store_name"] or (raw.get("store_name") or None),
        scanned_at=scanned_at_from_filename(filename),
        customer_name=name,
        customer_name_source=name_source,
        address=raw.get("address"),
        city=raw.get("city"),
        state=raw.get("state"),
        zip_code=raw.get("zip_code"),
        phone=phone,
        email=email,
        stock_no=(raw.get("stock_no") or "").strip() or None,
        condition=raw.get("condition"),
        year=raw.get("year"),
        make=raw.get("make"),
        model=raw.get("model"),
        vin=vin,
        salesperson=(raw.get("salesperson") or "").strip() or None,
        delivery_date=normalise_date(raw.get("delivery_date")),
        form_date=form_date,
        expires_on=expires_on,
        we_owe_items=we_items,
        you_owe_items=you_items,
        has_open_items=has_open,
        needs_review=bool(reasons),
        review_reasons=reasons,
        raw=raw,
    )


async def extract(pdf_bytes: bytes, filename: str, api_key: Optional[str] = None) -> WeOweRecord:
    key = api_key or os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set - the scan cannot be read")
    images = render_pages(pdf_bytes)
    raw = await _call_vision(images, key)
    return build_record(raw, filename)


# --- CLI --------------------------------------------------------------------
# python weowe.py ../weowe_files/*.pdf

if __name__ == "__main__":
    import asyncio
    import glob
    import sys

    async def _main(patterns: List[str]) -> None:
        paths: List[str] = []
        for p in patterns:
            paths.extend(sorted(glob.glob(p)))
        if not paths:
            print("no files matched")
            return
        for path in paths:
            with open(path, "rb") as fh:
                data = fh.read()
            print("=" * 70)
            print(os.path.basename(path))
            try:
                rec = await extract(data, path)
            except Exception as exc:  # noqa: BLE001 - CLI, show and continue
                print(f"  FAILED: {exc}")
                continue
            print(json.dumps(rec.model_dump(exclude={"raw"}), indent=2))

    asyncio.run(_main(sys.argv[1:] or ["../weowe_files/*.pdf"]))
