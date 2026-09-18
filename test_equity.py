"""
Tests for the service drive trade equity flow.

Run:  .venv/Scripts/python.exe -m pytest test_equity.py -q

These lock down Reid's design decisions, not the arithmetic:
  * the salesperson alert waits for the SECOND yes
  * nobody who bought in the last 12 months gets the text
  * no message ever states a dollar figure
  * two salespeople cannot claim the same customer
"""

import asyncio
from datetime import datetime, timedelta

import equity
from equity import (
    COLD,
    HOT,
    ClaimRequest,
    ResponseRequest,
    ScreenRequest,
    _is_yes,
    _parse_mileage,
    equity_claim,
    equity_claims,
    equity_response,
    equity_screen,
)


def screen(**kw):
    return asyncio.run(equity_screen(ScreenRequest(**kw)))


def respond(**kw):
    return asyncio.run(equity_response(ResponseRequest(**kw)))


def claim(**kw):
    return asyncio.run(equity_claim(ClaimRequest(**kw)))


def days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def setup_function():
    equity._CLAIMS.clear()


# ── Reading replies off a phone ───────────────────────────────────────────────
def test_is_yes_reads_how_people_actually_text():
    for s in ("yes", "Yes please", "yeah", "yep", "sure", "ok", "Y", "sounds good"):
        assert _is_yes(s) is True, s
    for s in ("no", "No thanks", "nope", "not today", "not interested", "STOP"):
        assert _is_yes(s) is False, s
    # Anything we can't read must NOT be guessed at.
    assert _is_yes("what do you mean") is None
    assert _is_yes("") is None
    assert _is_yes(None) is None


def test_parse_mileage_handles_spoken_and_typed():
    assert _parse_mileage("84,000") == 84000
    assert _parse_mileage("about 84k") == 84000
    assert _parse_mileage(62000) == 62000
    assert _parse_mileage("no idea") is None


# ── Screening: Reid's exclusions ──────────────────────────────────────────────
def test_recent_purchase_is_excluded():
    """Reid: 'we need to make sure this excludes purchases from past 12 months.'"""
    r = screen(phone="6305550147", vehicle_year="2022", vehicle_make="Honda",
               vehicle_model="CR-V", last_purchase_date=days_ago(90))
    assert r["eligible"] is False
    assert "equity-skip-recent-purchase" in r["tags"]
    assert r["message"] is None


def test_purchase_older_than_a_year_is_fine():
    r = screen(phone="6305550147", vehicle_year="2022", vehicle_make="Honda",
               vehicle_model="CR-V", last_purchase_date=days_ago(400))
    assert r["eligible"] is True


def test_recent_decline_is_suppressed():
    r = screen(phone="6305550147", vehicle_year="2022", vehicle_make="Honda",
               vehicle_model="CR-V", last_declined_date=days_ago(30))
    assert r["eligible"] is False
    assert "equity-skip-cooldown" in r["tags"]


def test_campaign_collision_is_suppressed():
    r = screen(phone="6305550147", vehicle_year="2022", vehicle_make="Honda",
               vehicle_model="CR-V", in_active_campaign=True)
    assert r["eligible"] is False
    assert "equity-skip-collision" in r["tags"]


def test_very_old_vehicle_is_skipped():
    r = screen(phone="6305550147", vehicle_year="2005", vehicle_make="Honda",
               vehicle_model="Civic")
    assert r["eligible"] is False


def test_eligible_customer_gets_a_compliant_message():
    r = screen(phone="6305550147", first_name="Dan", vehicle_year="2022",
               vehicle_make="Honda", vehicle_model="CR-V", mileage="34k",
               appointment_day="Tuesday")
    assert r["eligible"] is True
    msg = r["message"]
    # Marketing text -> must carry an opt-out.
    assert "STOP" in msg
    # "Appraisal" is regulated language in several states.
    assert "appraisal" not in msg.lower()
    # We have no valuation source. No figure may ever appear.
    assert "$" not in msg
    assert "Dan" in msg
    # Sent while they are AT the dealership, so it must say so - the
    # salesperson walk-over only works if the customer is on site.
    assert "today" in msg.lower()


# ── The two-step thread ───────────────────────────────────────────────────────
def test_first_yes_does_not_fire_the_alert():
    """Reid was explicit: wanting a number is not wanting to be approached."""
    r = respond(step="value_offer", answer="yes", phone="6305550147",
                vehicle_year="2022", vehicle_make="Honda", vehicle_model="CR-V")
    assert r["fire_salesperson_alert"] is False
    assert r["next_step"] == "see_options"


def test_second_yes_fires_the_alert():
    r = respond(step="see_options", answer="sure", phone="6305550147",
                first_name="Dan", vehicle_year="2022", vehicle_make="Honda",
                vehicle_model="CR-V", mileage="34k",
                appointment_time="Tue 9:00 AM")
    assert r["fire_salesperson_alert"] is True
    assert "Dan" in r["alert_card"]
    assert r["claim_timeout_seconds"] == equity.CLAIM_TIMEOUT_SECONDS
    # Still no figure anywhere in what the customer receives.
    assert "$" not in r["next_message"]


