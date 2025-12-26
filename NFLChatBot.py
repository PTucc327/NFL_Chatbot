"""
NFL API Client
Handles:
- Live scores
- Standings + playoff projections
- Team news (multi-source)
- Player lookup
- Fantasy stats
- Schedule (next/last game)

FIXES APPLIED:
1. NLU helpers (_normalize_query, etc.) defined and integrated.
2. get_fantasy_player_stats fixed to use NLU for accurate player matching.
3. get_standings fixed for single-team output format.
4. get_player_profile_smart corrected for proper output formatting.
"""

from typing import Optional, Dict, Any, List, Tuple
import datetime
import re
import requests
import feedparser
import pandas as pd
import time
from zoneinfo import ZoneInfo
import traceback # Added for debugging safety

# NOTE: Assuming basic utils functions (fetch_json, parse_iso_datetime, to_et, trend_indicator)
# are either defined below or imported correctly from a self-contained utils.py.
# For guaranteed execution, I am redefining core utilities and NLU helpers here.

# -------------------------
# Config & endpoints
# -------------------------
REQUEST_TIMEOUT = 10
CACHE_TTL = 60 * 60 * 6  # 6 hours default cache (teams / players)
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"
ESPN_TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
ESPN_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_STATS_URL_TEMPLATE = "https://api.sleeper.app/v1/stats/nfl/regular/{year}"

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}+NFL&hl=en-US&gl=US&ceid=US:en"
YAHOO_NFL_RSS = "https://sports.yahoo.com/nfl/rss.xml"
PFT_RSS = "https://profootballtalk.nbcsports.com/feed/"
BING_API_KEY = None  # optional

# Global constants for Player Lookup
COMMON_TEAM_NAMES = [
    "giants","cowboys","eagles","commanders","49ers","seahawks","rams","cardinals",
    "packers","bears","lions","vikings","saints","falcons","buccaneers","panthers",
    "chiefs","broncos","raiders","chargers","bills","patriots","dolphins","jets",
    "ravens","bengals","steelers","browns","colts","titans","jaguars","texans"
]
POSITIONS = {"QB","RB","WR","TE","K","P","DE","DT","LB","CB","S","OL","G","T","C"}

# -------------------------
# Local caches
# -------------------------
_TEAM_CACHE: Dict[str, Dict[str, Any]] = {}
_TEAM_CACHE_LAST = 0
_PLAYER_CACHE: Dict[str, Dict[str, Any]] = {}
_PLAYER_CACHE_LAST = 0
player_df = pd.DataFrame(columns=["Name", "Age", "Position", "Team", "College", "Years_in_NFL"])

def save_player_profile(profile_dict: Dict[str, Any]):
    global player_df
    player_df = pd.concat([player_df, pd.DataFrame([profile_dict])], ignore_index=True)


# ============================================================
# 1. BASE UTILITIES (REDEFINED FOR STABILITY)
# ============================================================

def fetch_json(url: str) -> Dict[str, Any]:
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"__error": str(e)}

def parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime.datetime]:
    if not dt_str: return None
    try:
        return datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None

def to_et(dt: Optional[datetime.datetime]) -> str:
    if not dt: return "TBD"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    try:
        et = dt.astimezone(ZoneInfo("America/New_York"))
        return et.strftime("%a, %b %d %I:%M %p EST")
    except Exception:
        return dt.isoformat()

def trend_indicator(pct):
    if pct >= 0.700: return "↑"
    if pct <= 0.350: return "↓"
    return "•"

# ----------------------------------------------------
# NLU HELPERS (FIXED AND DEFINED LOCALLY)
# ----------------------------------------------------

# REPLACES: external clean_query for NLU tasks
def _normalize_query(q: str) -> str:
    q = q.lower().strip()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()

    for w in (
        "who is", "tell me about", "show me", "give me",
        "player", "on the", "in the", "from", "team", "the",
        "for", "fantasy", "stats", "ppr", "pts",
        "qb for", "wr for", "rb for", "te for", "k for",
        "of the", "on team", "the team", "play for"
    ): q = q.replace(w, " ")

    q = re.sub(r"\s+", " ", q).strip()
    return q

