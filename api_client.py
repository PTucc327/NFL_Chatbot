"""
NFL API Client (Consolidated Version)
Handles data retrieval from ESPN, Sleeper, and RSS feeds with a natural, AI-driven tone.
"""

import datetime
import random
import re
import requests
import feedparser
import pandas as pd
import time
import logging
import concurrent.futures
from typing import Optional, Dict, Any, List, Tuple

# Professional logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from src.utils import (
    fetch_json,
    parse_iso_datetime,
    to_et,
    trend_indicator,
    clean_query,
    is_fuzzy_match
)

# -------------------------
# Configuration & Endpoints
# -------------------------
CACHE_TTL = 60 * 60 * 6 
REQUEST_TIMEOUT = 10

ENDPOINTS = {
    "scoreboard": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "news": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news",
    "teams": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams",
    "standings": "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
    "sleeper_players": "https://api.sleeper.app/v1/players/nfl",
    "sleeper_stats": "https://api.sleeper.app/v1/stats/nfl/regular/{year}"
}

# Mapping for nicknames to ensure robust entity recognition
NICKNAMES = {
    "pats": "patriots", "fins": "dolphins", "philly": "eagles", "g-men": "giants",
    "vikes": "vikings", "bolts": "chargers", "bucs": "buccaneers", "skins": "commanders",
    "jags": "jaguars", "cards": "cardinals", "pack": "packers", "birds": "eagles"
}

# -------------------------
# Local Caches
# -------------------------
_TEAM_CACHE: Dict[str, Dict[str, Any]] = {}
_TEAM_CACHE_LAST = 0
_PLAYER_CACHE: Dict[str, Dict[str, Any]] = {}
_PLAYER_CACHE_LAST = 0

# -------------------------
# Team Cache Management
# -------------------------
def ensure_team_cache():
    """Populate team metadata with robust error handling."""
    global _TEAM_CACHE, _TEAM_CACHE_LAST
    now = time.time()
    if _TEAM_CACHE and now - _TEAM_CACHE_LAST < CACHE_TTL:
        return
    
    data = fetch_json(ENDPOINTS["teams"])
    if "__error" in data:
        logger.error(f"Failed to refresh team cache: {data['__error']}")
        return

    try:
        leagues = data.get("sports", [])[0].get("leagues", [])
        teams = leagues[0].get("teams", []) if leagues else []
        
        new_cache = {}
        for item in teams:
            t = item.get("team", {})
            team_id = str(t.get("id"))
            meta = {
                "id": team_id,
                "displayName": t.get("displayName"),
                "abbr": t.get("abbreviation", "").lower(),
                "slug": t.get("slug", ""),
                "schedule_url": f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/schedule"
            }
            if meta["displayName"]: new_cache[meta["displayName"].lower()] = meta
            if meta["abbr"]: new_cache[meta["abbr"]] = meta
            new_cache[team_id] = meta
            
        _TEAM_CACHE = new_cache
        _TEAM_CACHE_LAST = now
    except Exception as e:
        logger.error(f"Parsing error in team cache: {e}")

def detect_team_from_query(query: str) -> Optional[str]:
    """
    Detects a team using exact name, abbreviation, or common nickname.
    Prioritizes longer matches to handle 'New York Giants' vs 'Giants' correctly.
    """
    ensure_team_cache()
    q = query.lower().strip()
    
    # Check nicknames first
    for nick, full in NICKNAMES.items():
        if re.search(rf"\b{nick}\b", q):
            return full

    # Check full cache sorted by length to prevent partial match collisions
    sorted_keys = sorted(_TEAM_CACHE.keys(), key=len, reverse=True)
    for k in sorted_keys:
        if re.search(rf"\b{re.escape(k)}\b", q):
            return _TEAM_CACHE[k]["displayName"]
    return None

def find_team(query: Optional[str]) -> Optional[Dict[str, Any]]:
    if not query: return None
    ensure_team_cache()
    q = query.strip().lower()
    
    # Check nicknames
    if q in NICKNAMES:
        q = NICKNAMES[q]
        
    if q in _TEAM_CACHE: return _TEAM_CACHE[q]
    for meta in _TEAM_CACHE.values():
        if q in (meta.get("displayName") or "").lower() or q == meta.get("abbr"):
            return meta
    return None