def test_no_to_options_still_gives_them_the_number_in_person():
    r = respond(step="see_options", answer="no thanks", phone="6305550147")
    assert r["fire_salesperson_alert"] is False
    assert "equity-options-no" in r["tags"]


def test_stop_is_an_opt_out_not_a_no():
    r = respond(step="value_offer", answer="STOP", phone="6305550147")
    assert r["answer"] == "opt_out"
    assert "equity-opted-out" in r["tags"]
    assert r["next_message"] is None


def test_unclear_reply_is_routed_to_a_human():
    r = respond(step="value_offer", answer="what do you mean?",
                phone="6305550147")
    assert r["answer"] == "unclear"
    assert r["fire_salesperson_alert"] is False
    assert r["next_message"] is None


# ── The claim board ───────────────────────────────────────────────────────────
def test_two_salespeople_cannot_claim_the_same_customer():
    respond(step="see_options", answer="yes", phone="6305550147",
            first_name="Dan", vehicle_year="2022", vehicle_make="Honda",
            vehicle_model="CR-V")

    first = claim(phone="6305550147", salesperson="Mitch", action="claim")
    assert first["success"] is True

    second = claim(phone="6305550147", salesperson="Brad", action="claim")
    assert second["success"] is False
    assert second["error"] == "already_claimed"
    assert second["claimed_by"] == "Mitch"


def test_release_puts_it_back_on_the_board():
    respond(step="see_options", answer="yes", phone="6305550147",
            vehicle_year="2022", vehicle_make="Honda", vehicle_model="CR-V")
    claim(phone="6305550147", salesperson="Mitch", action="claim")
    claim(phone="6305550147", salesperson="Mitch", action="release")
    assert claim(phone="6305550147", salesperson="Brad",
                 action="claim")["success"] is True


def test_presented_and_sold_are_logged_for_the_funnel():
    respond(step="see_options", answer="yes", phone="6305550147",
            vehicle_year="2022", vehicle_make="Honda", vehicle_model="CR-V")
    claim(phone="6305550147", salesperson="Mitch", action="claim")
    assert claim(phone="6305550147", action="presented")["status"] == "presented"
    assert claim(phone="6305550147", action="sold")["status"] == "sold"


def test_claiming_something_that_does_not_exist():
    assert claim(phone="0000000000", salesperson="Mitch",
                 action="claim")["success"] is False


def test_claim_times_out_to_the_bdc():
    respond(step="see_options", answer="yes", phone="6305550147",
            vehicle_year="2022", vehicle_make="Honda", vehicle_model="CR-V")
    claim(phone="6305550147", salesperson="Mitch", action="claim")
    # Wind the clock past the timeout rather than sleeping through it.
    equity._CLAIMS["6305550147"]["at"] -= equity.CLAIM_TIMEOUT_SECONDS + 1
    board = asyncio.run(equity_claims())
    assert board["timed_out"] == 1


def test_board_sorts_highest_priority_first():
    respond(step="see_options", answer="yes", phone="1111111111",
            first_name="Old", vehicle_year="2016", vehicle_make="Honda",
            vehicle_model="Civic", mileage="160k")
    respond(step="see_options", answer="yes", phone="2222222222",
            first_name="Prime", vehicle_year="2023", vehicle_make="Acura",
            vehicle_model="MDX", mileage="18k", is_lease=True,
            lease_months_remaining=3)
    board = asyncio.run(equity_claims())
    assert board["claims"][0]["name"] == "Prime"
    assert board["claims"][0]["priority_band"] == HOT


def test_split_label_pulls_the_model_out():
    """myKaarma gives one string; the SMS copy needs the model alone."""
    from equity import _split_label
    assert _split_label("2020 Honda Accord") == (2020, "Honda", "Accord")
    assert _split_label("2022 Honda CR-V") == (2022, "Honda", "CR-V")
    assert _split_label("2023 Acura MDX A-Spec") == (2023, "Acura", "MDX A-Spec")
    assert _split_label(None) == (None, None, None)
    assert _split_label("Honda") == (None, None, "Honda")


def test_days_since_handles_every_format_ghl_sends():
    """A date we can't read means the exclusion silently doesn't apply, so all
    of GHL's shapes have to parse."""
    from equity import _days_since
    ninety = datetime.now() - timedelta(days=90)
    for value in (
        ninety.strftime("%Y-%m-%d"),
        ninety.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z",
        ninety.strftime("%Y-%m-%dT%H:%M:%S") + "+00:00",
        ninety.strftime("%m/%d/%Y"),
        str(int(ninety.timestamp() * 1000)),   # epoch millis
        str(int(ninety.timestamp())),          # epoch seconds
    ):
        got = _days_since(value)
        assert got is not None and 89 <= got <= 91, (value, got)

    # Empty / missing must stay None, not raise.
    assert _days_since("") is None
    assert _days_since(None) is None
    assert _days_since("   ") is None
    assert _days_since("not a date") is None