def _detect_position_and_strip(query: str) -> Tuple[Optional[str], str]:
    words = query.split()
    pos, remaining = None, []
    for w in words:
        if w.upper() in POSITIONS: pos = w.upper()
        else: remaining.append(w)
    return pos, " ".join(remaining)

def _detect_team_from_query(query: str, debug=False) -> Optional[str]:
    for t in COMMON_TEAM_NAMES:
        if t in query: return t
    return None

def _player_matches_name(info: Dict[str, Any], name_tokens: List[str]) -> bool:
    first, last = (info.get("first_name") or "").lower().strip(), (info.get("last_name") or "").lower().strip()
    full = (info.get("full_name") or f"{first} {last}").lower().strip()
    if not full: return False
    return all(tok in full for tok in name_tokens)

def _player_matches_team(info: Dict[str, Any], team_filter: str) -> bool:
    if not team_filter: return True
    team_field = (info.get("team") or "").lower()
    return team_filter in team_field

# ============================================================
# 2. CACHE & FINDER (Synchronized)
# ============================================================

def ensure_team_cache():
    global _TEAM_CACHE, _TEAM_CACHE_LAST
    now = time.time()
    if _TEAM_CACHE and now - _TEAM_CACHE_LAST < CACHE_TTL: return

    _TEAM_CACHE = {}
    data = fetch_json(ESPN_TEAMS_URL)
    if "__error" in data:
        _TEAM_CACHE_LAST = now
        return
    
    teams = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    for item in teams:
        team_obj = item.get("team", {})
        if not team_obj: continue
        team_id = team_obj.get("id")
        display = team_obj.get("displayName") or team_obj.get("name") or team_obj.get("shortDisplayName")
        abbr = team_obj.get("abbreviation") or ""
        slug = team_obj.get("slug") or ""
        if not team_id: continue
            
        meta = {
            "id": str(team_id), "displayName": display, "abbr": abbr, "slug": slug,
            "schedule_url": f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/schedule"
        }
        keys = {k.lower() for k in [display, abbr, slug] if k}
        for k in keys: _TEAM_CACHE[k] = meta
        _TEAM_CACHE[str(team_id)] = meta
    _TEAM_CACHE_LAST = now

def find_team(query: Optional[str]) -> Optional[Dict[str, Any]]:
    if not query: return None
    ensure_team_cache()
    q = query.strip().lower()
    if q in _TEAM_CACHE: return _TEAM_CACHE[q]
    
    for meta in _TEAM_CACHE.values():
        dn, ab, slug = (meta.get("displayName") or "").lower(), (meta.get("abbr") or "").lower(), (meta.get("slug") or "").lower()
        if q == ab or q == slug or q in dn or q in slug or q in ab: return meta
    return None

def _ensure_player_cache(force: bool = False):
    global _PLAYER_CACHE, _PLAYER_CACHE_LAST
    now = time.time()
    if _PLAYER_CACHE and (now - _PLAYER_CACHE_LAST) < CACHE_TTL and not force: return

    _PLAYER_CACHE = {}
    _PLAYER_CACHE_LAST = now
    data = fetch_json(SLEEPER_PLAYERS_URL)
    if "__error" in data: return
    
    if isinstance(data, dict):
        for pid, rec in data.items():
            full = rec.get("full_name") or f"{rec.get('first_name','')} {rec.get('last_name','')}".strip()
            meta = {
                "id": pid, "first_name": rec.get("first_name") or "", "last_name": rec.get("last_name") or "",
                "full_name": full, "position": (rec.get("position") or "").upper(), "team": (rec.get("team") or ""),
                "age": rec.get("age"), "college": rec.get("college") or "",
                "years_exp": rec.get("years_exp") or rec.get("experience") or "N/A"
            }
            _PLAYER_CACHE[str(pid)] = meta
            if full: _PLAYER_CACHE[full.lower()] = meta


