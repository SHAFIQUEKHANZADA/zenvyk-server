"""
Dealer configuration.

Multi-store: one shared myKaarma username/password (in the environment) works for
ALL McGrath dealers. Each store is routed by `dealer_key` to its own dealer_uuid +
service department_uuid. The GHL agent for each store must send its own dealer_key.

UUIDs pulled 2026-08-19 from the myKaarma configurations endpoint
(GET /developer/application/configurations). These are the SERVICE departments —
Esther is a service appointment coordinator.

LATER: move DEALERS into Supabase and look up by dealer_key, so new McGrath stores
       can be added without a redeploy.
"""

import os
import base64
from typing import Dict

from dotenv import load_dotenv

load_dotenv()

MYKAARMA_BASE_URL = os.getenv("MYKAARMA_BASE_URL", "https://api.mykaarma.com")

# How many appointment times we hand back to the voice agent.
# Keep it small — the agent has to read them out loud on a phone call.
MAX_SLOTS = int(os.getenv("MAX_SLOTS", "3"))

# Default appointment length when the opcode doesn't give us a duration.
DEFAULT_APPOINTMENT_MINUTES = int(os.getenv("DEFAULT_APPOINTMENT_MINUTES", "60"))

DEFAULT_DEALER_KEY = os.getenv("DEFAULT_DEALER_KEY", "mcgrath_honda_stcharles")


# ─────────────────────────────────────────────────────────────────────────────
# The five live McGrath stores (+ sandbox). Same credentials for all — only the
# dealer_uuid / department_uuid differ. Each GHL agent sends its own dealer_key.
# ─────────────────────────────────────────────────────────────────────────────
DEALERS: Dict[str, Dict[str, str]] = {
    "mcgrath_honda_stcharles": {
        "name": "McGrath Honda of St. Charles",
        "dealer_uuid": "be7d471ce70f649d3ce8590c392e2a77f03d62b99b75c4e89eb4d6c5c51cc760",
        "department_uuid": "c17b4e9ac52fe91744616552b8270a87913563c016dc93bda04fb3005077cfa1",
    },
    "mcgrath_kia_stcharles": {
        "name": "McGrath KIA of St. Charles",
        "dealer_uuid": "f4833260988c91faf2ea57c68f2911983a309774bff2cddd77145b69bb330338",
        "department_uuid": "9084c224001d1ce09b1a809ab8758032c8c1942e931f36801286754cdd8a21a0",
    },
    "mcgrath_honda_elgin": {
        "name": "McGrath Honda of Elgin",
        "dealer_uuid": "de2ab4e928f5277f79c101444bc162c8d187d2f47830e7f6ef19e9214168405e",
        "department_uuid": "68b9221ee2adf695ecc4bb7a7eb6987c32dea3293179e39a53b327115430fd22",
    },
    "mcgrath_acura_mortongrove": {
        "name": "McGrath Acura Morton Grove",
        "dealer_uuid": "1e42452d45bbd4b828f9a188c7a6b4d66841a404fc7c3b290311b54068127ea1",
        "department_uuid": "10dd244d8faa2792aca0514db500ea6314f51e0fcb64cdc657974806570e03ce",
    },
    "mcgrath_acura_libertyville": {
        "name": "McGrath Acura of Libertyville",
        "dealer_uuid": "83abfacbb3e6236122ff808b4a27852bc6968fdb14242c0b796b196d88974cc5",
        "department_uuid": "af7feeebf612aed0c88bbdaadb9e645b6636841f8d171de726e6b99631dbfdc4",
    },
    "sandbox": {
        "name": "McGrath Motors Sandbox",
        "dealer_uuid": "b357c9534eaa04d6ec31fa7c6259863af85dbc96ad97ffd19572adfbd528b12e",
        "department_uuid": "52bf8131020858c865a522962136b6415d2bf3b371c65b61be96c22233d0f9d7",
    },
}


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


class DealerNotConfigured(Exception):
    pass


def get_dealer(dealer_key: str | None = None) -> Dict[str, str]:
    """
    Return the credentials + UUIDs for a dealership.

    Credentials (username/password) are SHARED across all stores and come from the
    environment. The dealer_uuid + department_uuid are looked up per store from
    DEALERS by dealer_key. An unknown key falls back to the MYKAARMA_DEALER_UUID /
    MYKAARMA_DEPARTMENT_UUID env vars (single-dealer / ad-hoc use).
    """
    key = dealer_key or DEFAULT_DEALER_KEY

    username = os.getenv("MYKAARMA_USERNAME")
    password = os.getenv("MYKAARMA_PASSWORD")

    dealer = DEALERS.get(key)
    if dealer:
        dealer_uuid = dealer["dealer_uuid"]
        department_uuid = dealer["department_uuid"]
    else:
        # Unknown dealer_key — fall back to env-var single dealer (backward compatible).
        dealer_uuid = os.getenv("MYKAARMA_DEALER_UUID")
        department_uuid = os.getenv("MYKAARMA_DEPARTMENT_UUID")

    missing = [
        name
        for name, value in [
            ("MYKAARMA_USERNAME", username),
            ("MYKAARMA_PASSWORD", password),
            ("dealer_uuid", dealer_uuid),
            ("department_uuid", department_uuid),
        ]
        if not value
    ]
    if missing:
        raise DealerNotConfigured(
            f"Dealer '{key}' is not configured (missing: {', '.join(missing)}). "
            f"Known dealer_keys: {', '.join(DEALERS)}"
        )

    return {
        "dealer_key": key,
        "dealer_uuid": dealer_uuid,
        "department_uuid": department_uuid,
        "auth_header": _basic_auth(username, password),
    }


def headers(dealer: Dict[str, str]) -> Dict[str, str]:
    return {
        "Authorization": dealer["auth_header"],
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
