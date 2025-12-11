"""
NFL API Client
Handles:
- Live scores
- Standings + playoff projections
- Team news (multi-source)
- Player lookup
- Fantasy stats
- Schedule (next/last game)
"""

from typing import Optional, Dict, Any, List, Tuple
import datetime
import re
import requests
import feedparser
import pandas as pd
import time

from utils import (
    fetch_json,
    parse_iso_datetime,
    to_et,
    trend_indicator,
    clean_query,
    CACHE_TTL
)

# ------------------------- #
# ESPN + Sleeper Endpoints
# ------------------------- #
# -------------------------
# Config & endpoints
# -------------------------
# -------------------------
# Config & endpoints
# -------------------------
REQUEST_TIMEOUT = 10
CACHE_TTL = 60 * 60 * 6   # 6 hours default cache (teams / players)
ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ESPN_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"
ESPN_TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
ESPN_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"
SLEEPER_PLAYERS_URL = "https://api.sleeper.app/v1/players/nfl"
SLEEPER_STATS_URL_TEMPLATE = "https://api.sleeper.app/v1/stats/nfl/regular/{year}"

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}+NFL&hl=en-US&gl=US&ceid=US:en"
YAHOO_NFL_RSS = "https://sports.yahoo.com/nfl/rss.xml"
PFT_RSS = "https://profootballtalk.nbcsports.com/feed/"
BING_API_KEY = None  # optional: set externally if you want Bing news fallback

 #Global constants for Player Lookup
COMMON_TEAM_NAMES = [
    "giants","cowboys","eagles","commanders","49ers","seahawks","rams","cardinals",
    "packers","bears","lions","vikings","saints","falcons","buccaneers","panthers",
    "chiefs","broncos","raiders","chargers","bills","patriots","dolphins","jets",
    "ravens","bengals","steelers","browns","colts","titans","jaguars","texans"
]
# Use uppercase for POSITIONS set
POSITIONS = {"QB","RB","WR","TE","K","P","DE","DT","LB","CB","S","OL","G","T","C"}

# -------------------------
# Local caches
# -------------------------
_TEAM_CACHE: Dict[str, Dict[str, Any]] = {}
_TEAM_CACHE_LAST = 0
_PLAYER_CACHE: Dict[str, Dict[str, Any]] = {}
_PLAYER_CACHE_LAST = 0

# -------------------------
# Team cache
# -------------------------
def ensure_team_cache(force: bool = False):
    global _TEAM_CACHE, _TEAM_CACHE_LAST
    now = time.time()
    if _TEAM_CACHE and (now - _TEAM_CACHE_LAST) < CACHE_TTL and not force:
        return
    _TEAM_CACHE = {}
    _TEAM_CACHE_LAST = now
    data = fetch_json(ESPN_TEAMS_URL)
    if "__error" in data:
        return
    teams = []
    try:
        sports = data.get("sports", [])
        if sports:
            leagues = sports[0].get("leagues", [])
            if leagues:
                teams = leagues[0].get("teams", [])
    except Exception:
        teams = data.get("teams", []) or []
    for item in teams:
        team_obj = item.get("team") if isinstance(item, dict) and "team" in item else item
        if not isinstance(team_obj, dict):
            continue
        tid = team_obj.get("id")
        display = team_obj.get("displayName") or team_obj.get("name") or ""
        abbr = team_obj.get("abbreviation") or ""
        slug = team_obj.get("slug") or ""
        schedule_url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{tid}/schedule" if tid else None
        meta = {"id": str(tid) if tid else None, "displayName": display, "abbr": abbr, "slug": slug, "schedule_url": schedule_url}
        # keys to store for fuzzy matching
        keys = set()
        if display: keys.add(display.lower())
        if abbr: keys.add(abbr.lower())
        if slug: keys.add(slug.lower())
        # also store by last word (mascot) and short name tokens
        if display:
            for part in re.split(r"[\s\-]+", display.lower()):
                if part: keys.add(part)
            mascot = display.lower().split()[-1]
            keys.add(mascot)
        for k in keys:
            _TEAM_CACHE[k] = meta
        if tid:
            _TEAM_CACHE[str(tid)] = meta


