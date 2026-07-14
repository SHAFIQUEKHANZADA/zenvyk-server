"""
Zenvyk × myKaarma Connector
===========================
Sits between the GoHighLevel Voice AI agent and the myKaarma Scheduler API.

The GHL voice agent ("Esther") calls three endpoints during a live phone call:
    POST /mykaarma/lookup-customer    -> who is calling + what they drive
    POST /mykaarma/get-slots          -> REAL open appointment times
    POST /mykaarma/book-appointment   -> creates the appointment in myKaarma

Run locally:
    pip install -r requirements.txt
    cp .env.example .env      # fill in the myKaarma credentials
    uvicorn main:app --reload --port 8000

Deploy (Railway):
    Start command:  uvicorn main:app --host 0.0.0.0 --port $PORT

NOTE: myKaarma geo-blocks non-US IPs (403 Forbidden). Run this on a US-hosted
      server (Railway). Testing from a non-US laptop will fail.
"""

import logging
import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routes import router as mykaarma_router

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
log = logging.getLogger("mykaarma")

app = FastAPI(
    title="Zenvyk × myKaarma Connector",
    description="Bridges the GoHighLevel Voice AI agent to the myKaarma Scheduler API.",
    version="1.0.0",
)

# GHL calls us server-to-server, so CORS is permissive by default.
# Lock this down if you ever call it from a browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("ALLOWED_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(mykaarma_router)


@app.get("/")
def root():
    return {
        "service": "Zenvyk × myKaarma Connector",
        "status": "ok",
        "endpoints": [
            "POST /mykaarma/lookup-customer",
            "POST /mykaarma/get-slots",
            "POST /mykaarma/book-appointment",
            "POST /mykaarma/refresh-opcodes",
        ],
        "docs": "/docs",
    }


@app.get("/health")
def health():
    """Also reports whether the myKaarma credentials are actually configured."""
    required = [
        "MYKAARMA_USERNAME",
        "MYKAARMA_PASSWORD",
        "MYKAARMA_DEALER_UUID",
        "MYKAARMA_DEPARTMENT_UUID",
    ]
    missing = [k for k in required if not os.getenv(k)]
    return {
        "status": "ok" if not missing else "misconfigured",
        "mykaarma_configured": not missing,
        "missing_env": missing,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=True,
    )
