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
def ensure_team_cache():
    """Populate _team_cache with keys mapping to team metadata (id, displayName, abbr, slug, schedule_url)."""
    global _TEAM_CACHE, _TEAM_CACHE_LAST
    now = time.time()
    if _TEAM_CACHE and now - _TEAM_CACHE_LAST < CACHE_TTL:
        return
    _TEAM_CACHE = {}
    data = fetch_json(ESPN_TEAMS_URL)
    if "__error" in data:
        _TEAM_CACHE_LAST = now
        return
    teams = []
    if isinstance(data, dict):
        try:
            leagues = data.get("sports", [])[0].get("leagues", [])
            if leagues:
                teams = leagues[0].get("teams", [])
        except Exception:
            teams = data.get("teams", []) or []
    elif isinstance(data, list):
        teams = data

    for item in teams:
        team_obj = item.get("team") if isinstance(item, dict) and "team" in item else item
        if not isinstance(team_obj, dict):
            continue
        team_id = team_obj.get("id")
        display = team_obj.get("displayName") or team_obj.get("name") or team_obj.get("shortDisplayName")
        abbr = team_obj.get("abbreviation") or ""
        slug = team_obj.get("slug") or ""
        if not team_id:
            continue
        schedule_url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/schedule"
        meta = {
            "id": str(team_id),
            "displayName": display,
            "abbr": abbr,
            "slug": slug,
            "schedule_url": schedule_url
        }
        # store under several lookup keys (lowercased)
        keys = set()
        if display:
            keys.add(display.lower())
        if abbr:
            keys.add(abbr.lower())
        if slug:
            keys.add(slug.lower())
        for k in keys:
            _TEAM_CACHE[k] = meta
        # also store under the numeric id string
        _TEAM_CACHE[str(team_id)] = meta
    _TEAM_CACHE_LAST = now
    
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
def get_next_game(team_name: str) -> str:
    if not team_name:
        return "Please include a team name."

    meta = find_team(team_name)
    if not meta:
        return f"Could not resolve team '{team_name}'."

    sched_url = meta.get("schedule_url")
    if not sched_url:
        return f"Schedule unavailable for {meta.get('displayName')}."

    data = fetch_json(sched_url)
    if "__error" in data:
        return f"Error fetching schedule: {data['__error']}"

    events = data.get("events") or data.get("items") or []
    if not events:
        return f"No schedule data found for {meta['displayName']}."

    now = datetime.datetime.now(datetime.timezone.utc)

    future_games = []
    for ev in events:
        dt = parse_iso_datetime(ev.get("date"))
        if dt and dt > now:
            future_games.append((dt, ev))

    if not future_games:
        return f"No upcoming games found for {meta['displayName']}."

    future_games.sort(key=lambda x: x[0])
    dt, ev = future_games[0]

    comp = (ev.get("competitions") or [None])[0]
    competitors = comp.get("competitors", []) if comp else []

    opponent = "Unknown"
    home_away = ""

    for c in competitors:
        team = c.get("team", {})
        name = team.get("displayName", "")
        if meta["displayName"].lower() in name.lower():
            home_away = c.get("homeAway", "")
        else:
            opponent = name

    when = to_et(dt)
    ha_text = "at home" if home_away == "home" else "away" if home_away == "away" else ""

    return f"Next game for {meta['displayName']}: {ha_text} vs {opponent} on {when}."


def get_last_game(team_name: str) -> str:
    if not team_name:
        return "Please include a team name."

    meta = find_team(team_name)
    if not meta:
        return f"Could not resolve team '{team_name}'."

    sched_url = meta.get("schedule_url")
    if not sched_url:
        return f"Schedule unavailable for {meta.get('displayName')}."

    data = fetch_json(sched_url)
    if "__error" in data:
        return f"Error fetching schedule: {data['__error']}"

    events = data.get("events") or data.get("items") or []
    if not events:
        return f"No schedule data found for {meta['displayName']}."

    now = datetime.datetime.now(datetime.timezone.utc)

    past_games = []
    for ev in events:
        dt = parse_iso_datetime(ev.get("date"))
        if dt and dt <= now:
            past_games.append((dt, ev))

    if not past_games:
        return f"No completed games found for {meta['displayName']}."

    past_games.sort(key=lambda x: x[0], reverse=True)
    dt, ev = past_games[0]

    comp = (ev.get("competitions") or [None])[0]
    competitors = comp.get("competitors", []) if comp else []

    lines = []
    for c in competitors:
        team = c.get("team", {})
        name = team.get("displayName", "Unknown")
        score = c.get("score", "0")
        lines.append(f"{name} {score}")

    when = to_et(dt)
    return f"Last game for {meta['displayName']} on {when}: " + " – ".join(lines)

