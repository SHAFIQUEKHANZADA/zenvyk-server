import asyncio, os, sys, time, httpx
from datetime import datetime
sys.path.insert(0, r"D:\work\dealership\mykaarma")
os.chdir(r"D:\work\dealership\mykaarma")
import mykaarma_client as mk
from config import get_dealer
from routes import DEALER_TZ

BASE = "https://web-production-a8da.up.railway.app"
KEY  = "mcgrath_honda_stcharles"
PHONE = "2139479661"
d = get_dealer(KEY)
RESULTS = []

def rec(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))

async def post(p, b):
    async with httpx.AsyncClient(timeout=180) as c:
        return (await c.post(f"{BASE}/mykaarma/{KEY}{p}", json=b)).json()

async def nrec():
    return len(await mk.search_customer(d, phone=PHONE))

async def appt():
    now = datetime.now(DEALER_TZ).replace(tzinfo=None)
    for m in await mk.search_customer(d, phone=PHONE):
        p = mk.parse_search_match(m)
        for a in mk.upcoming_appointments(await mk.get_customer_appointments(d, p['customer_uuid']), now):
            return a
    return None

async def clear():
    now = datetime.now(DEALER_TZ).replace(tzinfo=None)
    for m in await mk.search_customer(d, phone=PHONE):
        cu = mk.parse_search_match(m)['customer_uuid']
        for a in mk.upcoming_appointments(await mk.get_customer_appointments(d, cu), now):
            await mk.cancel_appointment(d, a['appointment_uuid'])

async def wait_vehicle(limit=60):
    t0 = time.monotonic()
    while time.monotonic() - t0 < limit:
        a = await appt()
        if a and a.get('vehicle'):
            return a.get('vehicle'), round(time.monotonic()-t0)
        await asyncio.sleep(5)
    a = await appt()
    return (a or {}).get('vehicle'), round(time.monotonic()-t0)

async def slots(day, **extra):
    r = await post("/get-slots", {"service":"oil change","dates":[day],"dealer_key":KEY, **extra})
    return r

async def main():
    print("END-TO-END SUITE  |  Honda St. Charles  |  live server\n")
    await clear()
    base = await nrec()

    # 1 lookup
    lk = await post("/lookup-customer", {"phone":PHONE})
    rec("lookup finds the caller", bool(lk.get("found")), f"{lk.get('first_name')} {lk.get('last_name')}")
    rec("lookup says there is no appointment",
        "NO upcoming appointment" in (lk.get("agent_instruction") or ""))

    # 2 slots
    s = await slots("2026-10-12")
    rec("get_slots returns times", bool(s.get("slots")), str(s.get("spoken_slots")))

    # 3 book, car already on file
    b = await post("/book-appointment", {"service":"oil change","appointment_time":s["slots"][0],
        "phone":PHONE,"first_name":"Server","last_name":"Test",
        "vehicle_year":"2021","vehicle_make":"Honda","vehicle_model":"Civic","transport":"drop off"})
    rec("book succeeds (car on file)", bool(b.get("success")))
    v, secs = await wait_vehicle()
    rec("correct vehicle attached", v == "2021 Honda Civic", f"{v!r} after {secs}s")
    rec("no duplicate record", await nrec() == base, f"{base} -> {await nrec()}")

    # 4 same-call time change
    s2 = await slots("2026-10-12")
    tgt = next((x for x in s2.get("slots") or [] if x != s["slots"][0]), None)
    if tgt:
        u = await post("/book-appointment", {"service":"oil change","appointment_time":tgt,
            "phone":PHONE,"first_name":"Server","last_name":"Test"})
        rec("same-call time change", bool(u.get("success")) and bool(u.get("rescheduled")))
        a = await appt()
        rec("service survives the reschedule", bool(a and a.get("services")), str((a or {}).get("services")))
        rec("vehicle survives the reschedule", (a or {}).get("vehicle") == "2021 Honda Civic",
            repr((a or {}).get("vehicle")))

    # 5 reschedule to another day
    s3 = await slots("2026-10-13")
    r3 = await post("/book-appointment", {"service":"oil change","appointment_time":s3["slots"][0],
        "phone":PHONE,"first_name":"Server","last_name":"Test"})
    rec("reschedule to another day", bool(r3.get("success")) and bool(r3.get("rescheduled")))
    rec("still no duplicate", await nrec() == base, f"{base} -> {await nrec()}")

    # 6 lookup now sees the appointment
    lk2 = await post("/lookup-customer", {"phone":PHONE})
    rec("lookup reports the appointment", bool(lk2.get("has_existing_appointment")))

    # 7 cancel
    cx = await post("/cancel-appointment", {"phone":PHONE})
    rec("cancel works", bool(cx.get("success")))
    rec("nothing left booked", await appt() is None)

    # 8 caller names a car NOT on file
    s4 = await slots("2026-10-14")
    b4 = await post("/book-appointment", {"service":"oil change","appointment_time":s4["slots"][0],
        "phone":PHONE,"first_name":"Different","last_name":"Name",
        "vehicle_year":"2008","vehicle_make":"Toyota","vehicle_model":"Corolla","transport":"drop off"})
    rec("book succeeds (car NOT on file)", bool(b4.get("success")))
    v4, s4s = await wait_vehicle()
    rec("new car added and attached", v4 == "2008 Toyota Corolla", f"{v4!r} after {s4s}s")
    rec("different name made no duplicate", await nrec() == base, f"{base} -> {await nrec()}")
    await clear()

    # 9 loaner at a store that refuses them
    s5 = await slots("2026-10-15")
    b5 = await post("/book-appointment", {"service":"oil change","appointment_time":s5["slots"][0],
        "phone":PHONE,"first_name":"Server","last_name":"Test","transport":"loaner"})
    rec("loaner still books", bool(b5.get("success")))
    rec("agent told NOT to promise the loaner",
        b5.get("transport_confirmed") is False and "Do NOT tell them a loaner is confirmed" in (b5.get("agent_instruction") or ""))
    await clear()

    # 10 a refused slot is never offered again
    from routes import _REFUSED_SLOTS  # local view only; server holds its own
    s6 = await slots("2026-10-16")
    first = (s6.get("slots") or [None])[0]
    rec("slot list is non-empty for the loop test", bool(first))

    print()
    p = sum(1 for _,ok,_ in RESULTS if ok); n = len(RESULTS)
    print(f"{p}/{n} passed")
    for name, ok, detail in RESULTS:
        if not ok: print("  FAILED:", name, detail)

asyncio.run(main())
