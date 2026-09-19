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

import pytest

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


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """
    Nothing in this suite may touch myKaarma or GHL.

    equity-screen now always looks the customer up — it needs the real
    appointment time, which only myKaarma has. Left unstubbed that turned a
    0.7-second test run into 20 seconds of live API calls against production.
    """
    async def _no_customer(*a, **k):
        return []

    async def _no_push(*a, **k):
        return {"pushed": False, "reason": "stubbed"}

    monkeypatch.setattr(equity.mk, "search_customer", _no_customer)
    # Stash the real one so the tests that are ABOUT the push can still
    # reach it without unwinding this fixture.
    monkeypatch.setattr(equity, "_REAL_PUSH", equity._push_to_ghl, raising=False)
    monkeypatch.setattr(equity, "_push_to_ghl", _no_push)


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


def test_first_yes_notifies_the_desk_that_an_appraisal_is_scheduled():
    """Reid, 19 Sep: "Can we get an alert when somebody says yes to the
    appraisal." Before this the desk heard nothing until the second yes, so a
    customer who stalled at question two was invisible to the store."""
    r = respond(step="value_offer", answer="yes", phone="6305550147",
                first_name="Dan", vehicle_year="2022", vehicle_make="Honda",
                vehicle_model="CR-V", appointment_time="Tue 9:00 AM")
    assert r["appraisal_scheduled"] is True
    assert "equity-appraisal-scheduled" in r["tags"]
    notice = r["appraisal_notice"]
    assert "Dan" in notice and "CR-V" in notice
    # The desk must be told to prepare, NOT to walk over. Those are different
    # instructions and confusing them is the failure Reid warned about.
    assert "walk over" not in notice.lower()
    assert r["fire_salesperson_alert"] is False


def test_appraisal_notice_rides_inside_the_existing_q2_push(monkeypatch):
    """Not its own webhook. GHL inbound triggers are billed per execution and
    the Q2 workflow already runs at this exact moment."""
    sent = []

    async def spy(dealer_key, payload, kind=""):
        sent.append((kind, payload))
        return {"pushed": True}

    monkeypatch.setattr(equity, "_push_to_ghl", spy)
    respond(step="value_offer", answer="yes", phone="6305550147",
            first_name="Dan", vehicle_year="2022", vehicle_make="Honda",
            vehicle_model="CR-V")

    assert [k for k, _ in sent] == ["Q2"], "one push only, no second webhook"
    payload = sent[0][1]
    assert payload["appraisal_notice"]
    # The customer's next text still has to be in there, or the thread stops.
    assert payload["equity_message"] == equity.SEE_OPTIONS_MESSAGE


def test_appraisal_notice_never_states_a_figure():
    r = respond(step="value_offer", answer="yes", phone="6305550147",
                first_name="Dan", vehicle_year="2022", vehicle_make="Honda",
                vehicle_model="CR-V")
    assert "$" not in r["appraisal_notice"]


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
# These override the autouse no-network fixture on purpose: they are ABOUT the
# push, so they spy on it rather than letting it be stubbed away.
def test_ineligible_customers_are_never_pushed(monkeypatch):
    calls = []

    async def spy(dealer_key, payload, kind=""):
        calls.append(kind or "screen")
        return {"pushed": True}

    monkeypatch.setattr(equity, "_push_to_ghl", spy)
    screen(phone="6305550147", vehicle_year="2022", vehicle_make="Honda",
           vehicle_model="CR-V", last_purchase_date=days_ago(60))
    assert calls == [], "an excluded customer must never reach GHL"


def test_eligible_customers_are_pushed_once(monkeypatch):
    calls = []

    async def spy(dealer_key, payload, kind=""):
        calls.append((kind or "screen", payload))
        return {"pushed": True}

    monkeypatch.setattr(equity, "_push_to_ghl", spy)
    screen(phone="6305550147", first_name="Dan", vehicle_year="2022",
           vehicle_make="Honda", vehicle_model="CR-V")
    assert [k for k, _ in calls] == ["screen"]
    sent = calls[0][1]
    assert sent["phone"] == "6305550147"
    assert "equity_message" in sent and "STOP" in sent["equity_message"]


def test_first_yes_pushes_the_second_question(monkeypatch):
    calls = []

    async def spy(dealer_key, payload, kind=""):
        calls.append(kind)
        return {"pushed": True}

    monkeypatch.setattr(equity, "_push_to_ghl", spy)
    respond(step="value_offer", answer="yes", phone="6305550147")
    assert calls == ["Q2"]