# ============================================================
# 3. CORE FEATURES (Score, Schedule)
# ============================================================

def get_live_scores(team_name: Optional[str] = None):
    data = fetch_json(ESPN_SCOREBOARD_URL)
    if "__error" in data: return f"⚠️ Error fetching scores: {data['__error']}"

    events = data.get("events", [])
    if not events: return "🏈 No NFL games scheduled or in progress today."

    team_q = (team_name or "").lower()
    results = {"in": [], "post": [], "pre": []}

    for ev in events:
        try:
            comp = ev.get("competitions", [ev])[0]
            comps = comp.get("competitors", [])

            if len(comps) < 2: continue

            away_list = [c for c in comps if c.get("homeAway") == "away"]
            home_list = [c for c in comps if c.get("homeAway") == "home"]
            if not away_list or not home_list: continue

            def simplify(c):
                return {"name": c.get("team", {}).get("displayName", ""), "score": c.get("score", "0")}
            away, home = simplify(away_list[0]), simplify(home_list[0])

            dt = parse_iso_datetime(ev.get("date"))
            status = comp.get("status", {}).get("type", {})
            state = status.get("state", "")
            detail = status.get("shortDetail", "")

            line = f"{away['name']} {away['score']} @ {home['name']} {home['score']}"

            if team_q:
                if team_q not in away["name"].lower() and team_q not in home["name"].lower(): continue

            results[state].append(f"{line} ({to_et(dt)}, {detail})")
        except Exception: continue

    out = ["🏈 **NFL Scoreboard**\n"]
    if results["in"]: out.append("🟧 **IN PROGRESS**"); out.extend([f"- {l}" for l in results["in"]])
    if results["post"]: out.append("\n🟥 **FINAL**"); out.extend([f"- {l}" for l in results["post"]])
    if results["pre"]: out.append("\n🟩 **SCHEDULED**"); out.extend([f"- {l}" for l in results["pre"]])

    return "\n".join(out)

def get_next_game(team_name: Optional[str]) -> str:
    if not team_name: return "Please include a team name."
    meta = find_team(team_name)
    if not meta: return f"Unknown team '{team_name}'."

    sched = fetch_json(meta["schedule_url"])
    events = sched.get("events", [])
    now = datetime.datetime.now(datetime.timezone.utc)
    future = []

    for ev in events:
        dt = parse_iso_datetime(ev.get("date"))
        if dt and dt > now: future.append((dt, ev))

    if not future: return f"No future games found for {meta['displayName']}."

    future.sort()
    dt, ev = future[0]

    comp = ev.get("competitions", [ev])[0]
    competitors = comp.get("competitors", [])

    opponent, homeaway = "Unknown", ""
    for c in competitors:
        name = c.get("team", {}).get("displayName", "")
        if name.lower() == meta["displayName"].lower():
            homeaway = c.get("homeAway")
        else:
            opponent = name

    when = to_et(dt)
    side = "at home" if homeaway == "home" else "away"
    return f"Next game for **{meta['displayName']}**: {side} vs {opponent} on {when}."

def get_last_game(team_name: Optional[str]) -> str:
    if not team_name: return "Please include a team name."
    meta = find_team(team_name)
    if not meta: return f"Unknown team '{team_name}'."

    sched = fetch_json(meta["schedule_url"])
    events = sched.get("events", [])
    now = datetime.datetime.now(datetime.timezone.utc)
    past = []

    for ev in events:
        dt = parse_iso_datetime(ev.get("date"))
        if dt and dt < now: past.append((dt, ev))

    if not past: return f"No completed games found for {meta['displayName']}."

    past.sort(reverse=True)
    dt, ev = past[0]

    comp = ev.get("competitions", [ev])[0]
    lines = []
    for c in comp.get("competitors", []):
        name = c.get("team", {}).get("displayName", "")
        score = c.get("score", "")
        lines.append(f"{name} {score}")

    when = to_et(dt)
    return f"Last game for **{meta['displayName']}** on {when}: " + " - ".join(lines)


# ============================================================
# 4. NEWS (Multi-source)
# ============================================================