def find_team(query: Optional[str]) -> Optional[Dict[str, Any]]:
    if not query:
        return None
    ensure_team_cache()
    q = query.strip().lower()
    if not q: return None
    # direct keys
    if q in _TEAM_CACHE:
        return _TEAM_CACHE[q]
    # try partial match on displayName/slug/abbr
    for meta in _TEAM_CACHE.values():
        dn = (meta.get("displayName") or "").lower()
        ab = (meta.get("abbr") or "").lower()
        slug = (meta.get("slug") or "").lower()
        if q == ab or q == slug or q in dn or q in slug or q in ab:
            return meta
    # final fallback: substring anywhere
    for meta in _TEAM_CACHE.values():
        dn = (meta.get("displayName") or "").lower()
        if q in dn:
            return meta
    return None


# -------------------------
# Player cache (Sleeper)
# -------------------------
def _ensure_player_cache(force: bool = False):
    global _PLAYER_CACHE, _PLAYER_CACHE_LAST
    now = time.time()
    if _PLAYER_CACHE and (now - _PLAYER_CACHE_LAST) < CACHE_TTL and not force:
        return
    _PLAYER_CACHE = {}
    _PLAYER_CACHE_LAST = now
    data = fetch_json(SLEEPER_PLAYERS_URL)
    if "__error" in data:
        return
    if isinstance(data, dict):
        for pid, rec in data.items():
            # Normalize and store essential fields
            full = rec.get("full_name") or f"{rec.get('first_name','')} {rec.get('last_name','')}".strip()
            meta = {
                "id": pid,
                "first_name": rec.get("first_name") or "",
                "last_name": rec.get("last_name") or "",
                "full_name": full,
                "position": (rec.get("position") or "").upper(),
                "team": (rec.get("team") or ""),
                "age": rec.get("age"),
                "college": rec.get("college") or "",
                "years_exp": rec.get("years_exp") or rec.get("experience") or "N/A"
            }
            _PLAYER_CACHE[str(pid)] = meta
            # also store by lowercased name key for quick lookup
            if full:
                _PLAYER_CACHE[full.lower()] = meta


# ----------------------------------------------------
# Team news
# ----------------------------------------------------
# ----- RSS Fetcher -----
def fetch_rss(url):
    try:
        feed = feedparser.parse(url)
        return feed.entries
    except:
        return []


def fetch_google_news(team):
    url = GOOGLE_NEWS_RSS.format(query=team.replace(" ", "+"))
    return fetch_rss(url)


def fetch_yahoo_news():
    return fetch_rss(YAHOO_NFL_RSS)


def fetch_pft_news():
    return fetch_rss(PFT_RSS)


def fetch_bing_news(team):
    if not BING_API_KEY:
        return []
    try:
        headers = {"Ocp-Apim-Subscription-Key": BING_API_KEY}
        url = f"https://api.bing.microsoft.com/v7.0/news/search?q={team}+NFL"
        data = requests.get(url, headers=headers).json()
        return data.get("value", [])
    except:
        return []


# -------------------------------------------------------
#  SCORING SYSTEM (Relevance Ranking)
# -------------------------------------------------------
def score_article(article_text: str, tokens: list):
    text = article_text.lower()
    score = 0
    for tok in tokens:
        if tok in text:
            score += 2
    return score