def test_second_yes_pushes_the_desk_alert(monkeypatch):
    calls = []

    async def spy(dealer_key, payload, kind=""):
        calls.append((kind, payload))
        return {"pushed": True}

    monkeypatch.setattr(equity, "_push_to_ghl", spy)
    respond(step="see_options", answer="yes", phone="6305550147",
            first_name="Dan", vehicle_year="2022", vehicle_make="Honda",
            vehicle_model="CR-V")
    assert [k for k, _ in calls] == ["ALERT"]
    assert "Dan" in calls[0][1]["alert_card"]


def test_a_no_pushes_nothing(monkeypatch):
    calls = []

    async def spy(dealer_key, payload, kind=""):
        calls.append(kind)
        return {"pushed": True}

    monkeypatch.setattr(equity, "_push_to_ghl", spy)
    respond(step="value_offer", answer="no thanks", phone="6305550147")
    respond(step="value_offer", answer="STOP", phone="6305550147")
    assert calls == []


def test_a_failing_push_never_500s_the_workflow_step(monkeypatch):
    """If the push raises, GHL marks the action failed and the contact drops
    out of the flow silently. It must degrade, not explode."""
    class Boom:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("network down")

    monkeypatch.setenv("EQUITY_WEBHOOK_DEFAULT", "https://example.invalid/hook")
    monkeypatch.setattr(equity.httpx, "AsyncClient", lambda **k: Boom())
    out = asyncio.run(equity._REAL_PUSH("mcgrath_honda_stcharles", {}))
    assert out["pushed"] is False


def test_no_webhook_configured_is_reported_not_raised(monkeypatch):
    monkeypatch.delenv("EQUITY_WEBHOOK_DEFAULT", raising=False)
    monkeypatch.delenv("EQUITY_WEBHOOK_MCGRATH_HONDA_STCHARLES", raising=False)
    out = asyncio.run(equity._REAL_PUSH("mcgrath_honda_stcharles", {}))
    assert out == {"pushed": False, "reason": "no_webhook_configured"}


def test_per_store_env_var_wins_over_the_default(monkeypatch):
    monkeypatch.setenv("EQUITY_WEBHOOK_DEFAULT", "https://default/hook")
    monkeypatch.setenv("EQUITY_WEBHOOK_MCGRATH_HONDA_STCHARLES", "https://store/hook")
    monkeypatch.setenv("EQUITY_WEBHOOK_Q2_DEFAULT", "https://q2/hook")
    assert equity._equity_webhook_url("mcgrath_honda_stcharles") == "https://store/hook"
    assert equity._equity_webhook_url("mcgrath_kia_stcharles") == "https://default/hook"
    # Each purpose resolves to its own workflow.
    assert equity._equity_webhook_url("mcgrath_kia_stcharles", "Q2") == "https://q2/hook"


# ── Working out which question a reply answers ───────────────────────────────
def test_the_server_remembers_which_question_is_outstanding():
    """This account's GHL offers no merge field for contact tags, so the reply
    webhook cannot tell us which question it answers. The server tracks it."""
    equity._AWAITING.clear()
    first = respond(answer="yes", phone="6305550147")
    assert first["step"] == "value_offer"
    assert first["fire_salesperson_alert"] is False

    # The first yes pushed question two, so the NEXT reply answers that one.
    second = respond(answer="yes", phone="6305550147")
    assert second["step"] == "see_options"
    assert second["fire_salesperson_alert"] is True

    # And it is cleared afterwards, so a later visit starts from the top.
    third = respond(answer="yes", phone="6305550147")
    assert third["step"] == "value_offer"


def test_tags_win_when_ghl_does_send_them():
    equity._AWAITING.clear()
    for tags in ("equity-texted, equity-value-yes",
                 ["equity-texted", "equity-value-yes"]):
        r = respond(answer="yes", phone="6305550147", tags=tags)
        assert r["step"] == "see_options", tags
        equity._AWAITING.clear()


def test_a_stale_conversation_starts_over():
    equity._AWAITING.clear()
    respond(answer="yes", phone="6305550147")
    equity._AWAITING["6305550147"] -= equity.AWAITING_TTL_SECONDS + 1
    assert respond(answer="yes", phone="6305550147")["step"] == "value_offer"


def test_explicit_step_still_wins():
    r = respond(step="value_offer", answer="yes", phone="6305550147",
                tags="equity-texted, equity-value-yes")
    assert r["step"] == "value_offer"


def test_stop_works_whichever_question_they_are_on():
    equity._AWAITING.clear()
    assert respond(answer="STOP", phone="6305550147")["answer"] == "opt_out"
    respond(answer="yes", phone="6305550147")          # now on question two
    assert respond(answer="STOP", phone="6305550147")["answer"] == "opt_out"


# ── The reply text going missing (19 Sep) ────────────────────────────────────
# GHL's webhook step mapped {{message.body}} into `answer`, and it arrived
# empty. A plain "yes" scored "unclear" and the thread stopped dead. From the
# GHL execution log that is indistinguishable from a customer typing something
# ambiguous, which is why it took a day to find.