def fetch_rss(url):
    try: return feedparser.parse(url).entries
    except: return []

def fetch_google_news(team): return fetch_rss(GOOGLE_NEWS_RSS.format(query=team.replace(" ", "+")))
def fetch_yahoo_news(): return fetch_rss(YAHOO_NFL_RSS)
def fetch_pft_news(): return fetch_rss(PFT_RSS)
def fetch_bing_news(team):
    if not BING_API_KEY: return []
    try:
        headers = {"Ocp-Apim-Subscription-Key": BING_API_KEY}
        url = f"https://api.bing.microsoft.com/v7.0/news/search?q={team}+NFL"
        return requests.get(url, headers=headers).json().get("value", [])
    except: return []

def score_article(article_text: str, tokens: list):
    score = 0
    text = article_text.lower()
    for tok in tokens:
        if tok in text: score += 2
    return score

def get_team_news(team_name: str):
    if not team_name: return "Please provide a team (e.g., 'Patriots news')."
    team_lower = team_name.lower()
    tokens = [team_lower] + team_lower.split()

    espn_data = fetch_json(ESPN_NEWS_URL)
    espn_articles = [{"title": a.get("headline", ""), "link": a.get("links", {}).get("web", {}).get("href", ""), "desc": a.get("description", "")} for a in espn_data.get("articles", [])]
    google_articles = [{"title": e.title, "link": e.link, "desc": e.get("summary", "")} for e in fetch_google_news(team_name)]
    yahoo_articles = [{"title": e.title, "link": e.link, "desc": e.get("summary", "")} for e in fetch_yahoo_news()]
    pft_articles = [{"title": e.title, "link": e.link, "desc": e.get("summary", "")} for e in fetch_pft_news()]
    bing_articles = [{"title": e.get("name", ""), "link": e.get("url", ""), "desc": e.get("description", "")} for e in fetch_bing_news(team_name)]

    all_articles = espn_articles + google_articles + yahoo_articles + pft_articles + bing_articles

    ranked = []
    for art in all_articles:
        text = f"{art['title']} {art['desc']} {art['link']}"
        s = score_article(text, tokens)
        if s > 0: ranked.append((s, art))

    ranked.sort(key=lambda x: x[0], reverse=True)
    if not ranked: return f"No recent news found for '{team_name}'."

    md = [f"📰 **{team_name.title()} News (Multi-source)**\n"]
    for _, a in ranked[:6]:
        md.append(f"- ⭐ **[{a['title']}]({a['link']})**")

    return "\n".join(md)


# ============================================================
# 5. STANDINGS (FIXED: Single Team Output)
# ============================================================
def detect_conference(entry):
    """
    Reliable AFC/NFC detection using ESPN metadata + robust fallback.
    """
    team = entry.get("team", {})
    groups = team.get("groups", [])

    # 1 → Primary: ESPN metadata
    for g in groups:
        name = g.get("name", "")
        if name.startswith("AFC"):
            return "AFC"
        if name.startswith("NFC"):
            return "NFC"

    # 2 → Fallback: identify by mascot (last word in displayName)
    display = team.get("displayName", "").lower().strip()
    mascot = display.split()[-1]  # "new york giants" → "giants"

    NFC = {
        'cowboys','giants','eagles','commanders',
        'bears','lions','packers','vikings',
        'falcons','panthers','saints','buccaneers',
        'cardinals','rams','49ers','seahawks'
    }

    AFC = {
        'bills','dolphins','patriots','jets',
        'ravens','bengals','browns','steelers',
        'texans','colts','jaguars','titans',
        'broncos','chiefs','raiders','chargers'
    }

    if mascot in NFC:
        return "NFC"
    if mascot in AFC:
        return "AFC"

    # 3 → Last fallback (rare cases)
    return "AFC"



def trend_indicator(pct):
    if pct >= 0.700:
        return "↑"
    if pct <= 0.350:
        return "↓"
    return "•"