# ----------------------------------------------------
# Player lookup + fantasy (Sleeper)
# ----------------------------------------------------
# (copy your full get_player_profile_smart and get_fantasy_player_stats here)
def get_fantasy_player_stats(query_name: Optional[str] = None) -> str:
    _ensure_player_cache()

    if not query_name:
        return "Please specify a player name. Example: 'Fantasy stats for Josh Allen'"

    # Normalize query
    q = query_name.lower()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    tokens = q.split()

    if not tokens:
        return "Could not determine player from query."

    # Fantasy-relevant positions ONLY
    VALID_POSITIONS = {"QB", "RB", "WR", "TE"}

    # Fetch season stats
    year = datetime.datetime.now().year
    stats = fetch_json(SLEEPER_STATS_URL_TEMPLATE.format(year=year))

    if "__error" in stats:
        return f"Error fetching fantasy stats: {stats['__error']}"

    exact_matches = []
    partial_matches = []

    for meta in _PLAYER_CACHE.values():
        if not isinstance(meta, dict):
            continue

        pos = (meta.get("position") or "").upper()
        if pos not in VALID_POSITIONS:
            continue

        full = (meta.get("full_name") or "").lower()

        if full == q:
            exact_matches.append(meta)
        elif all(tok in full for tok in tokens):
            partial_matches.append(meta)

    candidates = exact_matches or partial_matches

    if not candidates:
        return f"No fantasy stats found for '{query_name}'."

    outputs = []

    for p in candidates:
        pid = p["id"]
        stat = stats.get(str(pid), {})

        pts = (
            stat.get("pts_ppr")
            or stat.get("fantasy_points")
            or stat.get("points")
        )

        if pts is None:
            continue

        name = p["full_name"]
        pos = p["position"]
        team = p.get("team", "FA")

        outputs.append((pts, f"{name} ({pos}, {team}): **{round(float(pts), 2)} PPR**"))

    if not outputs:
        return f"No usable fantasy stats found for '{query_name}'."

    # Sort by points descending and return BEST match
    outputs.sort(key=lambda x: x[0], reverse=True)
    return outputs[0][1]


# -------------------------
# Player Stats (Incorporated Fixed Logic)
# -------------------------
# Initialize once
player_df = pd.DataFrame(columns=["Name", "Age", "Position", "Team", "College", "Years_in_NFL"])

def save_player_profile(profile_dict: Dict[str, Any]):
    global player_df
    player_df = pd.concat([player_df, pd.DataFrame([profile_dict])], ignore_index=True)

# Helper functions for robust player lookup

def _normalize_player_query(q: str) -> str:
    q = (q or "").lower().strip()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    # strip common long phrases
    for w in ("who is", "tell me about", "player", "fantasy", "stats", "for"):
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
        ensure_team_cache()
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

def player_matches_name(info: Dict[str, Any], name_tokens: List[str]) -> bool:
    """Check if all name tokens are present in player's name fields."""
    first = (info.get("first_name") or "").lower().strip()
    last = (info.get("last_name") or "").lower().strip()
    full = (info.get("full_name") or f"{first} {last}").lower().strip()
    if not full:
        return False
    # All tokens must be present in the full name string
    return all(tok in full for tok in name_tokens)

def player_matches_team(info: Dict[str, Any], team_filter: str) -> bool:
    """Check if player's team matches the normalized team filter (abbr, full, or short name)."""
    if not team_filter:
        return True
    team_field = (info.get("team") or "").lower()
    if team_filter in team_field:
        return True
    try:
        ensure_team_cache()
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


def get_player_profile_smart(user_input: str, debug: bool = False) -> str:
    _ensure_player_cache()

    if not user_input or not user_input.strip():
        return "Please provide a player name."

    q = _normalize_player_query(user_input)
    tokens = [t for t in q.split() if t]

    if not tokens:
        return "Please include the player's name."

    # -------------------------------------------------
    # Collect UNIQUE players by ID (fixes duplicates)
    # -------------------------------------------------
    seen_ids = set()
    matches = []

    for key, meta in _PLAYER_CACHE.items():
        if not isinstance(meta, dict):
            continue

        pid = meta.get("id")
        if not pid or pid in seen_ids:
            continue

        full = (meta.get("full_name") or "").lower()

        if all(tok in full for tok in tokens):
            matches.append(meta)
            seen_ids.add(pid)

    # Fuzzy fallback
    if not matches:
        for key, meta in _PLAYER_CACHE.items():
            if not isinstance(meta, dict):
                continue

            pid = meta.get("id")
            if not pid or pid in seen_ids:
                continue

            full = (meta.get("full_name") or "").lower()
            if any(tok in full for tok in tokens):
                matches.append(meta)
                seen_ids.add(pid)

    if not matches:
        return f"Player '{user_input}' not found."

    # -------------------------------------------------
    # Single result → detailed profile
    # -------------------------------------------------
    if len(matches) == 1:
        p = matches[0]
        return (
            f"**Name:** {p.get('full_name').title()}\n"
            f"- **Age:** {p.get('age', 'N/A')}\n"
            f"- **Position:** {p.get('position', 'N/A').upper()}\n"
            f"- **Team:** {p.get('team', 'N/A')}\n"
            f"- **College:** {p.get('college', 'N/A')}\n"
            f"- **Years in NFL:** {p.get('years_exp', 'N/A')}"
        )

    # -------------------------------------------------
    # Multiple unique players → clean list
    # -------------------------------------------------
    lines = ["Multiple players found. Be more specific:"]
    for p in matches[:5]:
        lines.append(
            f"- {p.get('full_name').title()} "
            f"({p.get('position', '').upper()}, {p.get('team', 'N/A')})"
        )

    if len(matches) > 5:
        lines.append(f"...and {len(matches) - 5} more")

    return "\n".join(lines)
