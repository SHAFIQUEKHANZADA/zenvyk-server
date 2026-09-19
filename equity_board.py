"""
The salesperson's claim screen.

Reid's mock has a CLAIM button on the alert. This is that button, on a page a
salesperson can keep open on their phone while they work the floor.

    GET /mykaarma/equity-board                  group view
    GET /mykaarma/{dealer_key}/equity-board     one store

It is deliberately one self-contained HTML file with no build step, no
framework and no CDN. It has to load over dealership wifi on a five-year-old
Android in the two seconds between an alert arriving and someone walking to
the lounge. Every byte it needs is in this string.

WHAT IT TALKS TO
----------------
    GET  /mykaarma/equity-claims?dealer_key=   the board, highest priority first
    POST /mykaarma/equity-claim                claim / release / presented / sold

Both already exist and are unchanged. This file adds no state of its own.

WHO IS THE SALESPERSON
----------------------
Asked once and kept in the browser. There is no login: the board lives behind
the same obscure URL the rest of the connector does, and a login screen on a
showroom floor means the page is closed and the lead is lost. The name is only
used to show who took which customer -- nothing is authorised by it.

WHY IT POLLS
------------
Every five seconds, not a websocket. Claims live in the connector's memory and
expire in minutes; a dropped socket on hotel-grade wifi would leave the board
silently frozen, showing a lead as unclaimed while someone is already walking
over. A poll that fails just retries.
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(prefix="/mykaarma", tags=["Equity Mining"])


BOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Trade Alerts</title>
<style>
  :root{
    --bg:#f4f5f7; --card:#fff; --ink:#15181d; --muted:#6b7280; --line:#e3e6ea;
    --hot:#d92d20; --warm:#b54708; --cold:#475467;
    --go:#039855; --go-press:#027a48; --flat:#f2f4f7;
  }
  *{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
  html,body{margin:0;background:var(--bg);color:var(--ink);
    font:16px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
  header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid var(--line);
    padding:14px 16px calc(14px + env(safe-area-inset-bottom,0)) 16px;
    display:flex;align-items:center;gap:12px}
  h1{font-size:17px;margin:0;font-weight:650;letter-spacing:-.01em}
  .who{margin-left:auto;font-size:13px;color:var(--muted);
    background:var(--flat);border:0;padding:7px 11px;border-radius:99px;font:inherit;
    font-size:13px;cursor:pointer}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--go);flex:none}
  .dot.stale{background:#d0d5dd}
  main{padding:14px 16px 40px;max-width:640px;margin:0 auto}

  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
    padding:15px;margin-bottom:12px;box-shadow:0 1px 2px rgba(16,24,40,.05)}
  .card.hot{border-left:4px solid var(--hot)}
  .card.warm{border-left:4px solid var(--warm)}
  .card.cold{border-left:4px solid var(--cold)}
  .card.mine{background:#f6fef9;border-color:#a6f4c5}

  .top{display:flex;align-items:baseline;gap:10px;margin-bottom:2px}
  .name{font-size:19px;font-weight:650;letter-spacing:-.01em}
  .band{margin-left:auto;font-size:11px;font-weight:700;letter-spacing:.06em;
    text-transform:uppercase}
  .band.hot{color:var(--hot)} .band.warm{color:var(--warm)} .band.cold{color:var(--cold)}
  .car{color:var(--muted);font-size:15px}
  .when{color:var(--muted);font-size:14px;margin-top:5px}
  .why{margin:10px 0 0;padding:0;list-style:none;font-size:13px;color:var(--muted)}
  .why li{padding-left:13px;position:relative;margin-top:3px}
  .why li:before{content:"";position:absolute;left:3px;top:8px;width:3px;height:3px;
    border-radius:50%;background:#98a2b3}

  .wait{font-variant-numeric:tabular-nums}
  .wait.late{color:var(--hot);font-weight:600}

  .row{display:flex;gap:8px;margin-top:13px}
  button.act{flex:1;border:0;border-radius:10px;padding:13px 10px;font:inherit;
    font-weight:600;cursor:pointer;min-height:46px}
  .claim{background:var(--go);color:#fff;font-size:17px}
  .claim:active{background:var(--go-press)}
  .sub{background:var(--flat);color:var(--ink)}
  .sub:active{background:#e4e7ec}
  button[disabled]{opacity:.5}

  .taken{margin-top:12px;font-size:14px;color:var(--muted);
    background:var(--flat);border-radius:9px;padding:10px 12px}
  .empty{text-align:center;color:var(--muted);padding:56px 20px}
  .empty b{display:block;color:var(--ink);font-size:17px;margin-bottom:5px;font-weight:600}
  .err{background:#fffbfa;border:1px solid #fecdca;color:#b42318;border-radius:10px;
    padding:11px 13px;font-size:14px;margin-bottom:12px}

  @media (prefers-color-scheme:dark){
    :root{--bg:#0e1116;--card:#171b22;--ink:#e9edf3;--muted:#98a2b3;--line:#262c36;
      --flat:#222833;--cold:#98a2b3}
    header{background:#171b22}
    .card.mine{background:#0f2018;border-color:#085d3a}
    .err{background:#2a1512;border-color:#7a271a;color:#fda29b}
  }
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>Trade Alerts</h1>
  <button class="who" id="who" type="button"></button>
</header>
<main>
  <div id="err" class="err" hidden></div>
  <div id="board"></div>
</main>

<script>
(function(){
  "use strict";
  var DEALER = "__DEALER__";
  var API    = "__API__";
  var POLL   = 5000;

  // The salesperson's name lives in the browser. It labels a claim so the room
  // knows who went; it authorises nothing, so there is nothing here to steal.
  var me = "";
  try { me = localStorage.getItem("equity_me") || ""; } catch (e) {}

  var board = document.getElementById("board");
  var errEl = document.getElementById("err");
  var whoEl = document.getElementById("who");
  var dot   = document.getElementById("dot");

  // Buttons the finger is already on must not be yanked away by a poll landing
  // mid-tap, so a card is only redrawn when something about it actually changed.
  var lastSig = "";
  var busy = {};

  function askName(force){
    var v = prompt("Your name (shown when you claim a customer):", me || "");
    if (v === null) return;
    v = v.trim();
    if (!v && !force) return;
    me = v || "Sales";
    try { localStorage.setItem("equity_me", me); } catch (e) {}
    whoEl.textContent = me;
    lastSig = "";
    load();
  }
  whoEl.textContent = me || "Set your name";
  whoEl.onclick = function(){ askName(false); };

  function esc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  function waited(sec){
    sec = Math.max(0, sec|0);
    var m = Math.floor(sec/60), s = sec%60;
    return m + ":" + (s<10?"0":"") + s;
  }

  function card(c){
    var band = (c.priority_band || "cold").toLowerCase();
    var mine = c.status === "claimed" && c.salesperson === me;
    var late = c.status === "unclaimed" && c.waiting_seconds > 120;
    var why  = (c.reasons || []).slice(0,3).map(function(r){
      return "<li>" + esc(r) + "</li>";
    }).join("");

    var h = '<div class="card ' + band + (mine ? " mine" : "") + '" data-phone="' +
      esc(c.phone) + '">' +
      '<div class="top"><span class="name">' + esc(c.name || "Customer") + '</span>' +
      '<span class="band ' + band + '">' + esc(band) + " " +
      (c.priority_score|0) + "</span></div>" +
      '<div class="car">' + esc(c.vehicle || "vehicle") + "</div>" +
      '<div class="when">In for service: ' + esc(c.appointment_time || "today") +
      ' &middot; waiting <span class="wait' + (late ? " late" : "") + '">' +
      waited(c.waiting_seconds) + "</span></div>" +
      (why ? '<ul class="why">' + why + "</ul>" : "");

    if (c.status === "unclaimed" || c.status === "timed_out") {
      h += '<div class="row"><button class="act claim" data-do="claim">' +
        (c.status === "timed_out" ? "Nobody went - claim" : "Claim") +
        "</button></div>";
    } else if (mine) {
      h += '<div class="row">' +
        '<button class="act sub" data-do="presented">Presented</button>' +
        '<button class="act sub" data-do="sold">Sold</button>' +
        '<button class="act sub" data-do="release">Release</button></div>';
    } else if (c.status === "claimed") {
      h += '<div class="taken">' + esc(c.salesperson || "Someone") +
        " is walking over</div>";
    } else {
      h += '<div class="taken">' + esc(c.status) + "</div>";
    }
    return h + "</div>";
  }

  function render(rows){
    if (!rows.length) {
      board.innerHTML = '<div class="empty"><b>Nothing waiting</b>' +
        "A customer who asks about their trade shows up here.</div>";
      return;
    }
    board.innerHTML = rows.map(card).join("");
    Array.prototype.forEach.call(board.querySelectorAll("button[data-do]"), function(b){
      b.onclick = function(){ act(b, b.closest(".card").dataset.phone, b.dataset.do); };
    });
  }

  function act(btn, phone, action){
    if (busy[phone]) return;
    if (!me) { askName(true); if (!me) return; }
    busy[phone] = 1;
    btn.disabled = true;
    fetch(API + "/equity-claim", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({phone: phone, salesperson: me, action: action,
                            dealer_key: DEALER || null})
    }).then(function(r){ return r.json(); }).then(function(out){
      // Two people tapping Claim at the same moment is the whole reason the
      // lock exists -- say who won rather than silently doing nothing.
      if (!out.success && out.error === "already_claimed") {
        show(out.message || "Someone else got there first.");
      } else if (!out.success) {
        show(out.message || "That didn't go through.");
      } else {
        show("");
      }
    }).catch(function(){
      show("No connection. Try again.");
    }).then(function(){
      delete busy[phone];
      lastSig = "";
      load();
    });
  }

  function show(msg){
    errEl.textContent = msg;
    errEl.hidden = !msg;
  }

  function load(){
    fetch(API + "/equity-claims" + (DEALER ? "?dealer_key=" + encodeURIComponent(DEALER) : ""),
          {cache: "no-store"})
      .then(function(r){ return r.json(); })
      .then(function(d){
        dot.className = "dot";
        var rows = d.claims || [];
        // Redraw only on a real change, so a tap is never interrupted by a
        // poll. The waiting clock is ticked separately below.
        var sig = rows.map(function(c){
          return [c.phone, c.status, c.salesperson, c.priority_score].join("|");
        }).join(";");
        if (sig !== lastSig) { lastSig = sig; render(rows); }
        else { tick(rows); }
      })
      .catch(function(){ dot.className = "dot stale"; });
  }

  function tick(rows){
    rows.forEach(function(c){
      var el = board.querySelector('.card[data-phone="' +
        String(c.phone).replace(/"/g, '\\\\"') + '"] .wait');
      if (!el) return;
      el.textContent = waited(c.waiting_seconds);
      el.className = "wait" + (c.status === "unclaimed" &&
        c.waiting_seconds > 120 ? " late" : "");
    });
  }

  load();
  setInterval(load, POLL);
  // Coming back to a backgrounded tab must not show a stale board.
  document.addEventListener("visibilitychange", function(){
    if (!document.hidden) load();
  });
})();
</script>
</body>
</html>
"""


def _page(dealer_key: str = "") -> HTMLResponse:
    html = (BOARD_HTML
            .replace("__DEALER__", dealer_key or "")
            .replace("__API__", "/mykaarma"))
    return HTMLResponse(
        html,
        headers={
            # The board must never be served from cache: a salesperson looking
            # at a five-minute-old page walks up to a customer someone else has
            # already spoken to.
            "Cache-Control": "no-store, must-revalidate",
            "X-Robots-Tag": "noindex, nofollow",
        },
    )


@router.get("/equity-board", response_class=HTMLResponse, include_in_schema=False)
async def equity_board():
    return _page()


@router.get("/{dealer_key}/equity-board", response_class=HTMLResponse,
            include_in_schema=False)
async def equity_board_for_store(dealer_key: str):
    return _page(dealer_key)