def test_iso_date_from_ghl_still_blocks_a_recent_purchase():
    recent = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"
    r = screen(phone="6305550147", vehicle_year="2022", vehicle_make="Honda",
               vehicle_model="CR-V", last_purchase_date=recent)
    assert r["eligible"] is False
    assert "equity-skip-recent-purchase" in r["tags"]


def test_unknown_mileage_is_treated_as_average_not_as_zero():
    """myKaarma doesn't return mileage and GHL has no field for it, so unknown
    is the NORMAL case. Scoring it as zero made every real customer cold."""
    r = screen(phone="6305550147", first_name="Shafique", vehicle_year="2021",
               vehicle_make="Honda", vehicle_model="Accord")
    assert r["eligible"] is True
    assert r["priority_band"] != COLD, r["priority_score"]
    assert any("unknown" in x.lower() for x in r["priority_reasons"])


def test_known_low_mileage_still_beats_unknown():
    low = screen(phone="1", vehicle_year="2021", vehicle_make="Honda",
                 vehicle_model="Accord", mileage="30k")["priority_score"]
    unknown = screen(phone="2", vehicle_year="2021", vehicle_make="Honda",
                     vehicle_model="Accord")["priority_score"]
    high = screen(phone="3", vehicle_year="2021", vehicle_make="Honda",
                  vehicle_model="Accord", mileage="140k")["priority_score"]
    assert low > unknown > high


# ── The GHL push ──────────────────────────────────────────────────────────────
def test_no_webhook_configured_does_not_break_the_response(monkeypatch):
    monkeypatch.delenv("EQUITY_WEBHOOK_DEFAULT", raising=False)
    r = screen(phone="6305550147", vehicle_year="2022", vehicle_make="Honda",
               vehicle_model="CR-V")
    assert r["eligible"] is True
    assert r["ghl"]["pushed"] is False


def test_a_failing_push_never_500s_the_workflow_step(monkeypatch):
    """If the push raises, GHL marks the whole action failed and the contact
    drops out of the flow silently. It must degrade, not explode."""
    import equity as eq

    class Boom:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("network down")

    monkeypatch.setenv("EQUITY_WEBHOOK_DEFAULT", "https://example.invalid/hook")
    monkeypatch.setattr(eq.httpx, "AsyncClient", lambda **k: Boom())
    r = screen(phone="6305550147", vehicle_year="2022", vehicle_make="Honda",
               vehicle_model="CR-V")
    assert r["eligible"] is True
    assert r["ghl"]["pushed"] is False


def test_ineligible_customers_are_never_pushed(monkeypatch):
    import equity as eq
    calls = []

    class Spy:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, **k):
            calls.append(url)
            class R: status_code = 200; text = ""
            return R()

    monkeypatch.setenv("EQUITY_WEBHOOK_DEFAULT", "https://example.invalid/hook")
    monkeypatch.setattr(eq.httpx, "AsyncClient", lambda **k: Spy())
    screen(phone="6305550147", vehicle_year="2022", vehicle_make="Honda",
           vehicle_model="CR-V", last_purchase_date=days_ago(60))
    assert calls == [], "an excluded customer must never reach GHL"


def test_per_store_env_var_wins_over_the_default(monkeypatch):
    from equity import _equity_webhook_url
    monkeypatch.setenv("EQUITY_WEBHOOK_DEFAULT", "https://default/hook")
    monkeypatch.setenv("EQUITY_WEBHOOK_MCGRATH_HONDA_STCHARLES", "https://store/hook")
    assert _equity_webhook_url("mcgrath_honda_stcharles") == "https://store/hook"
    assert _equity_webhook_url("mcgrath_kia_stcharles") == "https://default/hook"


def test_outbound_sms_stays_in_the_gsm_alphabet():
    """A single em dash drops SMS from 160-char segments to 70, doubling the
    carrier cost of every send. Nothing customer-facing may leave GSM-7."""
    from equity import (CONFIRM_MESSAGE, DECLINE_MESSAGE, SEE_OPTIONS_MESSAGE,
                        VALUE_ONLY_MESSAGE, _onsite_message)
    GSM = set(
        "@£$¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà\n\r"
    ) | set("^{}[~]|€") | {"\\"}
    messages = [
        _onsite_message("Shafique", "Accord", "Tuesday"),
        _onsite_message(None, None, None),
        SEE_OPTIONS_MESSAGE, CONFIRM_MESSAGE, DECLINE_MESSAGE, VALUE_ONLY_MESSAGE,
        screen(phone="6305550147", first_name="Dan", vehicle_year="2022",
               vehicle_make="Honda", vehicle_model="CR-V")["message"],
    ]
    for m in messages:
        bad = sorted({c for c in m if c not in GSM})
        assert not bad, f"non-GSM {bad} in: {m[:70]}"


def test_alert_card_to_the_sales_desk_is_also_gsm_safe():
    r = respond(step="see_options", answer="yes", phone="6305550147",
                first_name="Dan", vehicle_year="2021", vehicle_make="Honda",
                vehicle_model="Accord")
    assert "\u2014" not in r["alert_card"], "em dash in the desk SMS"