# -------------------------------------------------------
#  MAIN NEWS FUNCTION
# -------------------------------------------------------
def get_team_news(team_name: str):
    """Fetch multi-source NFL news and rank results by relevance."""
    if not team_name:
        return "Please provide a team (e.g., 'Patriots news')."

    team_lower = team_name.lower()
    tokens = [team_lower] + team_lower.split()

    # ----- ESPN NEWS (via existing scoreboard fetch_json) -----
    espn_data = fetch_json(ESPN_NEWS_URL)
    espn_articles_raw = espn_data.get("articles", [])

    espn_articles = []
    for a in espn_articles_raw:
        title = a.get("headline", "")
        link = a.get("links", {}).get("web", {}).get("href", "")
        desc = a.get("description", "")
        espn_articles.append({"title": title, "link": link, "desc": desc})

    # ----- GOOGLE NEWS -----
    google_entries = fetch_google_news(team_name)
    google_articles = [
        {"title": e.title, "link": e.link, "desc": e.get("summary", "")}
        for e in google_entries
    ]

    # ----- YAHOO NEWS -----
    yahoo_entries = fetch_yahoo_news()
    yahoo_articles = [
        {"title": e.title, "link": e.link, "desc": e.get("summary", "")}
        for e in yahoo_entries
    ]

    # ----- PFT -----
    pft_entries = fetch_pft_news()
    pft_articles = [
        {"title": e.title, "link": e.link, "desc": e.get("summary", "")}
        for e in pft_entries
    ]

    # ----- BING NEWS -----
    bing_entries = fetch_bing_news(team_name)
    bing_articles = [
        {"title": e.get("name", ""), "link": e.get("url", ""), "desc": e.get("description", "")}
        for e in bing_entries
    ]

    # ------------------------------
    # Combine all sources
    # ------------------------------
    all_articles = (
        espn_articles +
        google_articles +
        yahoo_articles +
        pft_articles +
        bing_articles
    )

    # ------------------------------
    # Score relevance
    # ------------------------------
    ranked = []
    for art in all_articles:
        text = f"{art['title']} {art['desc']} {art['link']}"
        s = score_article(text, tokens)
        if s > 0:
            ranked.append((s, art))

    ranked.sort(key=lambda x: x[0], reverse=True)

    if not ranked:
        return f"No recent news found for '{team_name}'."

    top_articles = ranked[:6]

    # ------------------------------
    # Format Markdown Output
    # ------------------------------
    md = [f"📰 **{team_name.title()} News (Multi-source)**\n"]
    for score_val, a in top_articles:
        md.append(f"- ⭐ **[{a['title']}]({a['link']})**")

    return "\n".join(md)
# ----------------------------------------------------
# Live Scoreboard
# ----------------------------------------------------
def get_live_scores(team_name: Optional[str] = None):
    data = fetch_json(ESPN_SCOREBOARD_URL)

    if "__error" in data:
        return f"⚠️ Error fetching scores: {data['__error']}"

    events = data.get("events", [])

    if not events:
        return "🏈 No NFL games scheduled or in progress today."

    team_q = clean_query(team_name) if team_name else None
    results = {"in": [], "post": [], "pre": []}

    for ev in events:
        comp = ev.get("competitions", [ev])[0]
        comps = comp.get("competitors", [])

        if len(comps) < 2:
            continue

        def simplify(c):
            team = c.get("team", {})
            return {
                "name": team.get("displayName", ""),
                "abbr": team.get("abbreviation", "").lower(),
                "slug": team.get("slug", "").lower(),
                "score": c.get("score", "")
            }

        away = simplify([c for c in comps if c.get("homeAway") == "away"][0])
        home = simplify([c for c in comps if c.get("homeAway") == "home"][0])

        dt = parse_iso_datetime(ev.get("date"))
        status = comp.get("status", {}).get("type", {})
        state = status.get("state", "")
        detail = status.get("shortDetail", "")

        line = f"{away['name']} {away['score']} @ {home['name']} {home['score']} ({to_et(dt)}, {detail})"

        if team_q:
            if team_q not in away["name"].lower() and \
               team_q not in home["name"].lower():
                continue

        results[state].append(line)

    out = ["🏈 **NFL Scoreboard**\n"]

    if results["in"]:
        out.append("🟧 **IN PROGRESS**")
        out.extend([f"- {l}" for l in results["in"]])

    if results["post"]:
        out.append("\n🟥 **FINAL**")
        out.extend([f"- {l}" for l in results["post"]])

    if results["pre"]:
        out.append("\n🟩 **SCHEDULED**")
        out.extend([f"- {l}" for l in results["pre"]])

    return "\n".join(out)


