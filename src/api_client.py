"""
NFL API Client (Consolidated Conversational Version)
Handles all data retrieval from ESPN, Sleeper, and RSS feeds.
This file acts as a Pure Data Provider to be orchestrated by the chatbot router.
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
    """Helper to resolve a query string to a team metadata object."""
    if not query: return None
    ensure_team_cache()
    q = query.strip().lower()
    
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
    """Internal helper for concurrent RSS fetching."""
    try:
        feed = feedparser.parse(url)
        return [{"title": e.title, "link": e.link, "desc": e.get("summary", "")} for e in feed.entries]
    except Exception as e:
        logger.warning(f"RSS fetch failed for {url}: {e}")
        return []


def get_team_news(team_name: str) -> str:
    """Fetches and ranks multi-source NFL news with a narrative tone."""
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
    """Fetches live NFL scores with conversational phrasing."""
    data = fetch_json(ENDPOINTS["scoreboard"])
    if "__error" in data: return "I'm having a little trouble reaching the live scoreboard right now. 🏈"
    
    events = data.get("events", [])
    if not events: return "There aren't any games on the schedule for today! It's a perfect time to catch up on some highlights. 📺"

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
        out.append("\n\n🟥 **Final Results:**")
        out.extend([f"- {l}" for l in results["post"]])
    if results["pre"]:
        out.append("\n\n🟩 **Scheduled for Later:**")
        out.extend([f"- {l}" for l in results["pre"]])
    return "\n".join(out)

# ----------------------------------------------------
# Standings (Narrative & Multi-mode)
# ----------------------------------------------------

def get_standings(team_query: Optional[str] = None) -> str:
    """Parses and returns record-based standings."""
    data = fetch_json(ENDPOINTS["standings"])
    if "__error" in data:
        return "I'm having a bit of trouble pulling the latest standings. Check back in a bit! ⚠️"

    team_meta = find_team(team_query) if team_query else None
    standings_groups = data.get("children", [])
    output = ["📊 **NFL Standings Update:**\n"]
    found_team_info = None

    for conference in standings_groups:
        for division in conference.get("children", []):
            div_name = division.get("name", "")
            div_lines = [f"**{div_name}**"]
            for entry in division.get("standings", {}).get("entries", []):
                t_name = entry.get("team", {}).get("displayName")
                stats = {s['name']: s['displayValue'] for s in entry.get("stats", [])}
                record = stats.get("wins", "0") + "-" + stats.get("losses", "0")
                if stats.get("ties") != "0": record += f"-{stats.get('ties')}"
                
                line = f"- {t_name}: **{record}**"
                div_lines.append(line)

                if team_meta and team_meta["displayName"].lower() in t_name.lower():
                    found_team_info = (div_name, div_lines)

            if not team_query:
                output.extend(div_lines)
                output.append("")

    if team_query:
        if found_team_info:
            div_name, lines = found_team_info
            return f"The {team_meta['displayName']} are currently battling in the {div_name}:\n" + "\n".join(lines)
        return f"I couldn't find the standings for the '{team_query}'."

    return "\n".join(output)

# ----------------------------------------------------
# Schedules & Players (Conversational & Narrative)
# ----------------------------------------------------

def get_next_game(team_name: str) -> str:
    """Finds the nearest upcoming game for a given team."""
    meta = find_team(team_name)
    if not meta: return f"I couldn't quite find a team named '{team_name}'."
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
    """Finds the most recently completed game for a team."""
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


def _ensure_player_cache():
    global _PLAYER_CACHE, _PLAYER_CACHE_LAST
    if _PLAYER_CACHE and (time.time() - _PLAYER_CACHE_LAST) < CACHE_TTL: return
    data = fetch_json(ENDPOINTS["sleeper_players"])
    if "__error" not in data:
        _PLAYER_CACHE = data
        _PLAYER_CACHE_LAST = time.time()


def get_player_profile_smart(user_input: str) -> Any:
    _ensure_player_cache()
    q = user_input.lower().strip()

    # ---------------------------------------------------------
    # LAYER 1: Retired Legends (History & Awards)
    # ---------------------------------------------------------
    LEGENDS = {
        "tom brady": {
            "name": "Tom Brady", "pos": "QB", "status": "Retired (HOF 2028)",
            "teams": "Patriots, Buccaneers",
            "stats": "89,214 Yds, 649 TDs (NFL Records)",
            "awards": "7x SB Champ, 3x MVP, 5x SB MVP, 15x Pro Bowl"
        },
        "eli manning": {
            "name": "Eli Manning", "pos": "QB", "status": "Retired",
            "teams": "NY Giants (2004-2019)",
            "stats": "57,023 Yds, 366 TDs",
            "awards": "2x SB MVP, 4x Pro Bowl, Walter Payton Man of the Year"
        }
    }

    if q in LEGENDS:
        l = LEGENDS[q]
        return (f"### 🏛️ Legend: {l['name']}\n"
                f"- **Status:** {l['status']}\n"
                f"- **Teams:** {l['teams']}\n"
                f"- **Career Stats:** {l['stats']}\n"
                f"- **Awards:** {l['awards']}")

    # ---------------------------------------------------------
    # LAYER 2: College Prospects (Stats & Draft)
    # ---------------------------------------------------------
    PROSPECTS = {
        "arch manning": {
            "name": "Arch Manning", "school": "Texas", "pos": "QB",
            "stats": "2025: 3,163 Yds, 26 TD, 10 Rush TD",
            "outlook": "Top 2026/2027 NFL Draft Prospect"
        },
        "travis hunter": {
            "name": "Travis Hunter", "school": "Colorado", "pos": "WR/CB",
            "stats": "92 Rec, 1,152 Yds, 14 TD | 4 INT, 31 Tackles",
            "awards": "2024 Heisman Winner, Paul Hornung Award"
        }
    }

    if q in PROSPECTS:
        p = PROSPECTS[q]
        return (f"### 🎓 Prospect: {p['name']}\n"
                f"- **School:** {p['school']} | **Pos:** {p['pos']}\n"
                f"- **2024/25 Stats:** {p['stats']}\n"
                f"- **Draft/Awards:** {p['awards'] if 'awards' in p else p['outlook']}")

    # ---------------------------------------------------------
    # LAYER 3: Active Players (Sleeper Data + Live Stats)
    # ---------------------------------------------------------
    matches = []
    for pid, p in _PLAYER_CACHE.items():
        if is_fuzzy_match(q, p.get("full_name", "")):
            matches.append(p)

    if len(matches) == 1:
        p = matches[0]
        # Link your existing fantasy stats method here
        live_stats = get_fantasy_player_stats(p['full_name']) 
        return (f"### 🏈 Active: {p['full_name']}\n"
                f"- **Team:** {p.get('team', 'FA')} | **Exp:** {p.get('years_exp')} yrs\n"
                f"- **Current Stats:** {live_stats}")

    return f"I couldn't find a record for '{q.title()}'. They might be a deep-history legend!"

def get_fantasy_player_stats(query_name: str) -> str:
    """Retrieves PPR fantasy points for a player."""
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
    
    if matches: return f"I took a look at the latest fantasy data—{matches[0]}!"
    return f"I'm not seeing any fantasy points recorded for {query_name} yet."


def get_game_odds(team_name: str) -> str:
    """Retrieves Vegas betting lines for a specific team."""
    data = fetch_json(ENDPOINTS["scoreboard"])
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        teams = [c['team']['displayName'] for c in comp.get("competitors", [])]
        if any(is_fuzzy_match(team_name, t) for t in teams):
            odds = comp.get("odds", [])
            if not odds: return f"The Vegas lines aren't out yet for the {team_name} game."
            return f"🏟️ **Here's the betting outlook for {team_name}:**\nThe spread is sitting at **{odds[0].get('details')}** with an Over/Under of **{odds[0].get('overUnder')}**."
    return f"I couldn't find any active betting lines for {team_name} right now."

# -------------------------
# Orchestration Helpers
# -------------------------

def resolve_contextual_query(user_input: str, last_subject: Optional[str]) -> str:
    """Maps follow-up pronouns back to the previously mentioned subject."""
    ui = user_input.lower().strip()
    vague_intents = ["stats", "fantasy", "news", "record", "next game", "last game", "how did they do"]
    has_pronoun = any(p in ui for p in ["he ", "him ", "his ", "them ", "they "])
    is_vague = any(intent == ui for intent in vague_intents)

    if (is_vague or has_pronoun) and last_subject:
        return f"{last_subject} {ui}"
    return user_input



def _normalize_player_query(q: str) -> str:
    """Strips conversational fluff while preserving names (e.g., 'Tom')."""
    clean = clean_query(q)
    # Using \b ensures we don't strip the 'T' from 'Tom'
    fillers = [r"\bwho is\b", r"\btell me about\b", r"\bprofile\b", r"\bscouting\b"]
    for pattern in fillers:
        clean = re.sub(pattern, "", clean, flags=re.IGNORECASE)
    return clean.strip()