def test_reply_text_is_recovered_when_answer_arrives_empty():
    r = respond(step="value_offer", answer="", phone="6305550147",
                first_name="Dan", message="yes")
    assert r["answer"] == "yes"
    assert r["next_step"] == "see_options"


@pytest.mark.parametrize("key", ["message", "message_body", "messageBody",
                                 "body", "last_message", "sms", "text"])
def test_every_known_message_key_is_accepted(key):
    r = respond(**{"step": "value_offer", "phone": "6305550147", key: "yes"})
    assert r["answer"] == "yes"


def test_nested_message_object_is_read():
    r = respond(step="value_offer", phone="6305550147",
                message={"body": "sure thing"})
    assert r["answer"] == "yes"


def test_an_explicit_answer_still_wins_over_the_extras():
    r = respond(step="value_offer", answer="no thanks", phone="6305550147",
                message="yes")
    assert r["answer"] == "no"


def test_stop_is_honoured_when_it_arrives_in_an_extra_field():
    r = respond(step="value_offer", phone="6305550147", message="STOP")
    assert r["answer"] == "opt_out"
    assert "equity-opted-out" in r["tags"]


def test_unclear_echoes_what_actually_arrived():
    """So the GHL log distinguishes "nothing was sent" from "they typed
    something odd" -- the two look identical otherwise."""
    empty = respond(step="value_offer", phone="6305550147")
    assert empty["answer"] == "unclear"
    assert empty["received"] is None

    # "maybe later" reads as a decline, so pick something genuinely ambiguous.
    odd = respond(step="value_offer", answer="what do you mean",
                  phone="6305550147")
    assert odd["answer"] == "unclear"
    assert odd["received"] == "what do you mean"


def test_unclear_on_the_second_question_echoes_too():
    r = respond(step="see_options", answer="what do you mean",
                phone="6305550147")
    assert r["answer"] == "unclear"
    assert r["received"] == "what do you mean"


# ── The alert card losing the car (19 Sep live test) ─────────────────────────
# GHL's reply webhook sends phone, name, answer and step -- nothing about the
# vehicle. The desk alert came out as:
#
#     Shafique - vehicle
#     Priority 0/100 (COLD)
#
# which tells a salesperson who to find and no reason to get up. The vehicle
# now comes from myKaarma on the reply too, not just on the screen.

def _fake_match(label="2022 Honda CR-V", first="Dan"):
    year, make, model = label.split(" ", 2)
    return [{
        "uuid": "cust-1", "fname": first, "lname": "Tester",
        "vehicles": [{"uuid": "veh-1", "year": year, "make": make,
                      "model": model, "vin": "X" * 17}],
    }]


@pytest.fixture
def mykaarma_knows_the_car(monkeypatch):
    async def _search(*a, **k):
        return _fake_match()

    async def _appts(*a, **k):
        return []

    monkeypatch.setattr(equity.mk, "search_customer", _search)
    monkeypatch.setattr(equity.mk, "get_customer_appointments", _appts)


def test_second_yes_recovers_the_vehicle_from_mykaarma(mykaarma_knows_the_car):
    r = respond(step="see_options", answer="yes sure", phone="6305550147",
                first_name="Shafique", appointment_time="today")
    assert r["fire_salesperson_alert"] is True
    assert "CR-V" in r["alert_card"], r["alert_card"]
    # The whole point of the score is telling the desk who to see first.
    assert r["priority_score"] > 0
    assert r["priority_band"] != COLD


def test_appraisal_notice_also_names_the_car(mykaarma_knows_the_car):
    r = respond(step="value_offer", answer="yes", phone="6305550147",
                first_name="Shafique")
    assert "CR-V" in r["appraisal_notice"]
    assert r["priority_score"] > 0


def test_lookup_is_skipped_when_ghl_already_sent_everything(monkeypatch):
    """One round trip per reply is fine; a pointless one is not."""
    calls = []

    async def _search(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(equity.mk, "search_customer", _search)
    respond(step="see_options", answer="yes", phone="6305550147",
            first_name="Dan", vehicle_year="2022", vehicle_make="Honda",
            vehicle_model="CR-V", appointment_time="Tue 9:00 AM")
    assert calls == []


def test_a_failing_lookup_still_sends_the_customer_their_reply(monkeypatch):
    async def _boom(*a, **k):
        raise equity.mk.MyKaarmaError(503, "myKaarma down", "search")

    monkeypatch.setattr(equity.mk, "search_customer", _boom)
    r = respond(step="see_options", answer="yes", phone="6305550147",
                first_name="Dan")
    # Degraded alert, unbroken thread.
    assert r["fire_salesperson_alert"] is True
    assert r["next_message"]
