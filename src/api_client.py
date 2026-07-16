"""
NFL API Client (Consolidated Conversational Version)
Handles all data retrieval from ESPN, Sleeper, and RSS feeds.
This file acts as a Pure Data Provider to be orchestrated by the chatbot router.
"""

import datetime
import json
import os
import random
import re
import requests
import feedparser
import time
import logging
import concurrent.futures
from typing import Optional, Dict, Any, List, Union
from dotenv import load_dotenv

load_dotenv()

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
    "scoreboard":     "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "teams":          "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams",
    "standings":      "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
    "sleeper_players":    "https://api.sleeper.app/v1/players/nfl",
    "sleeper_stats":      "https://api.sleeper.app/v1/stats/nfl/regular/{year}",
    "sleeper_stats_week": "https://api.sleeper.app/v1/stats/nfl/regular/{year}/{week}",
}

def _current_nfl_season_year() -> int:
    """
    Returns the correct Sleeper stats year to query.
    The NFL season runs Sep–Feb, so Jan–Aug of a calendar year still belongs
    to the previous season (e.g., May 2026 -> 2025 season stats).
    """
    now = datetime.datetime.now()
    # NFL season data is available from September onward
    return now.year if now.month >= 9 else now.year - 1

# Mapping for nicknames to ensure robust entity recognition
NICKNAMES = {
    "pats": "patriots", "fins": "dolphins", "philly": "eagles", "g-men": "giants",
    "vikes": "vikings", "bolts": "chargers", "bucs": "buccaneers", "skins": "commanders",
    "jags": "jaguars", "cards": "cardinals", "pack": "packers", "birds": "eagles"
}

POSITIONS = {"QB","RB","WR","TE","K","P","DE","DT","LB","CB","S","OL","G","T","C"}

# -------------------------
# Static Data Loaders
# -------------------------

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")

