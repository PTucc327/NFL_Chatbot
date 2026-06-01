"""
Utility Helpers (Perfected Version)
Core functions for Networking, Fuzzy Matching, and Time Conversion.
"""
import time
import datetime
import re
import requests
import logging
from typing import Optional, Dict, Any
from rapidfuzz import fuzz

# Set up logging for the engine room
logger = logging.getLogger(__name__)

# Constants
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3

# -------------------------------------------------------------------
# Professional Fuzzy Matching
# -------------------------------------------------------------------

def is_fuzzy_match(target: str, candidate: str, threshold: int = 85) -> bool:
    """
    Uses token_set_ratio for high-accuracy matching.
    Handles typos, word order, and middle initials common in NFL names.
    Requires at least 2 tokens in the query to prevent a bare first name
    (e.g., 'josh') from matching every player with that first name.
    """
    if not target or not candidate:
        return False
        
    t_low = target.lower().strip()
    c_low = candidate.lower().strip()
    
    # 1. Quick bypass for exact matches
    if t_low == c_low:
        return True

    # 2. Single-token guard: a bare first name like "josh" would score 100
    #    against every "Josh X" via token_set_ratio. Require at least 2 tokens
    #    so the caller is forced to provide enough context for disambiguation.
    if len(t_low.split()) < 2:
        return False
        
    # 3. Token Set Ratio is best for "Josh Allen" vs "Josh R. Allen"
    score = fuzz.token_set_ratio(t_low, c_low)
    return score >= threshold

# -------------------------------------------------------------------
# Resilient Networking (With Backoff)
# -------------------------------------------------------------------

def fetch_json(url: str, params: dict = None, headers: dict = None) -> Dict[str, Any]:
    """
    Fetches JSON with exponential backoff retries.
    Prevents the bot from crashing during minor API hiccups.
    """
    attempt = 0
    backoff = 1.0  # Start with 1 second wait
    
    while attempt < MAX_RETRIES:
        try:
            response = requests.get(
                url, 
                params=params, 
                headers=headers, 
                timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            return response.json()
            
        except requests.exceptions.RequestException as e:
            attempt += 1
            if attempt == MAX_RETRIES:
                logger.error(f"Final fetch failure for {url}: {e}")
                return {"__error": str(e)}
            
            logger.warning(f"Fetch attempt {attempt} failed for {url}. Retrying in {backoff}s...")
            time.sleep(backoff)
            backoff *= 2  # Exponentially increase wait time
            
    return {"__error": "Unknown network error"}

# -------------------------------------------------------------------
# Time & Formatting Helpers
# -------------------------------------------------------------------

def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime.datetime]:
    """Robust ISO parser handling multiple NFL API formats."""
    if not dt_str:
        return None
    try:
        # Standard ISO format with Z
        if dt_str.endswith("Z"):
            dt_str = dt_str[:-1] + "+00:00"
        return datetime.datetime.fromisoformat(dt_str)
    except Exception:
        # Fallback for older strptime formats
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
            try:
                return datetime.datetime.strptime(dt_str, fmt).replace(tzinfo=datetime.timezone.utc)
            except Exception:
                continue
    return None

def to_et(dt: Optional[datetime.datetime]) -> str:
    """Converts UTC to Eastern Time with robust timezone handling."""
    if not dt:
        return "TBD"
    
    # Ensure UTC awareness
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
        
    try:
        # Attempt to use modern ZoneInfo (Python 3.9+)
        from zoneinfo import ZoneInfo
        et_tz = ZoneInfo("America/New_York")
    except ImportError:
        # Fallback for older environments
        from datetime import timezone, timedelta
        et_tz = timezone(timedelta(hours=-5)) # Approximation of ET

    et_dt = dt.astimezone(et_tz)
    return et_dt.strftime("%I:%M %p ET")

# -------------------------------------------------------------------
# Data Cleansing
# -------------------------------------------------------------------

def clean_query(text: str) -> str:
    """Standardizes user input for entity matching."""
    if not text:
        return ""
    # Lowercase, remove special chars, and strip extra whitespace
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())

def trend_indicator(pct: float) -> str:
    """Visual feedback for team performance."""
    if pct >= 0.700: return "🔥" # Upgraded to more visual emojis
    if pct <= 0.350: return "🧊"
    return "•"