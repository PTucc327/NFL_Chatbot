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
import threading
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
CACHE_TTL = 60 * 60 * 6          # 6 hours — team metadata, standings, scores
INJURY_CACHE_TTL = 60 * 60 * 4   # 4 hours — player/injury cache (practice reports
                                  # land Wed/Thu/Fri and go stale quickly in-season)
REQUEST_TIMEOUT = 10

ENDPOINTS = {
    "scoreboard":     "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "teams":          "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams",
    "standings":      "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
    "sleeper_players":    "https://api.sleeper.app/v1/players/nfl",
    "sleeper_stats":      "https://api.sleeper.app/v1/stats/nfl/regular/{year}",
    "sleeper_stats_week": "https://api.sleeper.app/v1/stats/nfl/regular/{year}/{week}",
    "sleeper_trending_add": "https://api.sleeper.app/v1/players/nfl/trending/add",
    "sleeper_state":      "https://api.sleeper.app/v1/state/nfl",
    "summary":        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary",
    "team_stats":     "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/statistics",
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


_WEEK_CACHE: Dict[str, Any] = {"week": None, "at": 0.0}
_WEEK_CACHE_TTL = 60 * 60  # weeks roll over once a week; an hour is plenty


def _current_nfl_week() -> int:
    """
    Current regular-season week (1-18) from Sleeper's /state/nfl, which flips
    to the next week on Tuesday. Falls back to counting weeks from the
    Tuesday before kickoff (the Thursday after Labor Day).
    """
    if _WEEK_CACHE["week"] and time.time() - _WEEK_CACHE["at"] < _WEEK_CACHE_TTL:
        return _WEEK_CACHE["week"]

    state = fetch_json(ENDPOINTS["sleeper_state"])
    week = state.get("week") if state.get("season_type") == "regular" else None
    if not isinstance(week, int) or not 1 <= week <= 18:
        year = _current_nfl_season_year()
        labor_day = datetime.date(year, 9, 1)
        labor_day += datetime.timedelta(days=(0 - labor_day.weekday()) % 7)
        week_one_tuesday = labor_day + datetime.timedelta(days=1)
        days = (datetime.date.today() - week_one_tuesday).days
        week = min(max(1, days // 7 + 1), 18)

    _WEEK_CACHE.update(week=week, at=time.time())
    return week

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

def _load_rosters() -> Dict[str, Any]:
    """
    Loads data/rosters.json produced by scripts/update_data.py.
    Returns the full dict (with _meta + rosters keys), or an empty
    structure if the file hasn't been generated yet.
    """
    path = os.path.join(_DATA_DIR, "rosters.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Accept both the wrapped format {_meta, rosters} and a bare dict
        return data if "rosters" in data else {"rosters": data, "_meta": {}}
    except FileNotFoundError:
        logger.warning(
            "data/rosters.json not found — run scripts/update_data.py to generate it."
        )
        return {"rosters": {}, "_meta": {}}
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse rosters.json: {e}")
        return {"rosters": {}, "_meta": {}}

_ROSTERS_DATA: Dict[str, Any] = _load_rosters()

# -------------------------
# Local Caches
# -------------------------
_TEAM_CACHE: Dict[str, Dict[str, Any]] = {}
_TEAM_CACHE_LAST = 0
_PLAYER_CACHE: Dict[str, Dict[str, Any]] = {}
_PLAYER_CACHE_LAST = 0

# _dispatch() now fans intents out across a ThreadPoolExecutor, so multiple
# threads can call ensure_team_cache()/_ensure_player_cache() at the same
# instant. Without a lock, each thread sees an empty/stale cache and fires
# its own redundant fetch (wasted requests + a brief window of duplicate
# network calls). These locks make cache population "first one in wins,
# everyone else waits and reuses the result" instead of racing.
_TEAM_CACHE_LOCK = threading.Lock()
_PLAYER_CACHE_LOCK = threading.Lock()

# -------------------------
# Team Cache Management
# -------------------------

def ensure_team_cache():
    """Populate team metadata with robust error handling."""
    global _TEAM_CACHE, _TEAM_CACHE_LAST
    now = time.time()
    if _TEAM_CACHE and now - _TEAM_CACHE_LAST < CACHE_TTL:
        return

    with _TEAM_CACHE_LOCK:
        # Re-check now that we hold the lock — another thread may have
        # already refreshed the cache while we were waiting.
        now = time.time()
        if _TEAM_CACHE and now - _TEAM_CACHE_LAST < CACHE_TTL:
            return

        data = fetch_json(ENDPOINTS["teams"])
        if "__error" in data:
            logger.error(
                f"component=api_client action=ensure_team_cache "
                f"endpoint={ENDPOINTS['teams']} error={data['__error']}"
            )
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


# ESPN and Sleeper agree on every team abbreviation except Washington.
_ESPN_TO_SLEEPER_ABBR = {"WSH": "WAS"}


def sleeper_team_abbr(team: Optional[str]) -> Optional[str]:
    """Resolves a team name/nickname/abbreviation to Sleeper's abbreviation (e.g. 'MIN')."""
    if not team:
        return None
    upper = team.strip().upper()
    if upper in _ESPN_TO_SLEEPER_ABBR.values():
        return upper
    meta = find_team(team)
    if not meta or not meta.get("abbr"):
        return None
    abbr = meta["abbr"].upper()
    return _ESPN_TO_SLEEPER_ABBR.get(abbr, abbr)

# ----------------------------------------------------
# News & Scores (Conversational & Dynamic)
# ----------------------------------------------------

def _fetch_rss_thread(url: str) -> List[Dict[str, str]]:
    """Internal helper for concurrent RSS fetching.

    feedparser.parse(url) opens its own HTTP connection with no configurable
    timeout — on a stalled server it can block a worker thread indefinitely.
    Fix: fetch the raw bytes ourselves via requests (with a timeout) and pass
    the text to feedparser so it never makes a network call of its own.
    """
    try:
        resp = requests.get(url, timeout=8, headers={"User-Agent": "NFL-Pro-Bot/1.0"})
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        return [{"title": e.title, "link": e.link, "desc": e.get("summary", "")} for e in feed.entries]
    except Exception as e:
        logger.warning(f"RSS fetch failed for {url}: {e}")
        return []


def get_league_headlines(limit: int = 5) -> str:
    """
    Fetches general NFL news not tied to any one team — for 'what's
    happening around the league' style questions. Unlike get_team_news(),
    this doesn't score/filter articles against team-name tokens; the
    sources themselves are already league-wide.
    """
    sources = [
        "https://sports.yahoo.com/nfl/rss.xml",
        "https://profootballtalk.nbcsports.com/feed/",
        "https://news.google.com/rss/search?q=NFL",
    ]

    all_articles = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(_fetch_rss_thread, url): url for url in sources}
        for future in concurrent.futures.as_completed(futures):
            all_articles.extend(future.result())

    seen = set()
    deduped = []
    for art in all_articles:
        key = (art.get("title") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(art)

    if not deduped:
        return "Couldn't pull any league headlines right now — try again in a bit."

    md = ["🏈 **Around the NFL right now:**\n"]
    for a in deduped[:limit]:
        md.append(f"- ⭐ **[{a['title']}]({a['link']})**")

    return "\n".join(md)


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


def _live_situation(situation: Optional[Dict[str, Any]], teams: List[Dict[str, Any]]) -> str:
    """
    In-game context from ESPN's live `situation`: possession, down and
    distance, red zone, last play. '' when there's nothing to report
    (between quarters, halftime, or not a live game).
    """
    if not situation:
        return ""
    parts = []
    poss_id = str(situation.get("possession") or "")
    poss = next((t.get("team", {}).get("abbreviation") for t in teams
                 if str(t.get("team", {}).get("id")) == poss_id), None)
    dd = situation.get("downDistanceText") or situation.get("shortDownDistanceText")
    if poss and dd:
        parts.append(f"🏈 {poss} ball, {dd}")
    elif poss:
        parts.append(f"🏈 {poss} ball")
    if situation.get("isRedZone"):
        parts.append("🔴 red zone")
    last = (situation.get("lastPlay") or {}).get("text")
    text = " · ".join(parts)
    if last:
        text += (" — " if text else "") + f"last play: {last}"
    return text


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

        if state == "pre":
            # No score before kickoff — "0 @ 0" reads like a result or a record.
            line = f"{aw_name} @ {hm_name}{venue_str} ({to_et(dt)}{_broadcast_suffix(comp)})"
        else:
            line = f"{aw_name} **{aw_score}** @ {hm_name} **{hm_score}**{venue_str} ({to_et(dt)}, {detail})"
            if state == "in" and (live := _live_situation(comp.get("situation"), teams)):
                line += f"\n  - {live}"

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

_CONF_SHORT = {"American Football Conference": "AFC", "National Football Conference": "NFC"}
# ESPN clincher codes, shown once teams start clinching late in the season.
_CLINCH = {"z": "clinched #1 seed", "*": "clinched #1 seed", "y": "clinched division",
           "x": "clinched playoff berth", "e": "eliminated"}


def _standings_row(entry: Dict[str, Any]) -> Dict[str, Any]:
    stats = {s.get("name"): s.get("displayValue") for s in entry.get("stats", [])}
    team = entry.get("team", {})
    w, l, t = stats.get("wins", "0"), stats.get("losses", "0"), stats.get("ties", "0")
    try:
        seed = int(float(stats.get("playoffSeed") or 0)) or None
    except ValueError:
        seed = None
    return {
        "name": team.get("displayName", "Unknown"),
        "abbr": team.get("abbreviation", ""),
        "w": int(float(w)), "l": int(float(l)), "t": int(float(t)),
        "record": f"{w}-{l}" + (f"-{t}" if t not in ("0", "0.000") else ""),
        "pct": stats.get("winPercent", ""),
        "div": stats.get("divisionRecord") or stats.get("vs. Div.", ""),
        "diff": stats.get("differential") or stats.get("pointDifferential", ""),
        "streak": stats.get("streak", ""),
        "seed": seed,
        "clinch": _CLINCH.get((stats.get("clincher") or "").lower(), ""),
    }


def _standings_groups(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    [{"conf": "AFC", "division": "AFC East" or None, "rows": [...]}].
    Handles division-level data (level=3: conference -> division -> entries)
    and plain conference-level data (conference -> entries).
    """
    groups = []
    for conf in data.get("children", []):
        conf_name = _CONF_SHORT.get(conf.get("name", ""), conf.get("name", ""))
        divisions = conf.get("children") or []
        if divisions:
            for div in divisions:
                rows = [_standings_row(e) for e in div.get("standings", {}).get("entries", [])]
                groups.append({"conf": conf_name, "division": div.get("name"), "rows": rows})
        else:
            rows = [_standings_row(e) for e in conf.get("standings", {}).get("entries", [])]
            groups.append({"conf": conf_name, "division": None, "rows": rows})
    for g in groups:
        g["rows"].sort(key=lambda r: (-(r["w"] + 0.5 * r["t"]) / max(1, r["w"] + r["l"] + r["t"]),
                                      r["seed"] or 99))
    return groups


def _standings_table(rows: List[Dict[str, Any]], highlight: Optional[str] = None) -> List[str]:
    out = ["| Team | W-L | Div | Diff | Streak | Seed |", "|---|---|---|---|---|---|"]
    for r in rows:
        name = f"**{r['name']}**" if highlight and r["name"] == highlight else r["name"]
        clinch = f" ({r['clinch']})" if r["clinch"] else ""
        out.append(f"| {name}{clinch} | {r['record']} | {r['div'] or '-'} | {r['diff'] or '-'} | "
                   f"{r['streak'] or '-'} | {r['seed'] or '-'} |")
    return out


def get_standings(team_query: Optional[str] = None, division: Optional[str] = None,
                  conference: Optional[str] = None) -> str:
    """
    Division standings. `division` ("NFC East") → that division; a team →
    its division plus its conference seed; `conference` ("AFC") → its four
    divisions; nothing → all eight divisions.
    """
    data = fetch_json(ENDPOINTS["standings"], params={"level": 3})
    if "__error" in data:
        return "I'm having a bit of trouble pulling the latest standings. Check back in a bit! ⚠️"
    groups = _standings_groups(data)

    if division:
        wanted = division.strip().lower()
        match = next((g for g in groups if (g["division"] or "").lower() == wanted), None)
        if match:
            return "\n".join([f"📊 **{match['division']} Standings**", ""] + _standings_table(match["rows"]))

    if team_query:
        team_meta = find_team(team_query)
        target = (team_meta or {}).get("displayName", "")
        for g in groups:
            row = next((r for r in g["rows"] if target and target.lower() in r["name"].lower()), None)
            if not row:
                continue
            title = g["division"] or g["conf"]
            seed_line = ""
            if row["seed"]:
                status = "in playoff position" if row["seed"] <= 7 else "outside the playoff spots"
                seed_line = f"\n{row['name']} are the **#{row['seed']} seed** in the {g['conf']} ({status})."
            return "\n".join([f"📊 **{title} Standings**", ""]
                             + _standings_table(g["rows"], highlight=row["name"])) + seed_line
        return f"I couldn't find the standings for '{team_query}'."

    conf = conference.upper() if conference and conference.upper() in ("AFC", "NFC") else None
    out = [f"📊 **{conf + ' ' if conf else 'NFL '}Standings Update:**"]
    for g in groups:
        if conf and g["conf"] != conf:
            continue
        out += ["", f"**{g['division'] or g['conf']}**"]
        out += [f"- {r['name']}: **{r['record']}**" + (f" (#{r['seed']} seed)" if r["seed"] else "")
                for r in g["rows"]]
    return "\n".join(out)


def get_playoff_picture(conference: Optional[str] = None) -> str:
    """
    Current playoff seeding per conference: seeds 1-4 (division leaders),
    5-7 (wild cards), then the teams in the hunt with games behind the 7th
    seed. Seeds come from ESPN, which applies the NFL tiebreakers.
    """
    data = fetch_json(ENDPOINTS["standings"], params={"level": 3})
    if "__error" in data:
        return "I'm having a bit of trouble pulling the playoff picture right now. ⚠️"
    groups = _standings_groups(data)
    conf = conference.upper() if conference and conference.upper() in ("AFC", "NFC") else None
    week = None
    try:
        week = _current_nfl_week()
    except Exception:
        pass

    out = [f"🏆 **NFL Playoff Picture**" + (f" (heading into Week {week})" if week else "")]
    for conf_name in ("AFC", "NFC"):
        if conf and conf_name != conf:
            continue
        rows = sorted((r for g in groups if g["conf"] == conf_name for r in g["rows"] if r["seed"]),
                      key=lambda r: r["seed"])
        if not rows:
            continue
        div_of = {r["name"]: g["division"] for g in groups if g["conf"] == conf_name for r in g["rows"]}
        out += ["", f"**{conf_name}**"]
        in_field = [r for r in rows if r["seed"] <= 7]
        for r in in_field:
            role = f"{div_of.get(r['name']) or 'division'} leader" if r["seed"] <= 4 else "wild card"
            clinch = f", {r['clinch']}" if r["clinch"] else ""
            out.append(f"{r['seed']}. {r['name']} ({r['record']}) — {role}{clinch}")
        chasing = [r for r in rows if r["seed"] > 7 and r["clinch"] != "eliminated"]
        if in_field and chasing:
            last_in = in_field[-1]
            hunt = []
            for r in chasing[:4]:
                gb = ((last_in["w"] - r["w"]) + (r["l"] - last_in["l"])) / 2
                gb_str = "tied" if gb <= 0 else f"{_fmt_num(gb)} GB"
                hunt.append(f"{r['name']} ({r['record']}, {gb_str})")
            out.append("*In the hunt:* " + "; ".join(hunt))
    return "\n".join(out)


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
    teams = comp.get("competitors", [])
    away = next((c for c in teams if c.get("homeAway") == "away"), None)
    home = next((c for c in teams if c.get("homeAway") == "home"), None)
    when = to_et(parse_iso_datetime(past[0].get("date")))
    if not away or not home:
        scores = [f"{c['team']['displayName']} {c.get('score', {}).get('displayValue', '0')}" for c in teams]
        return f"In their last outing, here's how it finished: {' - '.join(scores)} ({when}). 🏟️"
    score = lambda c: (c.get("score") or {}).get("displayValue", "0")
    venue = (comp.get("venue") or {}).get("fullName")
    # State home/away explicitly — without it the AI guessed the location.
    return (f"In their last outing: {away['team']['displayName']} {score(away)} @ "
            f"{home['team']['displayName']} {score(home)} (final, {when}"
            + (f", at {venue}" if venue else "") + "). 🏟️")


def _fmt_num(value: Any) -> str:
    """222.0 -> '222', 1234 -> '1,234', 18.94 -> '18.9' (Sleeper sends floats)."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{int(num):,}" if num.is_integer() else f"{num:,.1f}"


def _broadcast_suffix(comp: Dict[str, Any]) -> str:
    """', Prime Video' for a competition with a listed TV broadcast, else ''."""
    names = [n for b in comp.get("broadcasts", []) for n in b.get("names", [])]
    if not names:
        names = [m for b in comp.get("broadcasts", [])
                 if (m := (b.get("media") or {}).get("shortName"))]
    return f", {names[0]}" if names else ""


def _to_eastern(dt: datetime.datetime) -> datetime.datetime:
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # no tz database: approximate EDT/EST
        offset = -4 if 3 <= dt.month <= 11 else -5
        return dt.astimezone(datetime.timezone(datetime.timedelta(hours=offset)))


def _slot_label(et: datetime.datetime) -> str:
    """Primetime label for a kickoff in Eastern time."""
    if et.hour >= 19:
        return {3: "Thursday Night Football", 6: "Sunday Night Football",
                0: "Monday Night Football"}.get(et.weekday(), "Primetime")
    return ""


def get_week_schedule() -> str:
    """This week's full slate grouped by day, with primetime games and bye teams."""
    data = fetch_json(ENDPOINTS["scoreboard"])
    if "__error" in data:
        return "I'm having trouble reaching the NFL schedule right now. 🏈"

    week = (data.get("week") or {}).get("number")
    events = sorted(data.get("events", []), key=lambda e: e.get("date", ""))
    if not events:
        return "There are no NFL games on the schedule this week."

    out = [f"🗓️ **NFL Week {week} Schedule**" if week else "🗓️ **This Week's NFL Schedule**"]
    current_day = None
    for ev in events:
        comp = ev.get("competitions", [{}])[0]
        teams = comp.get("competitors", [])
        away = next((t for t in teams if t.get("homeAway") == "away"), {})
        home = next((t for t in teams if t.get("homeAway") == "home"), {})
        dt = parse_iso_datetime(ev.get("date"))
        et = _to_eastern(dt) if dt else None
        day = f"{et:%A, %b} {et.day}" if et else "TBD"
        if day != current_day:
            out.append(f"\n**{day}**")
            current_day = day

        state = comp.get("status", {}).get("type", {}).get("state", "pre")
        aw = away.get("team", {}).get("displayName", "TBD")
        hm = home.get("team", {}).get("displayName", "TBD")
        if state == "pre":
            when = f"{et:%I:%M %p} ET".lstrip("0") if et else "TBD"
            matchup = f"{aw} @ {hm} — {when}{_broadcast_suffix(comp)}"
        else:
            detail = comp.get("status", {}).get("type", {}).get("shortDetail", "")
            matchup = (f"{aw} **{away.get('score', '0')}** @ {hm} "
                       f"**{home.get('score', '0')}** ({detail})")
        slot = _slot_label(et) if et else ""
        out.append(f"- {matchup}" + (f" 🌙 *{slot}*" if slot else ""))

    byes = [t.get("displayName") for t in (data.get("week") or {}).get("teamsOnBye", [])]
    out.append(f"\n**On bye:** {', '.join(byes)}" if byes else "\n**On bye:** none this week")
    return "\n".join(out)


def get_team_schedule(team_name: str, opponent: Optional[str] = None) -> str:
    """
    A team's full regular season: results so far, then remaining games,
    with record and bye week. With `opponent`, only games against that team
    ("when do the Cowboys play the Eagles?").
    """
    meta = find_team(team_name)
    if not meta:
        return f"I couldn't find a team named '{team_name}'."
    data = fetch_json(meta["schedule_url"])
    if "__error" in data:
        return f"I'm having trouble pulling the {meta['displayName']} schedule right now."

    opp_meta = find_team(opponent) if opponent else None
    record = (data.get("team") or {}).get("recordSummary")
    bye = data.get("byeWeek")
    header = f"🗓️ **{meta['displayName']} Schedule**"
    if opp_meta:
        header = f"🗓️ **{meta['displayName']} vs {opp_meta['displayName']} this season**"
    out = [header + (f"  (record: {record}" + (f", bye: Week {bye}" if bye else "") + ")"
                     if record else "")]

    next_marked = False
    for ev in data.get("events", []):
        comp = ev.get("competitions", [{}])[0]
        teams = comp.get("competitors", [])
        us = next((t for t in teams if str(t.get("team", {}).get("id")) == meta["id"]), None)
        them = next((t for t in teams if t is not us), None)
        if not us or not them:
            continue
        if opp_meta and str(them.get("team", {}).get("id")) != opp_meta["id"]:
            continue

        week = (ev.get("week") or {}).get("number", "?")
        where = "vs" if us.get("homeAway") == "home" else "@"
        opp_name = them.get("team", {}).get("displayName", "TBD")
        state = comp.get("status", {}).get("type", {}).get("state", "pre")
        dt = parse_iso_datetime(ev.get("date"))
        if ev.get("timeValid") is False and dt:
            # Late-season kickoffs are set later ("flex"); ESPN sends midnight.
            et = _to_eastern(dt)
            when = f"{et:%a %b} {et.day}, time TBD"
        else:
            when = to_et(dt)
        if state == "post":
            ours = (us.get("score") or {}).get("displayValue", "?")
            theirs = (them.get("score") or {}).get("displayValue", "?")
            result = "W" if us.get("winner") else ("L" if them.get("winner") else "T")
            out.append(f"- Wk {week}: {where} {opp_name} — **{result} {ours}-{theirs}**")
        else:
            marker = ""
            if state == "in":
                marker = " 🔴 **LIVE**"
            elif not next_marked:
                marker, next_marked = " ⏭️ **next**", True
            out.append(f"- Wk {week}: {where} {opp_name} — {when}{marker}")

    if len(out) == 1:
        if opp_meta:
            return (f"The {meta['displayName']} don't play the {opp_meta['displayName']} "
                    f"in the regular season this year.")
        return f"I couldn't find any games on the {meta['displayName']} schedule."
    return "\n".join(out)


# ----------------------------------------------------
# Box Scores
# ----------------------------------------------------
# ESPN's per-game summary (~600 KB) has the line score, team stats, leaders
# and scoring plays. Final games are cached for a day, live ones briefly.
_SUMMARY_FINAL_TTL = 60 * 60 * 24
_SUMMARY_LIVE_TTL = 60
_SUMMARY_CACHE: Dict[str, tuple] = {}   # event id -> (fetched_at, final?, summary)
_SUMMARY_CACHE_LOCK = threading.Lock()

# Team stats worth comparing, in display order (ESPN labels).
_BOX_TEAM_STATS = (
    "Total Yards", "Passing", "Rushing", "Comp/Att", "Yards per Play",
    "1st Downs", "3rd down efficiency", "4th down efficiency",
    "Red Zone (Made-Att)", "Turnovers", "Sacks-Yards Lost", "Penalties", "Possession",
)


def _get_game_summary(event_id: str) -> Optional[Dict[str, Any]]:
    with _SUMMARY_CACHE_LOCK:
        hit = _SUMMARY_CACHE.get(event_id)
    if hit and time.time() - hit[0] < (_SUMMARY_FINAL_TTL if hit[1] else _SUMMARY_LIVE_TTL):
        return hit[2]
    data = fetch_json(ENDPOINTS["summary"], params={"event": event_id})
    if not isinstance(data, dict) or "__error" in data or "boxscore" not in data:
        return None
    comp = (data.get("header") or {}).get("competitions", [{}])[0]
    final = comp.get("status", {}).get("type", {}).get("state") == "post"
    with _SUMMARY_CACHE_LOCK:
        _SUMMARY_CACHE[event_id] = (time.time(), final, data)
    return data


def _find_game(team_meta: Dict[str, Any], opponent: Optional[str],
               week: Optional[int]) -> tuple:
    """
    (event, error message) for a team's game: the given week, else the most
    recent game against `opponent`, else the latest game that has started.
    """
    data = fetch_json(team_meta["schedule_url"])
    if "__error" in data:
        return None, f"I'm having trouble pulling the {team_meta['displayName']} schedule right now."
    opp_meta = find_team(opponent) if opponent else None
    started = []
    for ev in data.get("events", []):
        comp = ev.get("competitions", [{}])[0]
        if comp.get("status", {}).get("type", {}).get("state") not in ("in", "post"):
            continue
        ids = {str(c.get("team", {}).get("id")) for c in comp.get("competitors", [])}
        if opp_meta and opp_meta["id"] not in ids:
            continue
        if week and (ev.get("week") or {}).get("number") != week:
            continue
        started.append(ev)
    if not started:
        if week and data.get("byeWeek") == week:
            return None, f"The {team_meta['displayName']} were on bye in Week {week}."
        what = f" against the {opp_meta['displayName']}" if opp_meta else ""
        when = f" in Week {week}" if week else " yet this season"
        return None, f"The {team_meta['displayName']} haven't played{what}{when}."
    return started[-1], None


def get_box_score(team_name: str, opponent: Optional[str] = None,
                  week: Optional[int] = None) -> str:
    """
    Box score for a team's game (latest, a given week, or the latest vs an
    opponent): line score, team stat comparison, leaders, scoring plays.
    Works mid-game for live box scores.
    """
    meta = find_team(team_name)
    if not meta:
        return f"I couldn't find a team named '{team_name}'."
    event, error = _find_game(meta, opponent, week)
    if error:
        return error
    summary = _get_game_summary(str(event.get("id")))
    if not summary:
        return "I couldn't load the box score for that game right now."

    comp = summary["header"]["competitions"][0]
    teams = comp.get("competitors", [])
    away = next((t for t in teams if t.get("homeAway") == "away"), teams[0])
    home = next((t for t in teams if t.get("homeAway") == "home"), teams[-1])
    status = comp.get("status", {}).get("type", {})
    detail = status.get("detail") or status.get("shortDetail", "")
    venue = ((summary.get("gameInfo") or {}).get("venue") or {}).get("fullName", "")
    week_no = (event.get("week") or {}).get("number")
    when = to_et(parse_iso_datetime(event.get("date")))
    abbr = lambda t: t.get("team", {}).get("abbreviation", "?")
    name = lambda t: t.get("team", {}).get("displayName", "?")

    live = " 🔴 LIVE" if status.get("state") == "in" else ""
    situation = summary.get("situation") or comp.get("situation")
    out = [f"📦 **Box Score{live}: {name(away)} {away.get('score', '?')} @ "
           f"{name(home)} {home.get('score', '?')}** ({detail})",
           " · ".join(x for x in (f"Week {week_no}" if week_no else "", when,
                                  f"at {venue}" if venue else "") if x)]
    if live and (now := _live_situation(situation, teams)):
        out.append(f"**Right now:** {now}")

    # Line score by quarter
    quarters = max(len(away.get("linescores", [])), len(home.get("linescores", [])))
    if quarters:
        labels = [f"Q{i}" if i <= 4 else f"OT{i - 4 if quarters > 5 else ''}"
                  for i in range(1, quarters + 1)]
        out += ["", "| Team | " + " | ".join(labels) + " | Total |",
                "|---|" + "---|" * (quarters + 1)]
        for t in (away, home):
            ls = [l.get("displayValue", "0") for l in t.get("linescores", [])]
            ls += ["-"] * (quarters - len(ls))
            out.append(f"| {abbr(t)} | " + " | ".join(ls) + f" | **{t.get('score', '?')}** |")

    # Team stat comparison (boxscore.teams is ordered away, home)
    box_teams = (summary.get("boxscore") or {}).get("teams", [])
    if len(box_teams) == 2:
        stats = [{s.get("label"): s.get("displayValue") for s in bt.get("statistics", [])}
                 for bt in box_teams]
        heads = [bt.get("team", {}).get("abbreviation", "?") for bt in box_teams]
        out += ["", f"| Team stats | {heads[0]} | {heads[1]} |", "|---|---|---|"]
        for label in _BOX_TEAM_STATS:
            if label in stats[0] or label in stats[1]:
                out.append(f"| {label} | {stats[0].get(label, '-')} | {stats[1].get(label, '-')} |")

    # Leaders per team
    leaders = summary.get("leaders", [])
    if leaders:
        out.append("\n**Leaders**")
        for team_leaders in leaders:
            team_abbr = (team_leaders.get("team") or {}).get("abbreviation", "?")
            parts = []
            for cat in team_leaders.get("leaders", []):
                top = (cat.get("leaders") or [{}])[0]
                athlete = (top.get("athlete") or {}).get("displayName")
                if athlete:
                    parts.append(f"{cat.get('displayName', '')}: {athlete} ({top.get('displayValue', '')})")
            if parts:
                out.append(f"- **{team_abbr}** — " + "; ".join(parts))

    # Scoring summary
    plays = summary.get("scoringPlays", [])
    if plays:
        out.append("\n**Scoring**")
        for sp in plays:
            q = (sp.get("period") or {}).get("number", "?")
            clock = (sp.get("clock") or {}).get("displayValue", "")
            team_abbr = (sp.get("team") or {}).get("abbreviation", "?")
            out.append(f"- Q{q} {clock} {team_abbr}: {sp.get('text', '')} "
                       f"({abbr(away)} {sp.get('awayScore')}-{sp.get('homeScore')} {abbr(home)})")
    return "\n".join(out)


# ----------------------------------------------------
# Team Rankings (offense / defense)
# ----------------------------------------------------
# ESPN's per-team statistics give a team's own season stats and its
# opponents' stats (= what the defense allowed). ESPN's own ranks are
# incomplete and their direction undocumented, so ranks are computed here
# across all 32 teams: rank 1 is always the best (most scored, least allowed).
_TEAM_STATS_TTL = 60 * 60 * 6
_TEAM_STATS_CACHE: Dict[str, Any] = {"at": 0.0, "data": None}
_TEAM_STATS_LOCK = threading.Lock()

# (key, label, side, ESPN stat name, higher_is_better, is_percent)
# side "own" = the team's stats, "opp" = its opponents' stats against it.
_TEAM_METRICS = (
    ("pts",        "Points per game",           "own", "totalPointsPerGame",     True,  False),
    ("yds",        "Yards per game",            "own", "yardsPerGame",           True,  False),
    ("pass",       "Passing yards per game",    "own", "netPassingYardsPerGame", True,  False),
    ("rush",       "Rushing yards per game",    "own", "rushingYardsPerGame",    True,  False),
    ("third",      "3rd down conversion %",     "own", "thirdDownConvPct",       True,  True),
    ("redzone",    "Red zone scoring %",        "own", "redzoneScoringPct",      True,  True),
    ("sacked",     "Sacks allowed",             "own", "sacks",                  False, False),
    ("giveaways",  "Giveaways",                 "own", "totalGiveaways",         False, False),
    ("pts_allowed",  "Points allowed per game",       "opp", "totalPointsPerGame",     False, False),
    ("yds_allowed",  "Yards allowed per game",        "opp", "yardsPerGame",           False, False),
    ("pass_allowed", "Passing yards allowed per game", "opp", "netPassingYardsPerGame", False, False),
    ("rush_allowed", "Rushing yards allowed per game", "opp", "rushingYardsPerGame",    False, False),
    ("third_allowed", "Opponent 3rd down %",          "opp", "thirdDownConvPct",       False, True),
    ("sacks",        "Sacks",                         "opp", "sacks",                  True,  False),
    ("takeaways",    "Takeaways",                     "own", "totalTakeaways",         True,  False),
    ("to_diff",      "Turnover differential",         "own", "turnOverDifferential",   True,  False),
)
_OFFENSE_KEYS = ("pts", "yds", "pass", "rush", "third", "redzone", "sacked", "giveaways")
_DEFENSE_KEYS = ("pts_allowed", "yds_allowed", "pass_allowed", "rush_allowed",
                 "third_allowed", "sacks", "takeaways")

# Leaderboard focus -> metric key ("best run defense" -> rush_allowed).
TEAM_RANK_FOCUS = {
    "offense": "pts", "scoring": "pts", "passing": "pass", "rushing": "rush",
    "defense": "pts_allowed", "total_defense": "yds_allowed",
    "pass_defense": "pass_allowed", "rush_defense": "rush_allowed",
    "sacks": "sacks", "turnovers": "to_diff", "third_down": "third", "red_zone": "redzone",
}


def _flatten_team_stats(categories: List[Dict[str, Any]]) -> Dict[str, float]:
    flat: Dict[str, float] = {}
    for cat in categories or []:
        for s in cat.get("stats", []):
            if s.get("name") not in flat and isinstance(s.get("value"), (int, float)):
                flat[s["name"]] = s["value"]
    return flat


def _fetch_team_stats(team: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    data = fetch_json(ENDPOINTS["team_stats"].format(team_id=team["id"]))
    results = data.get("results") if isinstance(data, dict) else None
    if not results:
        return None
    own = results.get("stats") or {}
    return {
        "name": team.get("displayName"),
        "abbr": (team.get("abbr") or "").upper(),
        "own": _flatten_team_stats(own.get("categories", []) if isinstance(own, dict) else own),
        "opp": _flatten_team_stats(results.get("opponent", [])),
    }


def _all_team_stats() -> Optional[Dict[str, Dict[str, Any]]]:
    """{TEAM_ABBR: {"name", "abbr", "values": {metric_key: value}, "ranks": {...}}}"""
    with _TEAM_STATS_LOCK:
        if _TEAM_STATS_CACHE["data"] and time.time() - _TEAM_STATS_CACHE["at"] < _TEAM_STATS_TTL:
            return _TEAM_STATS_CACHE["data"]
        teams = _load_static_data("teams.json")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            fetched = [t for t in pool.map(_fetch_team_stats, teams) if t]
        if len(fetched) < 28:  # too many failures for meaningful ranks
            logger.error("team stats: only %d/%d teams loaded", len(fetched), len(teams))
            return None

        table: Dict[str, Dict[str, Any]] = {}
        for t in fetched:
            values = {key: t[side].get(stat) for key, _, side, stat, _, _ in _TEAM_METRICS}
            table[t["abbr"]] = {"name": t["name"], "abbr": t["abbr"], "values": values,
                                "games": t["own"].get("gamesPlayed"), "ranks": {}}
        for key, _, _, _, higher, _ in _TEAM_METRICS:
            scored = [(abbr, row["values"][key]) for abbr, row in table.items()
                      if row["values"][key] is not None]
            for abbr, value in scored:
                better = sum(1 for _, v in scored if (v > value if higher else v < value))
                tied = sum(1 for _, v in scored if v == value) > 1
                table[abbr]["ranks"][key] = (better + 1, tied)
        _TEAM_STATS_CACHE.update(at=time.time(), data=table)
        return table


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _rank_str(rank: tuple) -> str:
    return ("T-" if rank[1] else "") + _ordinal(rank[0])


def _metric_value(key: str, value: Optional[float]) -> str:
    if value is None:
        return "-"
    pct = next(m[5] for m in _TEAM_METRICS if m[0] == key)
    if pct:
        return f"{value:.1f}%"
    if key == "to_diff":
        return f"{value:+.0f}"
    return _fmt_num(round(value, 1))


def get_team_rankings(team_name: Optional[str] = None, focus: Optional[str] = None) -> str:
    """
    With a team: its offense and defense stats with league ranks (1 = best of
    32), plus top-5 strengths and bottom-5 weaknesses. Without a team: the
    top 10 teams in `focus` (see TEAM_RANK_FOCUS; default offense+defense).
    """
    table = _all_team_stats()
    if not table:
        return "I couldn't load team stats right now — try again in a bit."
    labels = {m[0]: m[1] for m in _TEAM_METRICS}
    n = len(table)

    if team_name:
        meta = find_team(team_name)
        abbr = (meta or {}).get("abbr", "").upper()
        row = table.get(abbr)
        if not row:
            return f"I couldn't find team stats for '{team_name}'."
        games = f"{_fmt_num(row['games'])} games, " if row.get("games") else ""
        out = [f"📈 **{row['name']} — Team Rankings** ({games}ranks out of {n}, 1st = best)"]
        for title, keys in (("Offense", _OFFENSE_KEYS), ("Defense", _DEFENSE_KEYS)):
            out += ["", f"**{title}**", "| Stat | Value | Rank |", "|---|---|---|"]
            for key in keys:
                rank = row["ranks"].get(key)
                out.append(f"| {labels[key]} | {_metric_value(key, row['values'][key])} | "
                           f"{_rank_str(rank) if rank else '-'} |")
        to_rank = row["ranks"].get("to_diff")
        out.append(f"\nTurnover differential: **{_metric_value('to_diff', row['values']['to_diff'])}**"
                   + (f" ({_rank_str(to_rank)})" if to_rank else ""))
        strengths = [labels[k] for k, r in row["ranks"].items() if r[0] <= 5]
        weaknesses = [labels[k] for k, r in row["ranks"].items() if r[0] > n - 5]
        if strengths:
            out.append(f"Strengths (top 5): {', '.join(strengths)}")
        if weaknesses:
            out.append(f"Weaknesses (bottom 5): {', '.join(weaknesses)}")
        return "\n".join(out)

    keys = [TEAM_RANK_FOCUS[focus]] if focus in TEAM_RANK_FOCUS else ["pts", "pts_allowed"]
    out = []
    for key in keys:
        ranked = sorted((r for r in table.values() if key in r["ranks"]),
                        key=lambda r: r["ranks"][key][0])
        out += [f"📈 **Best in the NFL — {labels[key]}**"]
        for r in ranked[:10]:
            out.append(f"- **{_rank_str(r['ranks'][key])}:** {r['name']} — {_metric_value(key, r['values'][key])}")
        out.append("")
    return "\n".join(out).rstrip()


# ----------------------------------------------------
# Postseason results (past playoffs / Super Bowls)
# ----------------------------------------------------
# ESPN postseason weeks: 1 Wild Card, 2 Divisional, 3 Conference
# Championships, 4 Pro Bowl (skipped), 5 Super Bowl. Past seasons never
# change, so results are cached for the life of the process.
_POSTSEASON_ROUNDS = ((1, "Wild Card"), (2, "Divisional Round"),
                      (3, "Conference Championships"), (5, "Super Bowl"))
_POSTSEASON_CACHE: Dict[int, List[tuple]] = {}


def _postseason_games(season: int, week: int) -> Optional[List[Dict[str, Any]]]:
    data = fetch_json(ENDPOINTS["scoreboard"],
                      params={"seasontype": 3, "week": week, "dates": season})
    if "__error" in data:
        return None
    games = []
    for ev in data.get("events", []):
        comp = ev.get("competitions", [{}])[0]
        if comp.get("status", {}).get("type", {}).get("state") != "post":
            continue
        teams = comp.get("competitors", [])
        winner = next((t for t in teams if t.get("winner")), None)
        loser = next((t for t in teams if t is not winner), None)
        if not winner or not loser:
            continue
        note = next((n.get("headline") for n in comp.get("notes", []) if n.get("headline")), "")
        games.append({
            "winner": winner["team"]["displayName"], "w_score": winner.get("score"),
            "loser": loser["team"]["displayName"], "l_score": loser.get("score"),
            "note": note, "venue": (comp.get("venue") or {}).get("fullName", ""),
            "date": ev.get("date", "")[:10],
        })
    return games


def get_postseason_results(season: Optional[int] = None, super_bowl_only: bool = False) -> str:
    """
    Playoff results for a season (the season a Super Bowl concludes, e.g. the
    2025 season's Super Bowl LX was in Feb 2026). Default: the most recent
    completed postseason.
    """
    if season is None:
        season = _current_nfl_season_year()
        # The current season's playoffs haven't happened until February.
        if datetime.date.today() < datetime.date(season + 1, 2, 15):
            season -= 1

    if season not in _POSTSEASON_CACHE:
        rounds = []
        for week, label in _POSTSEASON_ROUNDS:
            games = _postseason_games(season, week)
            if games is None:
                return "I'm having trouble reaching past playoff results right now."
            if games:
                rounds.append((label, games))
        if not rounds:
            return f"I don't have playoff results for the {season} season."
        if any(label == "Super Bowl" for label, _ in rounds):  # season complete: cache
            _POSTSEASON_CACHE[season] = rounds
    else:
        rounds = _POSTSEASON_CACHE[season]

    out = [f"🏆 **{season} NFL Playoffs** (played {season}-{season + 1} season)"]
    for label, games in rounds:
        if super_bowl_only and label != "Super Bowl":
            continue
        out.append(f"\n**{label}**")
        for g in games:
            title = f"{g['note']}: " if label == "Super Bowl" and g["note"] else ""
            where = f" at {g['venue']}" if label == "Super Bowl" and g["venue"] else ""
            out.append(f"- {title}**{g['winner']} {g['w_score']}**, {g['loser']} {g['l_score']}"
                       f"{where} ({g['date']})")
    return "\n".join(out)


def get_draft_context(limit: int = 80) -> str:
    """
    Guard data for draft-prospect questions. There is no free prospect feed,
    so the formatter answers from its own knowledge — which lags reality (it
    listed Caleb Downs as a 2027 prospect after Dallas drafted him in 2026).
    This lists the latest draft class already in the NFL, plus any curated
    college prospects, so those players are never presented as upcoming.
    """
    _ensure_player_cache()
    season = _current_nfl_season_year()
    rookies = sorted(
        (p for p in _PLAYER_CACHE.values()
         if p.get("active") and p.get("years_exp") == 0 and p.get("full_name")
         and (p.get("search_rank") or 9_999_999) < 9_999_999),
        key=lambda p: p.get("search_rank"),
    )[:limit]
    out = [f"ALREADY DRAFTED — the {season} NFL rookie class (now NFL players; never "
           f"list them as upcoming draft prospects):"]
    out += [f"- {p['full_name']} ({p.get('position', '?')}, {p.get('team') or 'FA'}"
            + (f", from {p['college']}" if p.get("college") else "") + ")" for p in rookies]
    curated = [f"- {p['name']} ({p.get('pos', '?')}, {p.get('school', '?')})" for p in _PROSPECTS.values()]
    if curated:
        out += ["", "Known college prospects (curated list, may be incomplete):"] + curated
    return "\n".join(out)


# Stat keys a fan can ask leaders for, with display labels.
LEADER_STATS = {
    "pass_yd": "passing yards", "pass_td": "passing TDs", "pass_int": "interceptions thrown",
    "rush_yd": "rushing yards", "rush_td": "rushing TDs",
    "rec": "receptions", "rec_yd": "receiving yards", "rec_td": "receiving TDs",
    "pts_ppr": "PPR fantasy points", "pts_half_ppr": "half-PPR fantasy points",
    "pts_std": "standard fantasy points",
}


def get_league_leaders(stat: Optional[str] = "pts_ppr", position: Optional[str] = None,
                       top_n: int = 10, rookies_only: bool = False) -> str:
    """
    Season leaders for a stat ("passing yards leaders"), optionally by
    position ("top 5 fantasy QBs" = pts_ppr, QB). Uses the cached Sleeper
    season stats, so it costs no extra requests after the first.
    """
    stat = stat if stat in LEADER_STATS else "pts_ppr"
    pos = position.upper() if position and position.upper() in POSITIONS else None
    year = _current_nfl_season_year()
    stats = _get_stats(year)
    if stats is None:
        return "I couldn't reach the stats service for league leaders right now."
    _ensure_player_cache()

    rows = []
    for pid, s in stats.items():
        value = s.get(stat)
        p = _PLAYER_CACHE.get(pid)
        if not value or not p or not p.get("full_name"):
            continue
        if pos and p.get("position") != pos:
            continue
        if rookies_only and p.get("years_exp") != 0:
            continue
        rows.append((value, p, s.get("gp")))
    if not rows:
        return f"No {LEADER_STATS[stat]} recorded yet this season."

    rows.sort(key=lambda r: r[0], reverse=True)
    label = LEADER_STATS[stat]
    title = ("Rookie " if rookies_only else "") + (f"{pos} " if pos else "")
    out = [f"🏆 **{year} Leaders — {title}{label}** (through Week {max(1, _current_nfl_week() - 1)})"]
    for rank, (value, p, gp) in enumerate(rows[:max(1, min(top_n, 25))], 1):
        games = f", {_fmt_num(gp)} GP" if gp else ""
        out.append(f"{rank}. **{p['full_name']}** ({p.get('position', '?')}, "
                   f"{p.get('team') or 'FA'}) — {_fmt_num(value)}{games}")
    return "\n".join(out)


# Public names for the season/week helpers (used by the chatbot's prompts).
def current_nfl_season_year() -> int:
    return _current_nfl_season_year()


def current_nfl_week() -> int:
    return _current_nfl_week()


def get_player_team(player_name: str) -> Optional[str]:
    """Team abbreviation of the best-matching active player, e.g. 'KC'."""
    resolved = _resolve_player(player_name)
    return resolved[1].get("team") if resolved else None


def get_team_roster(team_name: str, position: Optional[str] = None) -> str:
    """
    Returns the current depth chart for a team from the weekly-refreshed
    data/rosters.json file.  Falls back to the Sleeper live cache when
    rosters.json hasn't been generated yet.

    Args:
        team_name: Full team name, abbreviation, or nickname.
        position:  Optional filter — "QB", "WR", etc.
    """
    # Resolve the team abbreviation via the team cache
    meta = find_team(team_name)
    if not meta:
        return f"I couldn't find a team named '{team_name}'."

    abbr = sleeper_team_abbr(team_name) or ""
    display = meta.get("displayName", team_name)

    rosters = _ROSTERS_DATA.get("rosters", {})
    players  = rosters.get(abbr, [])

    # If rosters.json hasn't been populated yet, fall back to the live Sleeper cache
    if not players:
        _ensure_player_cache()
        players = [
            p for p in _PLAYER_CACHE.values()
            if (p.get("team") or "").upper() == abbr and p.get("active")
        ]
        if not players:
            updated = _ROSTERS_DATA.get("_meta", {}).get("updated_at", "never")
            return (
                f"I don't have a current roster for the {display} yet. "
                f"(Last data refresh: {updated}). "
                f"Run `python scripts/update_data.py` to fetch the latest rosters."
            )

    # Apply position filter
    pos_filter = position.upper().strip() if position else None
    if pos_filter:
        players = [p for p in players if p.get("position") == pos_filter]
        if not players:
            return f"No {pos_filter}s found on the {display} roster right now."

    # Group by position for a readable depth chart
    POS_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4,
                 "DE": 5, "DT": 6, "LB": 7, "CB": 8, "S": 9}
    groups: Dict[str, List] = {}
    for p in players:
        pos = p.get("position", "?")
        groups.setdefault(pos, []).append(p)

    # Sort each position group by depth order
    for pos in groups:
        groups[pos].sort(
            key=lambda x: x.get("depth_chart_order") if x.get("depth_chart_order") is not None else 99
        )

    updated = _ROSTERS_DATA.get("_meta", {}).get("updated_at", "unknown")
    lines = [f"📋 **{display} Roster**  *(last refreshed: {updated[:10]})*\n"]

    pos_labels = list(sorted(groups.keys(), key=lambda p: POS_ORDER.get(p, 99)))
    for pos in pos_labels:
        group = groups[pos]
        lines.append(f"**{pos}**")
        for i, p in enumerate(group, 1):
            name   = p.get("full_name", "Unknown")
            inj    = p.get("injury_status")
            inj_str = f" ⚠️ {inj}" if inj else ""
            depth_label = {1: "Starter", 2: "Backup", 3: "3rd"}.get(i, f"#{i}")
            lines.append(f"  {i}. {name} ({depth_label}){inj_str}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _ensure_player_cache():
    global _PLAYER_CACHE, _PLAYER_CACHE_LAST
    if _PLAYER_CACHE and (time.time() - _PLAYER_CACHE_LAST) < INJURY_CACHE_TTL:
        return

    with _PLAYER_CACHE_LOCK:
        # Re-check — another thread may have refreshed it while we waited.
        # This matters a lot here: the Sleeper player dump is several MB,
        # and _dispatch() can trigger this from 2-3 threads on a single
        # multi-intent query (e.g. "compare X vs Y" fans out per player).
        if _PLAYER_CACHE and (time.time() - _PLAYER_CACHE_LAST) < INJURY_CACHE_TTL:
            return
        data = fetch_json(ENDPOINTS["sleeper_players"])
        if "__error" not in data:
            _PLAYER_CACHE = data
            _PLAYER_CACHE_LAST = time.time()
        else:
            logger.error(
                f"component=api_client action=_ensure_player_cache "
                f"endpoint={ENDPOINTS['sleeper_players']} error={data['__error']}"
            )


def _find_players(name: str, team: Optional[str] = None,
                  active_only: bool = True) -> List[tuple]:
    """
    Returns (player_id, record) candidates for a name, best first:
    exact name matches before fuzzy ones, then Sleeper's search_rank
    (lower = more prominent), so "Justin Jefferson" resolves to the
    Vikings WR rather than whichever same-name player the dict yields first.
    `team` (any team name/abbreviation) restricts results to that team.
    """
    _ensure_player_cache()
    q = clean_query(name)
    matches = [(pid, p) for pid, p in _PLAYER_CACHE.items()
               if p.get("full_name") and is_fuzzy_match(q, p["full_name"])
               and (p.get("active") or not active_only)]

    team_abbr = sleeper_team_abbr(team)
    if team_abbr:
        on_team = [m for m in matches if (m[1].get("team") or "").upper() == team_abbr]
        matches = on_team or matches

    def rank(m):
        exact = clean_query(m[1]["full_name"]) == q
        return (not exact, m[1].get("search_rank") or 9_999_999)
    return sorted(matches, key=rank)


def _resolve_player(name: str, team: Optional[str] = None) -> Optional[tuple]:
    """Best (player_id, record) match for a name, or None."""
    matches = _find_players(name, team)
    return matches[0] if matches else None


# -------------------------
# Stats Cache
# -------------------------
# Every weekly/season stats request returns the whole league (~0.6 MB), and
# a single player question used to fetch up to 18 of them uncached. Keep
# only the fields the app reads; finished weeks rarely change (stat
# corrections land within a day or two), the in-progress week changes live.
_STAT_FIELDS = ("pts_ppr", "pts_half_ppr", "pts_std", "gp", "pass_yd", "pass_td",
                "pass_int", "rush_yd", "rush_td", "rec", "rec_yd", "rec_td")
_FINAL_STATS_TTL = 60 * 60 * 24
_LIVE_STATS_TTL = 60 * 15
_STATS_CACHE: Dict[tuple, tuple] = {}   # key -> (fetched_at, trimmed data)
_STATS_CACHE_LOCK = threading.Lock()


def _get_stats(year: int, week: Optional[int] = None) -> Optional[Dict[str, Dict[str, float]]]:
    """
    League-wide stats for a season (week=None) or a single week, trimmed to
    _STAT_FIELDS and cached. Returns None when the fetch fails (not cached).
    """
    key = (year, week)
    final = year < _current_nfl_season_year() or (week is not None and week < _current_nfl_week())
    ttl = _FINAL_STATS_TTL if final else _LIVE_STATS_TTL
    with _STATS_CACHE_LOCK:
        hit = _STATS_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]

    url = (ENDPOINTS["sleeper_stats"].format(year=year) if week is None
           else ENDPOINTS["sleeper_stats_week"].format(year=year, week=week))
    data = fetch_json(url)
    if not isinstance(data, dict) or "__error" in data:
        return None
    trimmed = {
        pid: {f: s[f] for f in _STAT_FIELDS if f in s}
        for pid, s in data.items() if isinstance(s, dict)
    }
    with _STATS_CACHE_LOCK:
        _STATS_CACHE[key] = (time.time(), trimmed)
    return trimmed


def get_player_profile_smart(user_input: str, team: Optional[str] = None) -> Union[str, Dict[str, Any]]:
    """
    Looks up a legend, prospect, or active player by name.
    `team` (any team name/abbreviation) narrows same-name active players,
    e.g. the Vikings WR vs the Browns LB named Justin Jefferson.
    """
    _ensure_player_cache()
    q = user_input.lower().strip()

    # ---------------------------------------------------------
    # LAYER 1: Retired Legends (History & Awards)
    # Loaded from data/legends.json — add entries there to expand coverage.
    # Skip this layer for active players so they get live stats + depth chart.
    # ---------------------------------------------------------
    if q in _LEGENDS:
        l = _LEGENDS[q]
        # If the player is still active, fall through to the live Sleeper lookup
        # so the user gets current stats, injury status, and depth chart.
        if not l.get("status", "").startswith("Active"):
            return (f"### 🏛️ Legend: {l['name']}\n"
                    f"- **Status:** {l['status']}\n"
                    f"- **Teams:** {l['teams']}\n"
                    f"- **Career Stats:** {l['stats']}\n"
                    f"- **Awards:** {l['awards']}")

    # ---------------------------------------------------------
    # LAYER 2: College Prospects (Stats & Draft)
    # Loaded from data/prospects.json — add entries there to expand coverage
    # ---------------------------------------------------------
    # Only for players not in the NFL yet — a drafted prospect (e.g. Travis
    # Hunter, now a Jaguar) must get his NFL profile, not his college card.
    if q in _PROSPECTS and not _find_players(q):
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

    # Narrow by team — explicit argument first, else a team named in the query.
    # Compare Sleeper abbreviations exactly; substring checks let free agents
    # (team == "") match every hint.
    team_abbr = sleeper_team_abbr(team or detect_team_from_query(q))
    if team_abbr:
        hinted = [p for p in matches if (p.get("team") or "").upper() == team_abbr]
        if hinted:
            matches = hinted

    if len(matches) == 1:
        p = matches[0]
        live_stats = get_fantasy_player_stats(p["full_name"], team=p.get("team"))
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

def get_fantasy_player_stats(query_name: str, team: Optional[str] = None) -> str:
    """Retrieves PPR fantasy points for a player using the correct NFL season year."""
    resolved = _resolve_player(query_name, team)
    if not resolved:
        return f"I'm not seeing any fantasy points recorded for {query_name} yet."
    pid, p = resolved

    stats = _get_stats(_current_nfl_season_year())
    if stats is None:
        return f"I couldn't reach the fantasy stats service for {p['full_name']} right now."

    pts = stats.get(pid, {}).get("pts_ppr", 0)
    return (f"I took a look at the latest fantasy data—{p['full_name']} "
            f"({p.get('position')}, {p.get('team') or 'FA'}): **{pts} PPR Points**!")


# ----------------------------------------------------
# Improvement #2 — Injury Reports
# ----------------------------------------------------

def get_player_injury(player_name: str, team: Optional[str] = None) -> str:
    """
    Returns injury status, body part, practice participation, and notes
    directly from the Sleeper player cache — no extra API call needed.
    """
    resolved = _resolve_player(player_name, team)
    if not resolved:
        return f"I couldn't find injury information for '{player_name}'."

    _, p = resolved
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

def get_player_weekly_stats(player_name: str, num_weeks: int = 5,
                            team: Optional[str] = None) -> str:
    """
    Returns the last N weeks of game stats for a player from Sleeper.
    Surfaces passing, rushing, and receiving lines depending on position.
    """
    year = _current_nfl_season_year()
    resolved = _resolve_player(player_name, team)
    if not resolved:
        return f"No weekly stats found for '{player_name}'."

    pid, player = resolved
    pos    = player.get("position", "")
    name   = player.get("full_name", player_name)

    # Fetch the last num_weeks weeks concurrently
    def _fetch_week(week: int):
        return week, (_get_stats(year, week) or {}).get(pid, {})

    # Current week from Sleeper (flips to the next week on Tuesday)
    current_week = _current_nfl_week()
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
                f"Pass: {_fmt_num(s.get('pass_yd', 0))} yds / {_fmt_num(s.get('pass_td', 0))} TD / "
                f"{_fmt_num(s.get('pass_int', 0))} INT | "
                f"Rush: {_fmt_num(s.get('rush_yd', 0))} yds | "
                f"**{pts} pts**"
            )
        elif pos in ("RB",):
            stat_line = (
                f"Rush: {_fmt_num(s.get('rush_yd', 0))} yds / {_fmt_num(s.get('rush_td', 0))} TD | "
                f"Rec: {_fmt_num(s.get('rec', 0))} / {_fmt_num(s.get('rec_yd', 0))} yds | "
                f"**{pts} pts**"
            )
        elif pos in ("WR", "TE"):
            stat_line = (
                f"Rec: {_fmt_num(s.get('rec', 0))} / {_fmt_num(s.get('rec_yd', 0))} yds / "
                f"{_fmt_num(s.get('rec_td', 0))} TD | "
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
    resolved = _resolve_player(player_name)
    if not resolved:
        return f"I couldn't find fantasy data for '{player_name}'."

    _, player = resolved
    name   = player.get("full_name", player_name)
    team   = player.get("team") or "FA"
    pos    = player.get("position", "?")

    # Gather components — pass the team so each lookup stays on this player
    weekly  = get_player_weekly_stats(name, num_weeks=4, team=player.get("team"))
    injury  = get_player_injury(name, team=player.get("team"))
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
    if "__error" in data:
        return "I'm having trouble reaching the scoreboard for betting lines right now. Try again in a moment."
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
    resolved = _resolve_player(name)
    if not resolved:
        return f"No data found for '{name}'."

    _, p = resolved
    full   = p.get("full_name", name)
    team   = p.get("team") or "FA"
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

    weekly = get_player_weekly_stats(full, num_weeks=4, team=p.get("team"))
    season = get_fantasy_player_stats(full, team=p.get("team"))

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
    def _next_game_for(name: str) -> str:
        resolved = _resolve_player(name)
        if resolved and resolved[1].get("team"):
            return get_next_game(resolved[1]["team"])
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
    Returns the most-added players across Sleeper fantasy leagues (the
    standard waiver-wire signal) with recent weekly PPR, injury status,
    and upcoming matchup for Gemini to analyse.

    Candidates come from Sleeper's trending-adds feed, ranked by add volume
    over the last 48 hours. (Unsigned NFL free agents can't be used — they
    score no fantasy points, and there's no league to check rosters against.)

    Args:
        position: Optional filter — "QB", "RB", "WR", or "TE".
        top_n:    Number of candidates to return (default 5).
    """
    _ensure_player_cache()
    year = _current_nfl_season_year()

    pos_filter = position.upper().strip() if position else None
    if pos_filter and pos_filter not in _WAIVER_POSITIONS:
        return f"'{position}' isn't a recognised fantasy position. Try QB, RB, WR, or TE."

    # ── Step 1: trending adds across Sleeper leagues ──────────────
    trending = fetch_json(ENDPOINTS["sleeper_trending_add"],
                          params={"lookback_hours": 48, "limit": 100})
    if not isinstance(trending, list):
        return "I couldn't reach the waiver-wire trends right now — try again in a bit."

    candidates = []
    for item in trending:
        pid = str(item.get("player_id", ""))
        p = _PLAYER_CACHE.get(pid)
        if (p and p.get("active") and p.get("full_name")
                and p.get("position") in _WAIVER_POSITIONS
                and (pos_filter is None or p.get("position") == pos_filter)):
            candidates.append((pid, p, item.get("count", 0)))

    if not candidates:
        label = f"{pos_filter} " if pos_filter else ""
        return f"No {label}players are trending on waivers right now."
    top = candidates[:top_n]  # feed is already sorted by add count

    # ── Step 2: fetch last 3 weeks concurrently ───────────────────
    current_week = _current_nfl_week()
    recent_weeks = list(range(max(1, current_week - 3), current_week + 1))

    def _fetch_week_data(week: int) -> tuple[int, dict]:
        return week, _get_stats(year, week) or {}

    week_data: dict[int, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        for week, data in pool.map(_fetch_week_data, recent_weeks):
            week_data[week] = data

    # ── Step 3: next game for each candidate (schedule context) ──
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
        schedules = list(pool.map(lambda item: _next_game_for_player(item[1]), top))

    # ── Step 4: build output block ────────────────────────────────
    label = f"{pos_filter} " if pos_filter else ""
    lines = [f"🏆 **Top {len(top)} {label}Waiver Wire Targets "
             f"(most added across Sleeper leagues, last 48 hrs)**\n"]

    for rank, ((pid, p, adds), schedule) in enumerate(zip(top, schedules), 1):
        name     = p.get("full_name")
        pos      = p.get("position", "?")
        team     = p.get("team") or "FA"
        inj      = p.get("injury_status") or "Healthy"
        inj_note = f" ⚠️ {inj}" if inj != "Healthy" else ""

        # Only weeks with data, so labels stay aligned with their points.
        recent = [(w, week_data[w].get(pid, {}).get("pts_ppr", 0))
                  for w in recent_weeks if week_data.get(w)]
        recent_str = " | ".join(f"Wk {w}: {pt:.1f}" for w, pt in recent) or "no games yet"
        total = sum(pt for _, pt in recent)

        lines.append(
            f"**{rank}. {name}** ({pos}, {team}){inj_note} — added in {adds:,} leagues\n"
            f"   Recent: {recent_str} → **{total:.1f} pts last {len(recent)} wks**\n"
            f"   Next: {schedule}"
        )

    return "\n".join(lines)


# ----------------------------------------------------
# Weekly PPR Chart Data (sparkline support)
# ----------------------------------------------------

def get_player_chart_data(player_name: str, team: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Returns {"weeks": [...], "pts": [...]} for a weekly PPR sparkline chart,
    or None if fewer than 4 weeks of data are available.

    Weeks come from the stats cache, so after the first request only the
    in-progress week is re-fetched.
    """
    resolved = _resolve_player(player_name, team)
    if not resolved:
        return None

    pid = resolved[0]
    year = _current_nfl_season_year()

    current_week = _current_nfl_week()

    def _week_pts(week: int):
        return week, (_get_stats(year, week) or {}).get(pid, {}).get("pts_ppr")

    week_pts: Dict[int, float] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        for week, pts in pool.map(_week_pts, range(1, current_week + 1)):
            if pts is not None:
                week_pts[week] = round(pts, 1)

    if len(week_pts) < 4:
        return None

    sorted_weeks = sorted(week_pts.keys())
    return {
        "name": resolved[1].get("full_name", player_name),
        "weeks": [f"Wk {w}" for w in sorted_weeks],
        "pts": [week_pts[w] for w in sorted_weeks],
    }


# ----------------------------------------------------
# Task 15 — Historical Game Log
# ----------------------------------------------------

def get_player_history(
    player_name: str,
    opponent_team: Optional[str] = None,
    season_year: Optional[int] = None,
) -> str:
    """
    Returns a full-season game log for a player from Sleeper weekly stats.

    Fetches all 18 weeks for the given season (defaults to the current NFL
    season year), filters for the player's stats, and formats a per-week
    breakdown with position-appropriate stat lines.

    Args:
        player_name:   Full or partial player name (fuzzy matched).
        opponent_team: Optional team name to filter to games vs that team only.
        season_year:   4-digit season year (e.g. 2024). Defaults to current season.
    """
    year = season_year or _current_nfl_season_year()
    resolved = _resolve_player(player_name)
    if not resolved:
        return f"No player found matching '{player_name}'."

    pid, player = resolved
    name   = player.get("full_name", player_name)
    pos    = player.get("position", "")

    # Fetch the season's weeks concurrently — only weeks played so far
    # for the current season, all 18 for past seasons.
    last_week = _current_nfl_week() if year == _current_nfl_season_year() else 18

    def _fetch_week(week: int):
        return week, (_get_stats(year, week) or {}).get(pid, {})

    week_data: Dict[int, Dict] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        for week, stats in pool.map(_fetch_week, range(1, last_week + 1)):
            if stats:
                week_data[week] = stats

    if not week_data:
        return f"No stats found for {name} in the {year} season."

    # Build per-week stat lines
    lines = [f"📋 **{name} — {year} Season Game Log**\n"]
    total_pts = 0.0
    weeks_played = 0

    for week in sorted(week_data.keys()):
        s   = week_data[week]
        pts = round(s.get("pts_ppr", 0), 1)
        total_pts  += pts
        weeks_played += 1

        if pos == "QB":
            stat_line = (
                f"Pass: {_fmt_num(s.get('pass_yd', 0))} yds / {_fmt_num(s.get('pass_td', 0))} TD / "
                f"{_fmt_num(s.get('pass_int', 0))} INT | "
                f"Rush: {_fmt_num(s.get('rush_yd', 0))} yds | "
                f"**{pts} pts**"
            )
        elif pos == "RB":
            stat_line = (
                f"Rush: {_fmt_num(s.get('rush_yd', 0))} yds / {_fmt_num(s.get('rush_td', 0))} TD | "
                f"Rec: {_fmt_num(s.get('rec', 0))} / {_fmt_num(s.get('rec_yd', 0))} yds | "
                f"**{pts} pts**"
            )
        elif pos in ("WR", "TE"):
            stat_line = (
                f"Rec: {_fmt_num(s.get('rec', 0))} / {_fmt_num(s.get('rec_yd', 0))} yds / "
                f"{_fmt_num(s.get('rec_td', 0))} TD | "
                f"**{pts} pts**"
            )
        else:
            stat_line = f"**{pts} PPR pts**"

        lines.append(f"- **Wk {week}:** {stat_line}")

    if weeks_played:
        avg = round(total_pts / weeks_played, 1)
        lines.append(f"\n**Season total:** {round(total_pts, 1)} pts over {weeks_played} weeks "
                     f"(**{avg} avg/game**)")

    if opponent_team:
        lines.append(f"\n_(Showing full season log — opponent filter '{opponent_team}' "
                     f"not yet supported at the weekly stat level)_")

    return "\n".join(lines)