# ----------------------------------------------------
# Standings + Playoffs (imports from your original)
# ----------------------------------------------------
# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------
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
    stats = entry.get("stats", [])
    statmap = {s.get("name"): s.get("value") for s in stats}
    if statmap.get("clinchedDivision"):
        return "🏆"
    if statmap.get("clinchedPlayoff"):
        return "🔒"
    return ""


def get_stat(entry, stat_name, default=0):
    """Safe helper to get numeric stats like SOS."""
    for s in entry.get("stats", []):
        if s.get("name") == stat_name:
            try:
                return float(s.get("value"))
            except:
                return default
    return default


# ---------------------------------------------------------
# Main Standings Function
# ---------------------------------------------------------
def get_standings(team_name: Optional[str] = None) -> str:
    data = fetch_json(ESPN_STANDINGS_URL)
    if "__error" in data:
        return f"⚠️ Error fetching standings: {data['__error']}"

    divisions = data.get("children", [])
    if not divisions:
        return "Standings unavailable."

    team_q = team_name.lower() if team_name else None

    output = []
    conferences = {"AFC": [], "NFC": []}

    # --------------------------------------
    # Parse all teams
    # --------------------------------------
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

            arrow = trend_indicator(pct)
            clinch = clinched_indicator(entry)
            sos = get_stat(entry, "strengthOfSchedule", 0.0)

            # TEAM FILTER MODE
            if team_q and (team_q in name.lower() or team_q == abbr):
                return (
                    f"📊 **{name} Standings**\n\n"
                    f"{wins}-{losses}-{ties} ({pct:.3f}) {arrow}{clinch}\n"
                    f"Strength of Schedule: **{sos:.3f}**"
                )

            # DIVISION-LISTS
            div_lines.append(
                f"{name}: **{wins}-{losses}-{ties}** ({pct:.3f}) {arrow}{clinch}"
            )

            # Add to conference dictionary
            conf = detect_conference(entry)
            conferences[conf].append({
                "name": name,
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "pct": pct,
                "arrow": arrow,
                "clinch": clinch,
                "sos": sos
            })

        if not team_q:
            output.append(f"### 🏈 {div_name}\n" + "\n".join(div_lines))

    if team_q:
        return f"No standings found for '{team_name}'."

    # --------------------------------------
    # PLAYOFF PROJECTIONS (Top 7)
    # --------------------------------------
    def playoff_block(conf):
        teams = conferences.get(conf, [])
        if not teams:
            return f"## 🔥 {conf} Playoff Projection\n\n_No data_"

        teams_sorted = sorted(teams, key=lambda t: (-t["pct"], -t["wins"]))

        lines = [
            f"**{i+1}. {t['name']}** — {t['wins']}-{t['losses']}-{t['ties']} "
            f"({t['pct']:.3f}) {t['arrow']}{t['clinch']}"
            for i, t in enumerate(teams_sorted[:7])
        ]

        return f"## 🔥 {conf} Playoff Projection\n\n" + "\n".join(lines)

    # --------------------------------------
    # WILD CARD RACE (Seeds 8–12)
    # --------------------------------------
    def wildcard_block(conf):
        teams = conferences.get(conf, [])
        teams_sorted = sorted(teams, key=lambda t: (-t["pct"], -t["wins"]))

        bubble = teams_sorted[7:12]  # seeds 8–12

        if not bubble:
            return ""

        lines = [
            f"- {t['name']}: {t['wins']}-{t['losses']}-{t['ties']} "
            f"({t['pct']:.3f}) {t['arrow']}"
            for t in bubble
        ]

        return f"### 🌟 {conf} Wild Card Race (Seeds 8–12)\n" + "\n".join(lines)

    # --------------------------------------
    # BUBBLE TEAMS (Trending up/down)
    # --------------------------------------
    def bubble_teams(conf):
        teams = conferences.get(conf, [])
        trending_up = [t for t in teams if t["arrow"] == "↑"]
        trending_down = [t for t in teams if t["arrow"] == "↓"]

        up_block = "\n".join([f"- {t['name']} ({t['pct']:.3f})" for t in trending_up[:5]])
        down_block = "\n".join([f"- {t['name']} ({t['pct']:.3f})" for t in trending_down[:5]])

        return (
            f"### 📈 {conf} Trending Up\n{up_block}\n\n"
            f"### 📉 {conf} Trending Down\n{down_block}"
        )

    output.append(playoff_block("AFC"))
    output.append(wildcard_block("AFC"))
    output.append(bubble_teams("AFC"))

    output.append(playoff_block("NFC"))
    output.append(wildcard_block("NFC"))
    output.append(bubble_teams("NFC"))

    return "\n\n".join(output)


