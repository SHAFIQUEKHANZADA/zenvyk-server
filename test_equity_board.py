"""
Tests for the salesperson claim screen.

The page is one static HTML string, so these check the things that would make
it fail silently on a showroom floor: that it is actually served, that the
store it was opened for is baked in, and that a browser can't cache a stale
board -- a salesperson reading a five-minute-old page walks up to a customer
someone else has already spoken to.
"""

import pytest
from fastapi.testclient import TestClient

import equity
from main import app

client = TestClient(app)


def setup_function():
    equity._CLAIMS.clear()


def test_the_board_is_served():
    r = client.get("/mykaarma/equity-board")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "Trade Alerts" in r.text


def test_the_board_is_never_cached():
    r = client.get("/mykaarma/equity-board")
    assert "no-store" in r.headers.get("cache-control", "")


def test_the_board_is_not_indexable():
    r = client.get("/mykaarma/equity-board")
    assert "noindex" in r.headers.get("x-robots-tag", "")


def test_the_store_is_baked_into_the_page():
    r = client.get("/mykaarma/mcgrath_honda_stcharles/equity-board")
    assert r.status_code == 200
    assert "mcgrath_honda_stcharles" in r.text


def test_the_group_board_has_no_store_filter():
    r = client.get("/mykaarma/equity-board")
    assert 'var DEALER = "";' in r.text


def test_no_placeholder_survives_into_the_page():
    """A missed substitution would render the board inert with no error."""
    for path in ("/mykaarma/equity-board",
                 "/mykaarma/mcgrath_honda_stcharles/equity-board"):
        assert "__DEALER__" not in client.get(path).text, path
        assert "__API__" not in client.get(path).text, path


def test_the_board_route_is_not_swallowed_by_the_per_store_pattern():
    """/mykaarma/{dealer_key}/equity-board and /mykaarma/equity-board are
    different routes; registration order decides which wins."""
    r = client.get("/mykaarma/equity-board")
    assert r.status_code == 200
    assert "Trade Alerts" in r.text


def test_the_page_is_self_contained():
    """No CDN, no build step. It has to load on dealership wifi on an old
    phone in the seconds before someone walks to the lounge."""
    html = client.get("/mykaarma/equity-board").text
    assert "<script src=" not in html
    assert "cdn" not in html.lower()
    assert 'rel="stylesheet"' not in html


def test_the_board_endpoint_it_calls_actually_exists():
    r = client.get("/mykaarma/equity-claims")
    assert r.status_code == 200
    body = r.json()
    assert "claims" in body and "unclaimed" in body


def test_a_live_claim_shows_up_on_the_board_feed():
    import time
    equity._CLAIMS["6305550147"] = {
        "phone": "6305550147", "name": "Dan", "vehicle": "2022 Honda CR-V",
        "dealer_key": "mcgrath_honda_stcharles", "appointment_time": "Tue 9:30 AM",
        "priority_score": 72, "priority_band": "hot", "reasons": ["4 years old"],
        "status": "unclaimed", "salesperson": None, "at": time.time(),
    }
    body = client.get("/mykaarma/equity-claims").json()
    assert body["count"] == 1
    assert body["unclaimed"] == 1
    row = body["claims"][0]
    assert row["name"] == "Dan"
    assert "waiting_seconds" in row
    # The page reads these exact keys; renaming one silently empties the card.
    for key in ("phone", "name", "vehicle", "priority_band", "priority_score",
                "status", "salesperson", "appointment_time", "reasons"):
        assert key in row, key


def test_two_salespeople_cannot_claim_the_same_customer():
    import time
    equity._CLAIMS["6305550147"] = {
        "phone": "6305550147", "name": "Dan", "vehicle": "2022 Honda CR-V",
        "dealer_key": None, "appointment_time": None,
        "priority_score": 72, "priority_band": "hot", "reasons": [],
        "status": "unclaimed", "salesperson": None, "at": time.time(),
    }
    first = client.post("/mykaarma/equity-claim", json={
        "phone": "6305550147", "salesperson": "Mitch", "action": "claim"}).json()
    assert first["success"] is True

    second = client.post("/mykaarma/equity-claim", json={
        "phone": "6305550147", "salesperson": "Juan", "action": "claim"}).json()
    assert second["success"] is False
    assert second["error"] == "already_claimed"
    # The board shows this message, so it has to name who won.
    assert "Mitch" in second["message"]


def test_an_empty_board_still_paints():
    """The board only redraws when its signature changes. An empty board's
    signature is the empty string, so starting lastSig at "" skipped the very
    first paint and the page stayed blank until a customer appeared."""
    html = client.get("/mykaarma/equity-board").text
    assert "var lastSig = null;" in html
    assert 'lastSig = "";' not in html
    # And the empty state it should be painting is actually in the page.
    assert "Nothing waiting" in html