def _load_static_data(filename: str) -> List[Dict[str, Any]]:
    """Loads a JSON data file from the project's data/ directory."""
    path = os.path.join(_DATA_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error(f"Static data file not found: {path}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse {filename}: {e}")
        return []

def _build_lookup(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Builds a lowercase name-keyed lookup dict from a list of records."""
    return {r["name"].lower(): r for r in records}

# Load once at module import time; reload by calling these again if needed
_LEGENDS: Dict[str, Dict[str, Any]] = _build_lookup(_load_static_data("legends.json"))
_PROSPECTS: Dict[str, Dict[str, Any]] = _build_lookup(_load_static_data("prospects.json"))

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
    """Fetches live NFL scores with home/away context and venue."""
    data = fetch_json(ENDPOINTS["scoreboard"])
    if "__error" in data: return "I'm having a little trouble reaching the live scoreboard right now. 🏈"
    
    events = data.get("events", [])
    if not events: return "There aren't any games on the schedule right now. It's a perfect time to catch up on some highlights. 📺"

    team_q = clean_query(team_name) if team_name else None
    results = {"in": [], "post": [], "pre": []}

    for ev in events:
        comp  = ev.get("competitions", [{}])[0]
        teams = comp.get("competitors", [])
        if len(teams) < 2: continue

        # Identify home and away reliably
        away = next((t for t in teams if t.get("homeAway") == "away"), teams[1])
        home = next((t for t in teams if t.get("homeAway") == "home"), teams[0])

        aw_name  = away["team"]["displayName"]
        hm_name  = home["team"]["displayName"]
        aw_score = away.get("score", "0")
        hm_score = home.get("score", "0")

        # Venue — ESPN returns it on the venue sub-object
        venue = comp.get("venue", {}).get("fullName", "")
        venue_str = f" @ {venue}" if venue else ""

        dt     = parse_iso_datetime(ev.get("date"))
        state  = comp.get("status", {}).get("type", {}).get("state", "pre")
        detail = comp.get("status", {}).get("type", {}).get("shortDetail", "")

        line = f"{aw_name} **{aw_score}** @ {hm_name} **{hm_score}**{venue_str} ({to_et(dt)}, {detail})"

        if team_q and team_q not in (aw_name + hm_name).lower(): continue
        results[state].append(line)

    out = ["🏈 **NFL Scoreboard**\n"]
    if results["in"]:
        out.append("🟧 **Live Right Now:**")
        out.extend([f"- {l}" for l in results["in"]])
    if results["post"]:
        out.append("\n🟥 **Final:**")
        out.extend([f"- {l}" for l in results["post"]])
    if results["pre"]:
        out.append("\n🟩 **Coming Up:**")
        out.extend([f"- {l}" for l in results["pre"]])

    if not any(results.values()):
        msg = f"No games found for **{team_name}** right now." if team_q else "No games found."
        out.append(msg)

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
    # ESPN standings API returns conferences directly under 'children';
    # each conference has its own 'standings.entries' (no division sub-children)
    conferences = data.get("children", [])
    output = ["📊 **NFL Standings Update:**\n"]
    found_team_info = None

    for conference in conferences:
        conf_name = conference.get("name", "")
        entries = conference.get("standings", {}).get("entries", [])
        if not entries:
            continue

        conf_lines = [f"**{conf_name}**"]
        for entry in entries:
            t_name = entry.get("team", {}).get("displayName", "Unknown")
            stats = {s["name"]: s["displayValue"] for s in entry.get("stats", [])}
            wins   = stats.get("wins", "0")
            losses = stats.get("losses", "0")
            ties   = stats.get("ties", "0")
            record = f"{wins}-{losses}" + (f"-{ties}" if ties != "0" else "")

            line = f"- {t_name}: **{record}**"
            conf_lines.append(line)

            if team_meta and team_meta["displayName"].lower() in t_name.lower():
                found_team_info = (conf_name, conf_lines[:])

        if not team_query:
            output.extend(conf_lines)
            output.append("")

    if team_query:
        if found_team_info:
            conf_name, lines = found_team_info
            return f"The {team_meta['displayName']} are currently in the {conf_name}:\n" + "\n".join(lines)
        return f"I couldn't find the standings for '{team_query}'."

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

    # Guard: skip events where date fails to parse (returns None)
    future = sorted(
        [e for e in events if parse_iso_datetime(e.get("date")) is not None
         and parse_iso_datetime(e.get("date")) > now],
        key=lambda x: parse_iso_datetime(x.get("date"))
    )
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

    # Guard: skip events where date fails to parse (returns None)
    past = sorted(
        [e for e in events if parse_iso_datetime(e.get("date")) is not None
         and parse_iso_datetime(e.get("date")) <= now],
        key=lambda x: parse_iso_datetime(x.get("date")),
        reverse=True
    )
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


def get_player_profile_smart(user_input: str) -> Union[str, Dict[str, Any]]:
    _ensure_player_cache()
    q = user_input.lower().strip()

    # ---------------------------------------------------------
    # LAYER 1: Retired Legends (History & Awards)
    # Loaded from data/legends.json — add entries there to expand coverage
    # ---------------------------------------------------------
    if q in _LEGENDS:
        l = _LEGENDS[q]
        return (f"### 🏛️ Legend: {l['name']}\n"
                f"- **Status:** {l['status']}\n"
                f"- **Teams:** {l['teams']}\n"
                f"- **Career Stats:** {l['stats']}\n"
                f"- **Awards:** {l['awards']}")

    # ---------------------------------------------------------
    # LAYER 2: College Prospects (Stats & Draft)
    # Loaded from data/prospects.json — add entries there to expand coverage
    # ---------------------------------------------------------
    if q in _PROSPECTS:
        p = _PROSPECTS[q]
        return (f"### 🎓 Prospect: {p['name']}\n"
                f"- **School:** {p['school']} | **Pos:** {p['pos']}\n"
                f"- **2024/25 Stats:** {p['stats']}\n"
                f"- **Draft/Awards:** {p.get('awards', p.get('outlook', 'N/A'))}")

    # ---------------------------------------------------------
    # LAYER 3: Active Players (Sleeper Data + Live Stats)
    # ---------------------------------------------------------
    matches = []
    for pid, p in _PLAYER_CACHE.items():
        if is_fuzzy_match(q, p.get("full_name", "")):
            matches.append(p)

    if not matches:
        return f"I couldn't find a record for '{q.title()}'. They might be a deep-history legend!"

    # Prefer active players — filters out retired/inactive duplicates (e.g. the
    # inactive G named Josh Allen when the user means the Bills QB)
    active_matches = [p for p in matches if p.get("active")]
    if active_matches:
        matches = active_matches

    # If a team hint is present in the original query, narrow further
    team_hint = detect_team_from_query(q)
    if team_hint:
        hinted = [p for p in matches
                  if team_hint.lower() in (p.get("team") or "").lower()
                  or (p.get("team") or "").upper() in team_hint.upper()]
        if hinted:
            matches = hinted

    if len(matches) == 1:
        p = matches[0]
        live_stats = get_fantasy_player_stats(p["full_name"])
        # Surface injury status inline on the profile
        injury_status = p.get("injury_status") or "Healthy"
        injury_part   = p.get("injury_body_part", "")
        injury_line   = f"{injury_status}" + (f" ({injury_part})" if injury_part else "")
        # Depth chart position (#2 — depth chart improvement)
        depth_pos   = p.get("depth_chart_position", "")
        depth_order = p.get("depth_chart_order")
        depth_line  = ""
        if depth_pos and depth_order is not None:
            ordinal = {1: "Starter", 2: "2nd string", 3: "3rd string"}.get(
                int(depth_order), f"#{depth_order}"
            )
            depth_line = f"\n- **Depth Chart:** {ordinal} {depth_pos}"
        return (f"### 🏈 Active: {p['full_name']}\n"
                f"- **Team:** {p.get('team', 'FA')} | **Pos:** {p.get('position', 'N/A')} "
                f"| **Exp:** {p.get('years_exp', '?')} yrs\n"
                f"- **Injury:** {injury_line}"
                f"{depth_line}\n"
                f"- **Season Stats:** {live_stats}")

    # Multiple matches — return disambiguation dict for app.py to render buttons
    return {
        "type": "selection_required",
        "message": f"I found {len(matches)} players named **{q.title()}**. Which one did you mean?",
        "matches": matches[:5],  # cap at 5 buttons
    }

def get_fantasy_player_stats(query_name: str) -> str:
    """Retrieves PPR fantasy points for a player using the correct NFL season year."""
    _ensure_player_cache()
    year = _current_nfl_season_year()
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


# ----------------------------------------------------
# Improvement #2 — Injury Reports
# ----------------------------------------------------

def get_player_injury(player_name: str) -> str:
    """
    Returns injury status, body part, practice participation, and notes
    directly from the Sleeper player cache — no extra API call needed.
    """
    _ensure_player_cache()
    q = clean_query(player_name)

    matches = [p for p in _PLAYER_CACHE.values()
               if p.get("full_name") and is_fuzzy_match(q, p["full_name"])]

    # Prefer active players
    active = [p for p in matches if p.get("active")]
    if active:
        matches = active

    if not matches:
        return f"I couldn't find injury information for '{player_name}'."

    p = matches[0]
    name   = p.get("full_name", player_name)
    status = p.get("injury_status") or "Healthy"
    part   = p.get("injury_body_part")
    notes  = p.get("injury_notes")
    practice = p.get("practice_participation") or p.get("practice_description")

    lines = [f"🏥 **{name} — Injury Report**", f"- **Status:** {status}"]
    if part:
        lines.append(f"- **Body Part:** {part}")
    if practice:
        lines.append(f"- **Practice:** {practice}")
    if notes:
        lines.append(f"- **Notes:** {notes}")
    # Depth chart context — who starts if this player is out? (#2)
    depth_pos   = p.get("depth_chart_position", "")
    depth_order = p.get("depth_chart_order")
    if depth_pos and depth_order is not None:
        ordinal = {1: "Starter", 2: "Backup", 3: "3rd string"}.get(int(depth_order), f"#{depth_order}")
        lines.append(f"- **Depth Chart:** {ordinal} {depth_pos}")
    if status == "Healthy":
        lines.append("- No current injury designation — expected to play.")

    return "\n".join(lines)


# ----------------------------------------------------
# Improvement #3 — Weekly Player Stats
# ----------------------------------------------------

def get_player_weekly_stats(player_name: str, num_weeks: int = 5) -> str:
    """
    Returns the last N weeks of game stats for a player from Sleeper.
    Surfaces passing, rushing, and receiving lines depending on position.
    """
    _ensure_player_cache()
    year = _current_nfl_season_year()
    q = clean_query(player_name)

    # Find the player record
    matches = [p for p in _PLAYER_CACHE.values()
               if p.get("full_name") and is_fuzzy_match(q, p["full_name"])
               and p.get("active")]
    if not matches:
        return f"No weekly stats found for '{player_name}'."

    player = matches[0]
    pid    = player.get("player_id") or next(
        (k for k, v in _PLAYER_CACHE.items() if v is player), None
    )
    pos    = player.get("position", "")
    name   = player.get("full_name", player_name)

    # Fetch the last num_weeks weeks concurrently
    def _fetch_week(week: int):
        url = ENDPOINTS["sleeper_stats_week"].format(year=year, week=week)
        data = fetch_json(url)
        return week, data.get(pid, {}) if "__error" not in data else {}

    # Determine current week (approximate from today's date)
    today = datetime.datetime.now()
    season_start = datetime.datetime(today.year if today.month >= 9 else today.year - 1, 9, 1)
    current_week = min(max(1, int((today - season_start).days // 7) + 1), 18)
    weeks_to_fetch = list(range(max(1, current_week - num_weeks), current_week + 1))

    week_stats = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        for week, stats in pool.map(_fetch_week, weeks_to_fetch):
            if stats:
                week_stats[week] = stats

    if not week_stats:
        return f"No weekly stats available for {name} this season yet."

    lines = [f"📊 **{name} — Last {len(week_stats)} Weeks**"]
    for week in sorted(week_stats.keys(), reverse=True):
        s = week_stats[week]
        pts = round(s.get("pts_ppr", 0), 1)

        if pos == "QB":
            stat_line = (
                f"Pass: {s.get('pass_yd', 0)} yds / {s.get('pass_td', 0)} TD / "
                f"{s.get('pass_int', 0)} INT | "
                f"Rush: {s.get('rush_yd', 0)} yds | "
                f"**{pts} pts**"
            )
        elif pos in ("RB",):
            stat_line = (
                f"Rush: {s.get('rush_yd', 0)} yds / {s.get('rush_td', 0)} TD | "
                f"Rec: {s.get('rec', 0)} / {s.get('rec_yd', 0)} yds | "
                f"**{pts} pts**"
            )
        elif pos in ("WR", "TE"):
            stat_line = (
                f"Rec: {s.get('rec', 0)} / {s.get('rec_yd', 0)} yds / "
                f"{s.get('rec_td', 0)} TD | "
                f"**{pts} pts**"
            )
        else:
            stat_line = f"**{pts} PPR pts**"

        lines.append(f"- **Wk {week}:** {stat_line}")

    return "\n".join(lines)


# ----------------------------------------------------
# Improvement #4 — Fantasy Sit/Start
# ----------------------------------------------------

def get_fantasy_sit_start(player_name: str, opponent_team: Optional[str] = None) -> str:
    """
    Builds a sit/start data package: recent weekly stats + injury status +
    upcoming matchup. Gemini uses this to generate the actual recommendation.
    """
    _ensure_player_cache()
    q = clean_query(player_name)

    matches = [p for p in _PLAYER_CACHE.values()
               if p.get("full_name") and is_fuzzy_match(q, p["full_name"])
               and p.get("active")]
    if not matches:
        return f"I couldn't find fantasy data for '{player_name}'."

    player = matches[0]
    name   = player.get("full_name", player_name)
    team   = player.get("team", "FA")
    pos    = player.get("position", "?")

    # Gather components
    weekly  = get_player_weekly_stats(name, num_weeks=4)
    injury  = get_player_injury(name)
    matchup = get_next_game(team) if team != "FA" else "No upcoming game found (free agent)."

    opp_context = f" vs {opponent_team}" if opponent_team else ""

    return (
        f"🎯 **Fantasy Sit/Start Data: {name} ({pos}, {team}){opp_context}**\n\n"
        f"{weekly}\n\n"
        f"{injury}\n\n"
        f"**Upcoming Matchup:** {matchup}"
    )


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


# ----------------------------------------------------
# #3 — Player Comparison
# ----------------------------------------------------

def _get_player_data_block(name: str) -> str:
    """
    Builds a stat + injury + depth chart block for a single player.
    Used internally by comparison and trade functions.
    """
    _ensure_player_cache()
    q = clean_query(name)
    matches = [p for p in _PLAYER_CACHE.values()
               if p.get("full_name") and is_fuzzy_match(q, p["full_name"])
               and p.get("active")]
    if not matches:
        return f"No data found for '{name}'."

    p = matches[0]
    full   = p.get("full_name", name)
    team   = p.get("team", "FA")
    pos    = p.get("position", "?")
    exp    = p.get("years_exp", "?")
    inj    = p.get("injury_status") or "Healthy"
    inj_part = p.get("injury_body_part", "")

    depth_pos   = p.get("depth_chart_position", "")
    depth_order = p.get("depth_chart_order")
    depth_str   = ""
    if depth_pos and depth_order is not None:
        ordinal = {1: "Starter", 2: "Backup", 3: "3rd string"}.get(int(depth_order), f"#{depth_order}")
        depth_str = f" | Depth: {ordinal} {depth_pos}"

    weekly = get_player_weekly_stats(full, num_weeks=4)
    season = get_fantasy_player_stats(full)

    return (
        f"**{full}** ({pos}, {team}, {exp} yrs exp{depth_str})\n"
        f"Injury: {inj}" + (f" ({inj_part})" if inj_part else "") + "\n"
        f"{season}\n"
        f"{weekly}"
    )


def get_player_comparison(player_a: str, player_b: str) -> str:
    """
    Fetches stats, injury status, and depth chart for two players in parallel.
    Returns a side-by-side data block for Gemini to analyse.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(_get_player_data_block, player_a)
        future_b = pool.submit(_get_player_data_block, player_b)
        block_a = future_a.result()
        block_b = future_b.result()

    return (
        f"⚔️ **Player Comparison**\n\n"
        f"--- PLAYER 1: {player_a} ---\n{block_a}\n\n"
        f"--- PLAYER 2: {player_b} ---\n{block_b}"
    )


# ----------------------------------------------------
# #5 — Trade Advice
# ----------------------------------------------------

def get_trade_analysis(player_give: str, player_receive: str) -> str:
    """
    Builds a data package comparing two players for trade evaluation.
    Includes recent weekly stats, injury status, depth chart, and schedule context.
    Gemini uses this to write the actual trade recommendation.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        future_give    = pool.submit(_get_player_data_block, player_give)
        future_receive = pool.submit(_get_player_data_block, player_receive)
        block_give    = future_give.result()
        block_receive = future_receive.result()

    # Get next game for each to add schedule context
    _ensure_player_cache()

    def _next_game_for(name: str) -> str:
        q = clean_query(name)
        matches = [p for p in _PLAYER_CACHE.values()
                   if p.get("full_name") and is_fuzzy_match(q, p["full_name"])
                   and p.get("active")]
        if matches and matches[0].get("team"):
            return get_next_game(matches[0]["team"])
        return "Schedule unavailable."

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        sched_give    = pool.submit(_next_game_for, player_give)
        sched_receive = pool.submit(_next_game_for, player_receive)

    return (
        f"🔄 **Trade Analysis**\n\n"
        f"--- GIVING AWAY: {player_give} ---\n"
        f"{block_give}\n"
        f"Next game: {sched_give.result()}\n\n"
        f"--- RECEIVING: {player_receive} ---\n"
        f"{block_receive}\n"
        f"Next game: {sched_receive.result()}"
    )


# ----------------------------------------------------
# Waiver Wire Recommendations
# ----------------------------------------------------

# Skill positions relevant to fantasy waiver decisions
_WAIVER_POSITIONS = {"QB", "RB", "WR", "TE"}

def get_waiver_recommendations(position: Optional[str] = None, top_n: int = 5) -> str:
    """
    Ranks unclaimed free agents by recent PPR performance and returns the
    top picks with injury status, upcoming matchup, and schedule context
    for Gemini to analyse.

    Ranking uses a weighted recent PPR score (most recent week × 3, prior
    weeks × 2 and × 1) so hot-streak players surface over stale producers.

    Args:
        position: Optional filter — "QB", "RB", "WR", or "TE".
        top_n:    Number of candidates to return (default 5).
    """
    _ensure_player_cache()
    year = _current_nfl_season_year()

    pos_filter = position.upper().strip() if position else None
    if pos_filter and pos_filter not in _WAIVER_POSITIONS:
        return f"'{position}' isn't a recognised fantasy position. Try QB, RB, WR, or TE."

    # ── Step 1: identify free agents ─────────────────────────────
    free_agents = [
        p for p in _PLAYER_CACHE.values()
        if p.get("active")
        and p.get("position") in _WAIVER_POSITIONS
        and not p.get("team")
        and p.get("full_name")
        and (pos_filter is None or p.get("position") == pos_filter)
    ]

    if not free_agents:
        label = f"{pos_filter} " if pos_filter else ""
        return f"No {label}free agents found in the player cache right now."

    # ── Step 2: fetch last 3 weeks concurrently ───────────────────
    today        = datetime.datetime.now()
    season_start = datetime.datetime(
        today.year if today.month >= 9 else today.year - 1, 9, 1
    )
    current_week = min(max(1, int((today - season_start).days // 7) + 1), 18)
    recent_weeks = list(range(max(1, current_week - 3), current_week + 1))

    def _fetch_week_data(week: int) -> tuple[int, dict]:
        url  = ENDPOINTS["sleeper_stats_week"].format(year=year, week=week)
        data = fetch_json(url)
        return week, data if "__error" not in data else {}

    week_data: dict[int, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for week, data in pool.map(_fetch_week_data, recent_weeks):
            week_data[week] = data

    # ── Step 3: score by weighted recent PPR ─────────────────────
    scored = []
    for p in free_agents:
        pid = p.get("player_id") or next(
            (k for k, v in _PLAYER_CACHE.items() if v is p), None
        )
        if not pid:
            continue

        recent_pts = [
            week_data[w].get(pid, {}).get("pts_ppr", 0)
            for w in recent_weeks
            if week_data.get(w)
        ]
        if not any(recent_pts):
            continue

        weights  = list(range(1, len(recent_pts) + 1))
        weighted = sum(pt * w for pt, w in zip(recent_pts, weights))
        total    = sum(recent_pts)
        scored.append((weighted, total, p, pid, recent_pts))

    if not scored:
        label = f"{pos_filter} " if pos_filter else ""
        return f"No {label}free agents have recorded fantasy points recently."

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:top_n]

    # ── Step 4: fetch next game for each candidate (schedule difficulty) ──
    def _next_game_for_player(p: dict) -> str:
        """Returns the next game string for a player's team, or 'Free agent'."""
        team = p.get("team")
        if not team:
            return "Free agent — no team assigned"
        try:
            return get_next_game(team)
        except Exception:
            return "Schedule unavailable"

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(top), 5)) as pool:
        schedules = list(pool.map(lambda item: _next_game_for_player(item[2]), top))

    # ── Step 5: build output block ────────────────────────────────
    label = f"{pos_filter} " if pos_filter else ""
    lines = [f"🏆 **Top {len(top)} {label}Waiver Wire Targets (Recent Trend + Matchup)**\n"]

    for rank, ((weighted, total, p, pid, recent_pts), schedule) in enumerate(
        zip(top, schedules), 1
    ):
        name     = p.get("full_name")
        pos      = p.get("position", "?")
        inj      = p.get("injury_status") or "Healthy"
        inj_note = f" ⚠️ {inj}" if inj != "Healthy" else ""

        wk_labels  = [f"Wk {w}: {pt:.1f}" for w, pt in zip(recent_weeks, recent_pts)]
        recent_str = " | ".join(wk_labels)

        lines.append(
            f"**{rank}. {name}** ({pos}){inj_note}\n"
            f"   Recent: {recent_str} → **{total:.1f} pts last {len(recent_pts)} wks**\n"
            f"   Next: {schedule}"
        )

    return "\n".join(lines)
