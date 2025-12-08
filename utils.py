"""
Utility helpers for NFL Chatbot
Used by api_client.py
"""

import time
import datetime
import re
import requests
from typing import Optional, Dict, Any
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

REQUEST_TIMEOUT = 10
CACHE_TTL = 60 * 60 * 6   # 6 hours


# -------------------------------------------------------------------
# Safe JSON Fetch
# -------------------------------------------------------------------
import traceback

# -------------------------
# Network helpers
# -------------------------
def safe_fetch_json(url: str, params: dict = None, headers: dict = None, retries: int = 3, timeout: int = REQUEST_TIMEOUT):
    """Fetch JSON with simple retry/backoff; returns dict with '__error' on failure."""
    attempt = 0
    backoff = 1.0
    while attempt < retries:
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            attempt += 1
            time.sleep(backoff)
            backoff *= 2
            last_err = e
    return {"__error": f"Failed to fetch {url}: {last_err}"}


def fetch_json(url: str, params: dict = None, headers: dict = None) -> Dict[str, Any]:
    return safe_fetch_json(url, params=params, headers=headers)


# -------------------------
# Time helpers
# -------------------------
def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime.datetime]:
    if not dt_str:
        return None
    try:
        # handle trailing Z
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(dt_str)
    except Exception:
        # fallback common formats
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.datetime.strptime(dt_str, fmt).replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
    return None


def to_et(dt: Optional[datetime.datetime]) -> str:
    if not dt:
        return "TBD"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    if ZoneInfo:
        try:
            et = dt.astimezone(ZoneInfo("America/New_York"))
            return et.strftime("%I:%M %p %Z")
        except Exception:
            return dt.isoformat()
    return dt.isoformat()


# -------------------------------------------------------------------
# Generic helpers
# -------------------------------------------------------------------
def trend_indicator(pct):
    if pct >= 0.700:
        return "↑"
    if pct <= 0.350:
        return "↓"
    return "•"


def clean_query(text: str):
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text