# ----------------------------------------------------
# Schedule lookups
# ----------------------------------------------------
def get_next_game(team: str):
    meta = find_team(team)
    if not meta:
        return f"Team '{team}' not found."

    sched = fetch_json(meta["schedule_url"])
    events = sched.get("events", [])
    now = datetime.datetime.now(datetime.timezone.utc)
    future = []

    for ev in events:
        dt = parse_iso_datetime(ev.get("date"))
        if dt and dt > now:
            future.append((dt, ev))

    if not future:
        return "No upcoming games."

    future.sort()
    dt, ev = future[0]

    comp = ev.get("competitions", [ev])[0]
    comps = comp.get("competitors", [])

    opponent = ""
    homeaway = ""

    for c in comps:
        name = c.get("team", {}).get("displayName", "")
        if name.lower() == meta["name"].lower():
            homeaway = c.get("homeAway")
        else:
            opponent = name

    side = "at home" if homeaway == "home" else "away"

    return f"Next game for {meta['name']}: {side} vs {opponent} on {to_et(dt)}."


def get_last_game(team: str):
    meta = find_team(team)
    if not meta:
        return f"Team '{team}' not found."

    sched = fetch_json(meta["schedule_url"])
    events = sched.get("events", [])
    now = datetime.datetime.now(datetime.timezone.utc)

    past = []
    for ev in events:
        dt = parse_iso_datetime(ev.get("date"))
        if dt and dt < now:
            past.append((dt, ev))

    if not past:
        return "No past games."

    past.sort(reverse=True)
    dt, ev = past[0]

    comp = ev.get("competitions", [ev])[0]
    lines = []
    for c in comp.get("competitors", []):
        name = c.get("team", {}).get("displayName", "")
        score = c.get("score", "")
        lines.append(f"{name} {score}")

    return f"Last game for {meta['name']} on {to_et(dt)}: " + " - ".join(lines)


# ----------------------------------------------------
# Player lookup + fantasy (Sleeper)
# ----------------------------------------------------
# (copy your full get_player_profile_smart and get_fantasy_player_stats here)
def get_fantasy_player_stats(query_name: Optional[str] = None) -> str:
    _ensure_player_cache()
    if not query_name:
        return "Please specify a player name. Example: 'Fantasy stats for Patrick Mahomes'"
    # Normalize query
    q = query_name.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    tokens = [t for t in q.split() if t]
    if not tokens:
        return "Could not determine player from query."
    # fetch season stats from Sleeper (current year)
    year = datetime.datetime.now().year
    url = SLEEPER_STATS_URL_TEMPLATE.format(year=year)
    stats = fetch_json(url)
    if "__error" in stats:
        return f"Error fetching fantasy stats: {stats['__error']}"
    results = []
    # search player cache for matching players
    player_entries = []
    for pid, meta in _PLAYER_CACHE.items():
        # skip alias keys where value isn't a dict (we stored both id->meta and name->meta)
        if not isinstance(meta, dict):
            continue
        full = (meta.get("full_name") or "").lower()
        if all(tok in full for tok in tokens):
            player_entries.append((pid, meta))
    # try to match by simple token membership in name fields if none found
    if not player_entries:
        for pid, meta in _PLAYER_CACHE.items():
            if not isinstance(meta, dict): continue
            full = (meta.get("full_name") or "").lower()
            if any(tok in full for tok in tokens):
                player_entries.append((pid, meta))
    # gather stats for matches
    for pid, meta in player_entries:
        stats_rec = {}
        if isinstance(stats, dict):
            stats_rec = stats.get(str(pid)) or stats.get(pid) or {}
        # try common fantasy keys used by Sleeper
        pts = stats_rec.get("pts_ppr") or stats_rec.get("fantasy_points") or stats_rec.get("points") or stats_rec.get("pass_yds") or "N/A"
        name = meta.get("full_name") or f"{meta.get('first_name','')} {meta.get('last_name','')}"
        position = (meta.get("position") or "N/A").upper()
        team = meta.get("team") or "FA"
        results.append(f"{name} ({position}, {team}): **{pts} PPR**")
    # dedupe & return
    unique = sorted(set(results))
    if not unique:
        return f"No fantasy stats found for '{query_name}'."
    return "\n".join(unique[:5])


