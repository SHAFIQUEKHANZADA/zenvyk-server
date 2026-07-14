"""
Dealer configuration.

V1: one dealer, read from environment variables.
LATER: move DEALERS into Supabase and look up by dealer_key, so Reid can add
       all 19+ McGrath stores without a redeploy.
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


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {token}"


class DealerNotConfigured(Exception):
    pass


def get_dealer(dealer_key: str | None = None) -> Dict[str, str]:
    """
    Return the credentials + UUIDs for a dealership.

    Swap the body of this function for a Supabase query when you go multi-store:
        SELECT dealer_uuid, department_uuid, username, password
        FROM mykaarma_dealers WHERE dealer_key = :dealer_key
    """
    key = dealer_key or DEFAULT_DEALER_KEY

    username = os.getenv("MYKAARMA_USERNAME")
    password = os.getenv("MYKAARMA_PASSWORD")
    dealer_uuid = os.getenv("MYKAARMA_DEALER_UUID")
    department_uuid = os.getenv("MYKAARMA_DEPARTMENT_UUID")

    missing = [
        name
        for name, value in [
            ("MYKAARMA_USERNAME", username),
            ("MYKAARMA_PASSWORD", password),
            ("MYKAARMA_DEALER_UUID", dealer_uuid),
            ("MYKAARMA_DEPARTMENT_UUID", department_uuid),
        ]
        if not value
    ]
    if missing:
        raise DealerNotConfigured(
            f"Missing environment variables: {', '.join(missing)}"
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
