# Zenvyk × myKaarma Connector

Bridges the **GoHighLevel Voice AI agent** ("Esther") to the **myKaarma Scheduler API**, so the agent can look up a caller, offer *real* open appointment times, and book the appointment straight into myKaarma.

---

## Why this service exists

myKaarma needs **four chained API calls** to book one appointment (find customer → get service opcode → check availability → create appointment). GHL's voice agent can only make **single** webhook calls, and an LLM cannot reliably juggle UUIDs or parse myKaarma's nested `availabilityInfoMap` mid-conversation.

This connector does all of that in code and exposes **three simple endpoints** the agent can call.

```
Customer calls
      │
GHL Voice Agent (Esther)
      ├── lookup_customer      ──► THIS SERVICE ──► myKaarma   "I see a 2020 Honda Accord on file"
      ├── get_available_slots  ──► THIS SERVICE ──► myKaarma   real open times (max 3)
      └── book_appointment     ──► THIS SERVICE ──► myKaarma   appointment created ✅
```

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/mykaarma/lookup-customer` | Find the caller by phone; return their name + vehicles |
| POST | `/mykaarma/get-slots` | Return REAL open appointment times (max 3) |
| POST | `/mykaarma/book-appointment` | Create the appointment in myKaarma |
| POST | `/mykaarma/refresh-opcodes` | Re-fetch and cache the dealership's service catalogue |
| GET | `/health` | Health + tells you if credentials are missing |
| GET | `/docs` | Interactive Swagger UI |

Every response includes an **`agent_instruction`** field telling the voice agent exactly what to say next. Every failure returns `transfer_to: 630-797-4570` so a caller is never left stranded.

---

## Setup

```bash
cd mykaarma
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env          # fill in the myKaarma credentials
uvicorn main:app --reload --port 8000
```

Open http://localhost:8000/docs to try the endpoints.

### Required environment variables

| Var | Where it comes from |
|---|---|
| `MYKAARMA_USERNAME` | myKaarma credentials email (sent to Zach) |
| `MYKAARMA_PASSWORD` | same |
| `MYKAARMA_DEALER_UUID` | myKaarma (per dealership) |
| `MYKAARMA_DEPARTMENT_UUID` | myKaarma (the **service** department) |

> ⚠️ **myKaarma geo-blocks non-US IPs (403 Forbidden).** Testing from a non-US laptop will fail. Deploy to Railway (US) and test from there.

---

## Deploy to Railway

1. Push this folder to a Git repo
2. Railway → **New Project → Deploy from GitHub**
3. Set the four `MYKAARMA_*` env vars
4. Start command (already in the `Procfile`):
   ```
   uvicorn main:app --host 0.0.0.0 --port $PORT
   ```
5. Check `https://<your-app>.up.railway.app/health` → should say `"mykaarma_configured": true`

---

## Wire it into the GHL voice agent

Add three **Custom Actions** to the agent:

| Action name | Method | URL |
|---|---|---|
| `lookup_customer` | POST | `https://<your-app>/mykaarma/lookup-customer` |
| `get_available_slots` | POST | `https://<your-app>/mykaarma/get-slots` |
| `book_appointment` | POST | `https://<your-app>/mykaarma/book-appointment` |

Then add this to the agent's instructions:

```
TOOLS — use them in this order:

1. At the START of the call, call `lookup_customer` with the caller's phone number.
   If a vehicle comes back, confirm it:
   "I see a 2020 Honda Accord on file — is that the vehicle you're bringing in?"
   If nothing is found, ask for their name and the year, make and model.

2. Once you know the SERVICE and the DAY they want, call `get_available_slots`.
   Offer ONLY the times it returns. NEVER invent, guess, or estimate a time.

3. After the customer picks a time, call `book_appointment` with the exact value
   from the `slots` array.
   Only tell the customer they are booked AFTER it returns success:true.

If any tool fails, apologize and transfer the call to 630-797-4570.
```

---

## Request / response examples

**Lookup**
```json
POST /mykaarma/lookup-customer
{ "phone": "4025781297" }

→ {
  "found": true,
  "customer_uuid": "abc…",
  "first_name": "Reid",
  "vehicles": [{ "vehicle_uuid": "veh…", "label": "2020 Honda Accord" }],
  "agent_instruction": "Greet Reid by name and confirm the vehicle: …"
}
```

**Slots**
```json
POST /mykaarma/get-slots
{ "service": "oil change", "dates": ["2026-07-16"],
  "customer_uuid": "abc…", "vehicle_uuid": "veh…" }

→ {
  "success": true,
  "slots": ["2026-07-16T09:30:00", "2026-07-16T13:00:00", "2026-07-16T15:30:00"],
  "spoken_slots": ["Thursday, July 16 at 9:30 AM", "…1:00 PM", "…3:30 PM"],
  "agent_instruction": "Offer ONLY these times. Do NOT invent any other time."
}
```

**Book**
```json
POST /mykaarma/book-appointment
{ "appointment_time": "2026-07-16T09:30:00", "service": "oil change",
  "customer_uuid": "abc…", "vehicle_uuid": "veh…",
  "phone": "4025781297", "email": "reid@example.com" }

→ {
  "success": true,
  "spoken_time": "Thursday, July 16 at 9:30 AM",
  "agent_instruction": "Confirm: 'You're all set for Thursday, July 16 at 9:30 AM.'"
}
```

---

## ⚠️ Two URLs you MUST verify before this works

The myKaarma appointment doc links out to two endpoints without printing their URLs. I've made a best guess — **confirm both in Postman (or in the myKaarma walkthrough call) and fix them in `mykaarma_client.py`:**

```python
CUSTOMER_PATH = "/customer/v2/department/{department_uuid}/customer"   # ← VERIFY
OPCODES_PATH  = "/kopcode/v1/dealer/{dealer_uuid}/opcodes"             # ← VERIFY
```

These two are confirmed correct from the docs:
```python
AVAILABILITY_PATH = "/appointment/v2/department/{department_uuid}/availability?refreshSelectionState=true"
APPOINTMENT_PATH  = "/appointment/v2/dealer/{dealer_uuid}/appointment"
```

---

## Design decisions worth knowing

- **`assignedUser`, `transportOption`, `teamUuid` are all sent as `null`** → myKaarma auto-assigns an advisor. Keeps V1 simple.
- **Opcodes are cached per dealer** — the catalogue is fetched once, not on every call.
- **Unknown service → transfer to a human.** We never guess an opcode.
- **The slot is re-checked immediately before booking** — myKaarma warns that slots fill from walk-ins, advisors and other apps. If it's gone, we return three alternatives instead of failing.
- **Every response tells the agent what to say next** (`agent_instruction`), so the prompt stays simple.
- **Multi-tenant hook:** `config.get_dealer(dealer_key)` reads env vars today. Swap its body for a Supabase lookup and you can add all 19+ McGrath stores without a redeploy.

---

## Build order

1. ⚠️ **Get the credentials from Zach** (myKaarma emailed them to him)
2. **Postman first** — manually run the 4 myKaarma calls in the sandbox and create one appointment. Fix the two unverified URLs.
3. Deploy this service to Railway, check `/health`
4. Add the 3 Custom Actions to the GHL voice agent + update its prompt
5. Make a real test call → confirm the appointment appears in myKaarma
