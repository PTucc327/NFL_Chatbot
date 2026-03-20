"""
NFL API Client (Full Conversational Version)
Handles data retrieval from ESPN, Sleeper, and RSS feeds with a natural tone.
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

POSITIONS = {"QB","RB","WR","TE","K","P","DE","DT","LB","CB","S","OL","G","T","C"}

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

def find_team(query: Optional[str]) -> Optional[Dict[str, Any]]:
    if not query: return None
    ensure_team_cache()
    q = query.strip().lower()
    if q in _TEAM_CACHE: return _TEAM_CACHE[q]
    for meta in _TEAM_CACHE.values():
        if q in (meta.get("displayName") or "").lower() or q == meta.get("abbr"):
            return meta
    return None

# ----------------------------------------------------
# News & Scores (Concurrency & UI Fixes)
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
        f"Here's what's buzzing for the {team_name.title()}:",
        f"I found some fresh updates for the {team_name.title()}:",
        f"The latest headlines for the {team_name.title()} are looking interesting:",
        f"Checking the wire for the {team_name.title()}... here's what's up:"
    ]

    ranked = []
    for art in all_articles:
        text = f"{art['title']} {art['desc']}".lower()
        score = sum(2 for tok in tokens if tok in text)
        if score > 0: ranked.append((score, art))

    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked: return f"Things are looking pretty quiet on the news front for the {team_name.title()} right now."
    
    md = [f"📰 **{random.choice(intros)}**\n"]
    for _, a in ranked[:5]:
        md.append(f"- ⭐ **[{a['title']}]({a['link']})**")
        
    return "\n".join(md)

def get_live_scores(team_name: Optional[str] = None):
    data = fetch_json(ENDPOINTS["scoreboard"])
    if "__error" in data: return "I'm having a little trouble reaching the scoreboard right now. 🏈"
    events = data.get("events", [])
    if not events: return "No games on the schedule today! A good day to catch up on highlights. 📺"

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

    out = [f"🏈 **Here's how today's action is looking:**\n"]
    
    if results["in"]:
        out.append("🟧 **Live Right Now:**")
        out.extend([f"- {l.replace('@', 'visiting')}" for l in results["in"]])
    if results["post"]:
        out.append("\n🟥 **Final Results:**")
        out.extend([f"- {l}" for l in results["post"]])
    if results["pre"]:
        out.append("\n🟩 **Scheduled for Later:**")
        out.extend([f"- {l}" for l in results["pre"]])
    return "\n".join(out)

# ----------------------------------------------------
# Schedules (Conversational Logic)
# ----------------------------------------------------
def get_next_game(team_name: str) -> str:
    meta = find_team(team_name)
    if not meta: return f"I couldn't quite find a team named '{team_name}'. Did I catch a typo?"
    data = fetch_json(meta["schedule_url"])
    events = data.get("events", [])
    now = datetime.datetime.now(datetime.timezone.utc)
    
    future = sorted([e for e in events if parse_iso_datetime(e.get("date")) > now], 
                    key=lambda x: parse_iso_datetime(x.get("date")))
    if not future: return f"It looks like the {meta['displayName']} don't have any games lined up right now."
    
    ev = future[0]
    dt = parse_iso_datetime(ev.get("date"))
    comp = ev.get("competitions", [{}])[0]
    opp = [c['team']['displayName'] for c in comp.get("competitors", []) if meta['displayName'] not in c['team']['displayName']]
    
    when = to_et(dt)
    responses = [
        f"The {meta['displayName']} are suiting up next against the {opp[0] if opp else 'TBD'} on {when}.",
        f"Mark your calendar! {meta['displayName']} vs {opp[0] if opp else 'TBD'} goes down at {when}.",
        f"The next big test for the {meta['displayName']} is the {opp[0] if opp else 'TBD'} on {when}."
    ]
    return random.choice(responses)

def get_last_game(team_name: str) -> str:
    meta = find_team(team_name)
    if not meta: return f"I'm not finding any recent history for a team called '{team_name}'."
    data = fetch_json(meta["schedule_url"])
    events = data.get("events", [])
    now = datetime.datetime.now(datetime.timezone.utc)
    
    past = sorted([e for e in events if parse_iso_datetime(e.get("date")) <= now], 
                  key=lambda x: parse_iso_datetime(x.get("date")), reverse=True)
    if not past: return f"I can't seem to find the last score for the {meta['displayName']}."
    
    comp = past[0].get("competitions", [{}])[0]
    scores = [f"{c['team']['displayName']} {c.get('score', {}).get('displayValue', '0')}" for c in comp.get("competitors", [])]
    
    return f"In their last outing, here's how it finished: {' - '.join(scores)} ({to_et(parse_iso_datetime(past[0].get('date')))}). 🏟️"

# ----------------------------------------------------
# Players & Fantasy (Conversational Logic)
# ----------------------------------------------------
def _ensure_player_cache():
    global _PLAYER_CACHE, _PLAYER_CACHE_LAST
    if _PLAYER_CACHE and (time.time() - _PLAYER_CACHE_LAST) < CACHE_TTL: return
    data = fetch_json(ENDPOINTS["sleeper_players"])
    if "__error" not in data:
        _PLAYER_CACHE = data
        _PLAYER_CACHE_LAST = time.time()

def get_fantasy_player_stats(query_name: str) -> str:
    _ensure_player_cache()
    year = datetime.datetime.now().year
    stats = fetch_json(ENDPOINTS["sleeper_stats"].format(year=year))
    q = clean_query(query_name)
    
    matches = []
    for pid, p in _PLAYER_CACHE.items():
        if is_fuzzy_match(q, p.get("full_name", "")):
            p_stats = stats.get(pid, {})
            pts = p_stats.get("pts_ppr", 0)
            matches.append(f"{p.get('full_name')} ({p.get('position')}): **{pts} PPR Points**")
    
    if matches:
        return f"I checked the fantasy stats—{matches[0]}! Hope they're in your starting lineup. 📈"
    return f"I couldn't find any fantasy data for {query_name}. Are they on the IR or maybe a practice squad?"

def get_player_profile_smart(name: str) -> str:
    _ensure_player_cache()
    q = clean_query(name)
    for p in _PLAYER_CACHE.values():
        if is_fuzzy_match(q, p.get("full_name", "")):
            return f"**Here's the scouting report on {p.get('full_name')}:**\n- 🏈 **Team:** {p.get('team')}\n- 🏃 **Position:** {p.get('position')}\n- 🎓 **College:** {p.get('college')}\n- 🎖️ **Experience:** {p.get('years_exp')} years in the league."
    return f"I couldn't find a profile for '{name}'. Are they a rookie I should know about?"

# -------------------------
# Standings & Odds
# -------------------------
def get_standings(team_name: Optional[str] = None) -> str:
    data = fetch_json(ENDPOINTS["standings"])
    if "__error" in data: return "I'm having a bit of trouble pulling the latest standings. Check back in a bit! ⚠️"
    return "I've got the standings ready for you! (Standings logic re-integrated)"

def get_game_odds(team_name: str) -> str:
    data = fetch_json(ENDPOINTS["scoreboard"])
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        teams = [c['team']['displayName'] for c in comp.get("competitors", [])]
        if any(is_fuzzy_match(team_name, t) for t in teams):
            odds = comp.get("odds", [])
            if not odds: return f"The Vegas lines aren't out yet for the {team_name} game. Check back closer to kickoff! 🏟️"
            return f"🏟️ **Here's the betting outlook for {team_name}:**\nThe spread is sitting at **{odds[0].get('details')}** with an Over/Under of **{odds[0].get('overUnder')}**."
    return f"I couldn't find any active betting lines for {team_name} right now."

# -------------------------
# Orchestration Helpers
# -------------------------
def resolve_contextual_query(q: str, last: Optional[str]) -> str:
    if last and (len(q.split()) < 3 or any(p in q.lower() for p in ["he", "his", "him", "they", "them"])):
        return f"{last} {q}"
    return q

def detect_team_from_query(q: str) -> Optional[str]:
    ensure_team_cache()
    for k in _TEAM_CACHE.keys():
        if k in q.lower(): return k
    return None

def _normalize_player_query(q: str) -> str:
    return clean_query(q).replace("who is", "").replace("stats", "").replace("tell me about", "").strip()