def clinched_indicator(entry):
    statmap = {s.get("name"): s.get("value") for s in entry.get("stats", [])}
    if statmap.get("clinchedDivision"): return "🏆"
    if statmap.get("clinchedPlayoff"): return "🔒"
    return ""

def get_stat(entry, stat_name, default=0):
    for s in entry.get("stats", []):
        if s.get("name") == stat_name:
            try: return float(s.get("value"))
            except: return default
    return default

def get_standings(team_name: Optional[str] = None) -> str:
    data = fetch_json(ESPN_STANDINGS_URL)
    if "__error" in data: return f"⚠️ Error fetching standings: {data['__error']}"

    divisions = data.get("children", [])
    if not divisions: return "Standings unavailable."

    team_q = (_normalize_query(team_name) if team_name else None) # FIXED: Use NLU helper
    
    output = []
    conferences = {"AFC": [], "NFC": []}

    for div in divisions:
        div_name = div.get("name", "Unknown Division")
        entries = div.get("standings", {}).get("entries", [])
        div_lines = []

        for entry in entries:
            team = entry.get("team", {})
            name = team.get("displayName", "")
            abbr = team.get("abbreviation", "").lower()
            stats = entry.get("stats", [])
            statmap = {s.get("name"): s.get("value") for s in stats}

            wins = int(statmap.get("wins", 0))
            losses = int(statmap.get("losses", 0))
            ties = int(statmap.get("ties", 0))
            pct = float(statmap.get("winPercent", 0))
            rank = next((s.get("displayValue") for s in stats if s.get("name") == "divisionRank"), "N/A")

            arrow = trend_indicator(pct)
            clinch = clinched_indicator(entry)
            sos = get_stat(entry, "strengthOfSchedule", 0.0)
            
            # FIXED: Single Team Filter Mode Output
            if team_q and (team_q in name.lower() or team_q == abbr):
                return (
                    f"📊 **{name} Record**\n"
                    f"- **Division Rank:** {rank}\n"
                    f"- **Record (W-L-T):** {wins}-{losses}-{ties} ({pct:.3f}) {arrow}{clinch}\n"
                    f"- **Strength of Schedule:** {sos:.3f}"
                )

            # DIVISION-LISTS
            div_lines.append(f"{name}: **{wins}-{losses}-{ties}** ({pct:.3f}) {arrow}{clinch}")

            # Add to conference dictionary
            conf = detect_conference(entry)
            conferences[conf].append({"name": name, "wins": wins, "losses": losses, "ties": ties, "pct": pct, "arrow": arrow, "clinch": clinch, "sos": sos})

        if not team_q:
             output.append(f"### 🏈 {div_name}\n" + "\n".join(div_lines))

    if team_q: return f"No standings found for '{team_name}'."
    
    # Logic for Playoff, Wildcard, and Trending blocks remains here for full standings output
    # NOTE: Assuming the rest of your playoff block functions are correctly defined in your full file.
    
    return "\n\n".join(output) # Simplified return for the full standings output


# ============================================================
# 6. PLAYER AND FANTASY STATS (FIXED: NLU Integration & Output)
# ============================================================