# -------------------------
# Player Stats (Incorporated Fixed Logic)
# -------------------------
# Initialize once
player_df = pd.DataFrame(columns=["Name", "Age", "Position", "Team", "College", "Years_in_NFL"])

def save_player_profile(profile_dict: Dict[str, Any]):
    global player_df
    player_df = pd.concat([player_df, pd.DataFrame([profile_dict])], ignore_index=True)

# Helper functions for robust player lookup

def _normalize_query(q: str) -> str:
    q = q.lower().strip()
    # remove punctuation
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    # collapse whitespace
    q = re.sub(r"\s+", " ", q).strip()

    # --- UPDATED: Added more human-like filler words to strip ---
    for w in (
        "who is", "tell me about", "show me", "give me",
        "player", "on the", "in the", "from", "team", "the",
        "for", "fantasy", "stats", "ppr", "pts",
        "qb for", "wr for", "rb for", "te for", "k for"
    ):
        # Use word boundaries (\b) to avoid removing parts of a name (e.g., 'who is' vs 'whis')
        # but since we are replacing single-word tokens in a loop, a simple replace is faster.
        q = q.replace(w, " ")

    q = re.sub(r"\s+", " ", q).strip()
    return q

def _detect_position_and_strip(query: str) -> Tuple[Optional[str], str]:
    """Return (position_hint_or_None, query_without_position)."""
    words = query.split()
    pos = None
    remaining = []
    for w in words:
        if w.upper() in POSITIONS:
            pos = w.upper()
        else:
            remaining.append(w)
    return pos, " ".join(remaining)

# NOTE: _detect_team_from_query relies on the global _team_cache which is handled by _ensure_team_cache()

def _detect_team_from_query(query: str, debug=False) -> Optional[str]:
    """
    Detect team token either from COMMON_TEAM_NAMES or from _team_cache (abbr/displayName/slug).
    Returns normalized team_token (like 'bills' or 'buf') or None.
    """
    # quick local check
    for t in COMMON_TEAM_NAMES:
        if t in query:
            if debug: print("team detected (common list):", t)
            return t

    # try to find team via team cache (if available)
    try:
        _ensure_team_cache()
    except Exception:
        pass

    # search _team_cache keys/values for matches
    for k, v in (globals().get("_team_cache") or {}).items():
        dn = (v.get("displayName") or "").lower()
        ab = (v.get("abbr") or "").lower()
        slug = (v.get("slug") or "").lower()
        if dn and dn in query:
            if debug: print("team detected (team_cache displayName):", dn)
            return dn.split()[-1] if " " in dn else dn
        if ab and ab.lower() in query:
            if debug: print("team detected (team_cache abbr):", ab)
            return ab
        if slug and slug in query:
            if debug: print("team detected (team_cache slug):", slug)
            return slug.split("-")[-1] if "-" in slug else slug

    return None

def _player_matches_name(info: Dict[str, Any], name_tokens: List[str]) -> bool:
    """Check if all name tokens are present in player's name fields."""
    first = (info.get("first_name") or "").lower().strip()
    last = (info.get("last_name") or "").lower().strip()
    full = (info.get("full_name") or f"{first} {last}").lower().strip()
    if not full:
        return False
    # All tokens must be present in the full name string
    return all(tok in full for tok in name_tokens)