# ----------------------------------------------------
# News & Scores (Conversational & Dynamic)
# ----------------------------------------------------
def _fetch_rss_thread(url: str) -> List[Dict[str, str]]:
    try:
        feed = feedparser.parse(url)
        return [{"title": e.title, "link": e.link, "desc": e.get("summary", "")} for e in feed.entries]
    except Exception as e:
        logger.warning(f"RSS fetch failed for {url}: {e}")
        return []

def get_team_news(team_name: str) -> str:
    if not team_name: return "I'd love to find some news for you! Which team are we talking about? 🏈"
    
    sources = [
        f"https://news.google.com/rss/search?q={team_name.replace(' ', '+')}+NFL",
        "https://sports.yahoo.com/nfl/rss.xml",
        "https://profootballtalk.nbcsports.com/feed/"
    ]
    
    all_articles = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_rss_thread, url): url for url in sources}
        for future in concurrent.futures.as_completed(futures):
            all_articles.extend(future.result())

    tokens = [team_name.lower()] + team_name.lower().split()
    
    intros = [
        f"I did some digging, and here's what's buzzing for the {team_name.title()}:",
        f"I found some fresh updates that you might find interesting regarding the {team_name.title()}:",
        f"The latest headlines for the {team_name.title()} are looking pretty active right now:",
        f"Checking the wire for the {team_name.title()}... here's the word:"
    ]

    ranked = []
    for art in all_articles:
        text = f"{art['title']} {art['desc']}".lower()
        score = sum(2 for tok in tokens if tok in text)
        if score > 0: ranked.append((score, art))

    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked: 
        return f"Things are looking pretty quiet on the news front for the {team_name.title()} at the moment."
    
    md = [f"📰 **{random.choice(intros)}**\n"]
    for _, a in ranked[:5]:
        md.append(f"- ⭐ **[{a['title']}]({a['link']})**")
        
    return "\n".join(md)

def get_live_scores(team_name: Optional[str] = None):
    data = fetch_json(ENDPOINTS["scoreboard"])
    if "__error" in data: return "I'm having a little trouble reaching the live scoreboard right now. 🏈"
    
    events = data.get("events", [])
    if not events: return "There aren't any games on the schedule for today! 📺"

    team_q = clean_query(team_name) if team_name else None
    results = {"in": [], "post": [], "pre": []}

    for ev in events:
        comp = ev.get("competitions", [{}])[0]
        teams = comp.get("competitors", [])
        if len(teams) < 2: continue

        away = teams[1] if teams[1].get("homeAway") == "away" else teams[0]
        home = teams[0] if teams[0].get("homeAway") == "home" else teams[1]
        
        aw_name, aw_score = away['team']['displayName'], away['score']
        hm_name, hm_score = home['team']['displayName'], home['score']
        
        dt = parse_iso_datetime(ev.get("date"))
        state = comp.get("status", {}).get("type", {}).get("state", "pre")
        detail = comp.get("status", {}).get("type", {}).get("shortDetail", "")

        line = f"{aw_name} {aw_score} @ {hm_name} {hm_score} ({to_et(dt)}, {detail})"
        
        if team_q and team_q not in (aw_name + hm_name).lower(): continue
        results[state].append(line)

    out = [f"🏈 **Here's the current situation on the field:**\n"]
    if results["in"]:
        out.append("🟧 **Live Action:**")
        for l in results["in"]:
            phrase = random.choice(["visiting", "taking on", "battling"])
            out.append(f"- {l.replace('@', phrase)}")
    if results["post"]:
        out.append("\n🟥 **Final Results:**")
        out.extend([f"- {l}" for l in results["post"]])
    if results["pre"]:
        out.append("\n🟩 **Scheduled for Later:**")
        out.extend([f"- {l}" for l in results["pre"]])
    return "\n".join(out)

# ... [Keep get_next_game, get_last_game, get_player_profile_smart, and get_fantasy_player_stats as previously perfected] ...

# -------------------------
# Orchestration Helpers
# -------------------------
def resolve_contextual_query(user_input: str, last_subject: Optional[str]) -> str:
    ui = user_input.lower().strip()
    vague_intents = ["stats", "fantasy", "news", "record", "next game", "last game", "how did they do"]
    has_pronoun = any(p in ui for p in ["he ", "him ", "his ", "them ", "they "])
    is_vague = any(intent == ui for intent in vague_intents)

    if (is_vague or has_pronoun) and last_subject:
        return f"{last_subject} {ui}"
    return user_input

def _normalize_player_query(q: str) -> str:
    return clean_query(q).replace("who is", "").replace("stats", "").replace("tell me about", "").strip()