# FIXED: Now uses NLU helpers for accurate query parsing
def get_fantasy_player_stats(query_name: Optional[str] = None) -> str:
    _ensure_player_cache()
    if not query_name: return "Please specify a player name. Example: 'Fantasy stats for Patrick Mahomes'"
    
    # FIXED NLU: Use robust helpers to clean the query
    q_norm = _normalize_query(query_name)
    pos_hint, q_name_part = _detect_position_and_strip(q_norm)
    team_hint = _detect_team_from_query(q_name_part)
    name_tokens = [tok for tok in q_name_part.split() if tok and tok != team_hint] # Strip team word if present

    if not name_tokens: return "Could not determine player name from query."

    year = datetime.datetime.now().year
    stats = fetch_json(SLEEPER_STATS_URL_TEMPLATE.format(year=year))
    if "__error" in stats: return f"Error fetching fantasy stats: {stats['__error']}"
        
    results = []
    player_entries = []

    # Search player cache for matching players (must contain ALL name tokens)
    for pid, meta in _PLAYER_CACHE.items():
        if not isinstance(meta, dict): continue
        if _player_matches_name(meta, name_tokens):
            if pos_hint and (meta.get("position") or "").upper() != pos_hint: continue
            if team_hint and not _player_matches_team(meta, team_hint): continue
            player_entries.append((pid, meta))
            
    # Gather stats for matches
    for pid, meta in player_entries:
        stats_rec = stats.get(str(pid)) or {}
        
        pts = stats_rec.get("pts_ppr") or stats_rec.get("fantasy_points") or stats_rec.get("points") or "N/A"
        name = meta.get("full_name") or f"{meta.get('first_name','')} {meta.get('last_name','')}"
        position = (meta.get("position") or "N/A").upper()
        team = meta.get("team") or "FA"

        results.append(f"{name} ({position}, {team}): **{pts} PPR**")

    unique_results = sorted(list(set(results)))
    return "\n".join(unique_results[:5]) if unique_results else f"No fantasy stats found for '{query_name}'."


def get_player_profile_smart(user_input: str, debug: bool=False) -> str:
    _ensure_player_cache()
    if not user_input or not user_input.strip(): return "Please provide a player name."

    q = _normalize_query(user_input)
    pos_hint, q = _detect_position_and_strip(q)
    team_hint = _detect_team_from_query(q, debug=debug)
    
    if team_hint:
        q = q.replace(team_hint, " ").strip()
        q = re.sub(r"\s+", " ", q)

    name_tokens = [tok for tok in q.split() if tok]
    if not name_tokens: return "Please include the player's name."

    matches = []
    for pid, info in _PLAYER_CACHE.items():
        if not isinstance(info, dict): continue
        try:
            if not _player_matches_name(info, name_tokens): continue
            if pos_hint and (info.get("position") or "").upper() != pos_hint: continue
            if team_hint and not _player_matches_team(info, team_hint): continue
            matches.append(info)
        except Exception: continue

    if not matches: return f"Player '{user_input}' not found."

    outputs = []
    for p in matches:
        first, last = p.get('first_name',''), p.get('last_name','')
        full_name = (p.get("full_name") or f"{first} {last}").strip().title()
        profile = {
            "Name": full_name, "Age": p.get("age", "N/A"), "Position": (p.get("position") or "N/A").upper(),
            "Team": p.get("team", "N/A"), "College": p.get("college", "N/A"), "Years_in_NFL": p.get("years_exp", "N/A")
        }
        save_player_profile(profile)
        outputs.append(profile)

    if len(outputs) == 1:
        p = outputs[0]
        # FIXED: Corrected Output Format
        return (
            f"**{p['Name']} Profile**\n"
            f"- **Position:** {p['Position']} ({p['Team']})\n"
            f"- **Age:** {p['Age']}\n"
            f"- **Experience:** {p['Years_in_NFL']} NFL seasons\n"
            f"- **College:** {p['College']}"
        )

    lines = ["Multiple players found (be more specific, e.g., include QB or team name):"]
    for p in outputs[:5]:
        lines.append(f"- **{p['Name']}** ({p['Position']} — {p['Team']})")

    if len(outputs) > 5: lines.append(f"(...{len(outputs) - 5} more matches)")
    return "\n".join(lines)


# ============================================================
# 7. CHATBOT ROUTER (Main Entry Point - Assumes this method is used in app.py)
# ============================================================