def _player_matches_team(info: Dict[str, Any], team_filter: str) -> bool:
    """Check if player's team matches the normalized team filter (abbr, full, or short name)."""
    if not team_filter:
        return True
    team_field = (info.get("team") or "").lower()
    if team_filter in team_field:
        return True
    try:
        _ensure_team_cache()
        for k, v in (globals().get("_team_cache") or {}).items():
            dn = (v.get("displayName") or "").lower()
            ab = (v.get("abbr") or "").lower()
            slug = (v.get("slug") or "").lower()
            if team_filter == ab or team_filter in dn or team_filter in slug:
                if ab and ab in team_field: return True
                if dn and dn.split()[-1] in team_field: return True
    except Exception:
        pass
    return False


def get_player_profile_smart(user_input: str, debug: bool=False) -> str:
    """
    Robust player lookup:
      - normalize input
      - extract position hint (optional)
      - extract team hint (optional)
      - gather all name matches, then filter by position/team if provided
      - save matched profiles to player_df (internal)
      - return a readable string (single profile or short list)
    """
    try:
        _ensure_player_cache()
    except Exception as e:
        if debug: print(f"Error calling _ensure_player_cache: {e}")
        pass

    if not user_input or not user_input.strip():
        return "Please provide a player name."

    q = _normalize_query(user_input)
    if debug: print("normalized query:", q)

    pos_hint, q = _detect_position_and_strip(q)
    if debug: print("pos_hint:", pos_hint, "remaining:", q)

    team_hint = _detect_team_from_query(q, debug=debug)
    if team_hint:
        q = q.replace(team_hint, " ").strip()
        q = re.sub(r"\s+", " ", q)
    if debug: print("team_hint:", team_hint, "name part:", q)

    name_tokens = [tok for tok in q.split() if tok]
    if not name_tokens:
        return "Please include the player's name."

    matches = []
    player_cache = globals().get("_player_cache") or {}

    for pid, info in player_cache.items():
        try:
            if not _player_matches_name(info, name_tokens):
                continue
            if pos_hint:
                if (info.get("position") or "").upper() != pos_hint:
                    continue
            if team_hint and not _player_matches_team(info, team_hint):
                continue
            matches.append(info)
        except Exception:
            continue

    if debug:
        print("matches found:", len(matches))
        for m in matches[:6]:
            fn = (m.get("full_name") or f"{m.get('first_name','')} {m.get('last_name','')}").strip()
            print(" -", fn, m.get("position"), m.get("team"))

    if not matches:
        return f"Player '{user_input}' not found."

    # Save matched profiles in dataframe and prepare outputs
    outputs = []
    for p in matches:
        full_name = (p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}").strip().title()
        profile = {
            "Name": full_name,
            "Age": p.get("age", "N/A"),
            "Position": (p.get("position") or "N/A").upper(),
            "Team": p.get("team", "N/A"),
            "College": p.get("college", "N/A"),
            "Years_in_NFL": p.get("years_exp", "N/A")
        }
        save_player_profile(profile)
        outputs.append(profile)

    # If exactly one found -> return formatted text for it
    if len(outputs) == 1:
        p = outputs[0]
        return (
            f"**Name:** {p['Name']}\n"
            f"- **Age:** {p['Age']}\n"
            f"- **Position:** {p['Position']}\n"
            f"- **Team:** {p['Team']}\n"
            f"- **College:** {p['College']}\n"
            f"- **Years in NFL:** {p['Years_in_NFL']}"
        )

    # multiple matches -> short list
    lines = ["Multiple players found:"]
    # Show at most 5 matches for brevity
    for p in outputs[:5]:
        lines.append(f"- {p['Name']} ({p['Position']} — {p['Team']})")

    if len(outputs) > 5:
        lines.append(f"(...{len(outputs) - 5} more matches)")

    return "\n".join(lines)