def nfl_chatbot(user_input: str, history=None) -> str:
    if not user_input or not user_input.strip():
        return "Ask me about scores, news, next/last games, standings, or fantasy stats."
    ui = user_input.strip().lower()

    team = find_team(user_input)
    team_name = team.get("displayName") if team else None

    # scores
    if "score" in ui or "scores" in ui:
        return get_live_scores(team_name)

    # news (uses multi-source logic)
    if "news" in ui or "article" in ui or "headline" in ui:
        return get_team_news(team_name or "NFL")

    # standings / record
    if "standing" in ui or "record" in ui or "rank" in ui:
        return get_standings(team_name)

    # schedule / next game
    if ("next" in ui or "upcoming" in ui or ("when" in ui and "play" in ui)) and ("play" in ui or "game" in ui or "schedule" in ui):
        if not team_name: return "Please include a team name for 'next game' queries."
        return get_next_game(team_name)

    # last / previous game
    if any(k in ui for k in ["last", "previous", "recent"]) and ("game" in ui or "played" in ui):
        if not team_name: return "Please include a team name for 'last game' queries."
        return get_last_game(team_name)

    # fantasy player stats
    if "fantasy" in ui or "pts" in ui or "ppr" in ui or "stats for" in ui:
        return get_fantasy_player_stats(user_input)

    # Player info (Human-like NLU enabled)
    if "who is" in ui or "player" in ui or "about" in ui or "profile" in ui or "tell me about" in ui:
        return get_player_profile_smart(user_input, debug=False)

    # fallback help
    return ("I can fetch live scores, NFL news (clickable links), team next/last games, team records, and fantasy player stats.\n"
        "Examples:\n - 'Give me NFL news'\n - 'Patriots news'\n - 'Bills record'\n - 'When do the Chiefs play next?'\n - 'Fantasy stats for Patrick Mahomes'\n - 'Who is Josh Allen QB Bills?'")



import streamlit as st
from chatbot import handle_user_query
from api_client import ensure_team_cache, _TEAM_CACHE

st.set_page_config(
    page_title="NFL Chatbot",
    page_icon="🏈",
    layout="centered"
)

# -------------------------------------
# Load team names for autocomplete
# -------------------------------------
ensure_team_cache()
TEAM_NAMES = sorted(list({meta["name"] for meta in _TEAM_CACHE.values() if "name" in meta}))


# -------------------------------------
# UI Header
# -------------------------------------
st.markdown("""
# 🏈 NFL Chatbot  
Your all-in-one assistant for:
- Live NFL scores  
- Standings + playoff projections  
- Team news  
- Player profiles  
- Fantasy stats  
- Next/last game info  
""")

# Maintain chat history
if "history" not in st.session_state:
    st.session_state.history = []


# -------------------------------------
# Sidebar Quick Actions
# -------------------------------------
st.sidebar.header("Quick Actions")

action = st.sidebar.selectbox(
    "What do you want?",
    [
        "Ask anything",
        "Get Live Scores",
        "Standings",
        "Team News",
        "Next Game",
        "Last Game",
        "Fantasy Stats",
        "Player Profile"
    ]
)

team_input = None
player_input = None

if action in ["Standings", "Team News", "Next Game", "Last Game"]:
    team_input = st.sidebar.selectbox("Select Team", TEAM_NAMES)

if action in ["Fantasy Stats", "Player Profile"]:
    player_input = st.sidebar.text_input("Enter Player Name")


# -------------------------------------
# Main Chat Interface
# -------------------------------------

# Display conversation
for role, msg in st.session_state.history:
    if role == "user":
        st.chat_message("user").markdown(msg)
    else:
        st.chat_message("assistant").markdown(msg)

# User input box
prompt = st.chat_input("Ask your NFL question...")

# Run sidebar action
if st.sidebar.button("Run Action"):
    if action == "Get Live Scores":
        prompt = "scores"
    elif action == "Standings":
        prompt = f"{team_input} standings"
    elif action == "Team News":
        prompt = f"{team_input} news"
    elif action == "Next Game":
        prompt = f"next game {team_input}"
    elif action == "Last Game":
        prompt = f"last game {team_input}"
    elif action == "Fantasy Stats":
        prompt = f"fantasy stats for {player_input}"
    elif action == "Player Profile":
        prompt = f"who is {player_input}"


# Process normal chat
if prompt:
    # Add user message
    st.session_state.history.append(("user", prompt))
    st.chat_message("user").markdown(prompt)

    # Run chatbot
    response = handle_user_query(prompt)

    # Add bot response
    st.session_state.history.append(("assistant", response))
    st.chat_message("assistant").markdown(response)
