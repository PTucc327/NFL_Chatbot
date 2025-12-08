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

from typing import Optional, Dict, Any, List
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

# ------------------------- #
# Team Cache
# ------------------------- #
_TEAM_CACHE = {}
_TEAM_CACHE_LAST = 0


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
def fetch_rss(url: str):
    try:
        feed = feedparser.parse(url)
        return feed.entries or []
    except Exception:
        return []


def fetch_google_news(team: str):
    url = GOOGLE_NEWS_RSS.format(query=team.replace(" ", "+"))
    return fetch_rss(url)


def fetch_yahoo_news():
    return fetch_rss(YAHOO_NFL_RSS)


def fetch_pft_news():
    return fetch_rss(PFT_RSS)


def fetch_bing_news(team: str):
    if not BING_API_KEY:
        return []
    try:
        headers = {"Ocp-Apim-Subscription-Key": BING_API_KEY}
        url = f"https://api.bing.microsoft.com/v7.0/news/search?q={team}+NFL"
        data = requests.get(url, headers=headers, timeout=10).json()
        return data.get("value", [])
    except Exception:
        return []


def score_article(article_text: str, tokens: List[str]) -> int:
    text = (article_text or "").lower()
    score = 0
    for tok in tokens:
        if not tok:
            continue
        # token weight: full team, abbreviation, mascot
        if re.search(rf"\b{re.escape(tok)}\b", text):
            score += 3
        elif tok in text:
            score += 1
    # small bonus for multiple matches
    matches = sum(1 for tok in tokens if tok and tok in text)
    score += min(matches, 3)
    return score

def get_team_news(team_name: str, max_results: int = 6) -> str:
    """
    Multi-source team news. If few/no relevant team articles found, fall back to top NFL (ESPN) headlines.
    """
    if not team_name:
        return "Please provide a team, e.g., 'Patriots news'."
    team_lower = team_name.lower().strip()
    tokens = [team_lower] + team_lower.split()
    # also add common mascots / keywords (quick map)
    common_map = {
        "giants": ["giants", "new york giants", "ny giants", "big blue"],
        "jets": ["jets", "new york jets", "ny jets"],
        "patriots": ["patriots", "new england patriots"],
        "cowboys": ["cowboys", "dallas cowboys"],
    }
    last_word = team_lower.split()[-1]
    if last_word in common_map:
        tokens += common_map[last_word]
    tokens = list(dict.fromkeys([t.lower() for t in tokens if t]))

    # collect sources
    articles: List[Dict[str, str]] = []

    # ESPN
    espn_data = fetch_json(ESPN_NEWS_URL)
    if isinstance(espn_data, dict):
        espn_items = espn_data.get("articles") or espn_data.get("items") or []
        for it in espn_items:
            title = it.get("headline") or it.get("title") or ""
            link = (it.get("links", {}) or {}).get("web", {}).get("href") or it.get("canonical") or it.get("link") or ""
            desc = it.get("description") or it.get("summary") or ""
            articles.append({"title": title, "link": link, "desc": desc, "source": "ESPN"})

    # Google News
    try:
        for e in fetch_google_news(team_name):
            title = getattr(e, "title", "") or e.get("title", "")
            link = getattr(e, "link", "") or e.get("link", "")
            desc = getattr(e, "summary", "") or e.get("summary", "")
            articles.append({"title": title, "link": link, "desc": desc, "source": "Google"})
    except Exception:
        pass

    # Yahoo + PFT
    for e in fetch_yahoo_news():
        title = getattr(e, "title", "") or e.get("title", "")
        link = getattr(e, "link", "") or e.get("link", "")
        desc = getattr(e, "summary", "") or e.get("summary", "")
        articles.append({"title": title, "link": link, "desc": desc, "source": "Yahoo"})
    for e in fetch_pft_news():
        title = getattr(e, "title", "") or e.get("title", "")
        link = getattr(e, "link", "") or e.get("link", "")
        desc = getattr(e, "summary", "") or e.get("summary", "")
        articles.append({"title": title, "link": link, "desc": desc, "source": "PFT"})

    # Optional Bing
    for e in fetch_bing_news(team_name):
        title = e.get("name", "")
        link = e.get("url", "")
        desc = e.get("description", "")
        articles.append({"title": title, "link": link, "desc": desc, "source": "Bing"})

    # score & filter
    scored = []
    for art in articles:
        text_blob = f"{art.get('title','')} {art.get('desc','')} {art.get('link','')}"
        s = score_article(text_blob, tokens)
        if s > 0:
            scored.append((s, art))
    scored.sort(key=lambda x: x[0], reverse=True)

    if scored:
        top = scored[:max_results]
        md = [f"📰 **{team_name.title()} News (Multi-source)**\n"]
        for s, a in top:
            title = a.get("title") or "Untitled"
            link = a.get("link") or ""
            safe_title = title.replace("]", "\\]")
            if link:
                md.append(f"- ⭐ **[{safe_title}]({link})**")
            else:
                md.append(f"- ⭐ {safe_title}")
        return "\n".join(md)

    # fallback: top NFL headlines from ESPN (first 6)
    if isinstance(espn_data, dict):
        espn_items = espn_data.get("articles") or espn_data.get("items") or []
        lines = ["📰 **Top NFL Headlines (ESPN)**\n"]
        for it in (espn_items or [])[:6]:
            title = it.get("headline") or it.get("title") or it.get("description") or "Untitled"
            link = (it.get("links", {}) or {}).get("web", {}).get("href") or it.get("canonical") or ""
            if link:
                lines.append(f"- [{title}]({link})")
            else:
                lines.append(f"- {title}")
        return "\n".join(lines)
    return f"No recent news found for '{team_name}'."

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
# (ALL standings code placed here EXACTLY as currently working)
# To save space here, assume we paste your entire get_standings() implementation
#from your_full_code_import_here import get_standings   # <- Replace with your full function
def _detect_conference_from_entry(entry: Dict[str, Any]) -> str:
    # Prefer groups metadata
    team = entry.get("team", {}) or {}
    groups = team.get("groups") or []
    for g in groups:
        name = (g.get("name") or "").upper()
        if "AFC" in name: return "AFC"
        if "NFC" in name: return "NFC"
    # fallback using mascot
    dn = (team.get("displayName") or "").lower().strip()
    mascot = dn.split()[-1] if dn else ""
    NFC = {'cowboys','giants','eagles','commanders','bears','lions','packers','vikings','falcons','panthers','saints','buccaneers','cardinals','rams','49ers','seahawks'}
    AFC = {'bills','dolphins','patriots','jets','ravens','bengals','browns','steelers','texans','colts','jaguars','titans','broncos','chiefs','raiders','chargers'}
    if mascot in NFC: return "NFC"
    if mascot in AFC: return "AFC"
    return "AFC"


def trend_indicator(pct: float) -> str:
    if pct >= 0.700: return "↑"
    if pct <= 0.350: return "↓"
    return "•"


def clinched_indicator(entry: Dict[str, Any]) -> str:
    stats = entry.get("stats", []) or []
    statmap = {s.get("name"): s.get("value") for s in stats}
    if statmap.get("clinchedDivision"): return "🏆"
    if statmap.get("clinchedPlayoff"): return "🔒"
    return ""


def get_stat(entry: Dict[str, Any], stat_name: str, default: float = 0.0) -> float:
    for s in entry.get("stats", []) or []:
        if s.get("name") == stat_name:
            try:
                return float(s.get("value"))
            except Exception:
                return default
    return default


def get_standings(team_name: Optional[str] = None) -> str:
    data = fetch_json(ESPN_STANDINGS_URL)
    if "__error" in data:
        return f"⚠️ Error fetching standings: {data['__error']}"
    divisions = data.get("children") or []
    if not divisions:
        return "Standings unavailable."
    team_q = team_name.lower() if team_name else None
    conferences = {"AFC": [], "NFC": []}
    # parse nested divisions/groups -> entries
    for div in divisions:
        standings_section = (div.get("standings") or {}) or {}
        entries = standings_section.get("entries") or []
        for entry in entries:
            team = entry.get("team") or {}
            name = team.get("displayName") or team.get("name") or ""
            abbr = (team.get("abbreviation") or "").lower()
            stats = entry.get("stats") or []
            statmap = {s.get("name"): s.get("value") for s in stats}
            try:
                wins = int(float(statmap.get("wins", 0)))
                losses = int(float(statmap.get("losses", 0)))
                ties = int(float(statmap.get("ties", 0)))
            except Exception:
                wins = int(statmap.get("wins", 0) or 0)
                losses = int(statmap.get("losses", 0) or 0)
                ties = int(statmap.get("ties", 0) or 0)
            pct = float(statmap.get("winPercent", 0) or 0.0)
            arrow = trend_indicator(pct)
            clinch = clinched_indicator(entry)
            sos = get_stat(entry, "strengthOfSchedule", 0.0)
            # team filter
            if team_q and (team_q in name.lower() or team_q == abbr):
                return f"📊 **{name} Standings**\n\n{wins}-{losses}-{ties} ({pct:.3f}) {arrow}{clinch}\nStrength of Schedule: **{sos:.3f}**"
            conf = _detect_conference_from_entry(entry)
            conferences.setdefault(conf, []).append({"name": name, "wins": wins, "losses": losses, "ties": ties, "pct": pct, "arrow": arrow, "clinch": clinch, "sos": sos})
    # Build output blocks
    out_blocks = []
    # Division blocks (human-readable)
    # We'll attempt to include division blocks if data present in children
    for div in divisions:
        div_name = div.get("name") or ""
        entries = (div.get("standings") or {}).get("entries") or []
        if not entries:
            continue
        lines = []
        for e in entries:
            t = e.get("team") or {}
            n = t.get("displayName") or t.get("name") or ""
            stats = e.get("stats") or []
            statmap = {s.get("name"): s.get("value") for s in stats}
            wins = int(float(statmap.get("wins", 0) or 0))
            losses = int(float(statmap.get("losses", 0) or 0))
            ties = int(float(statmap.get("ties", 0) or 0))
            pct = float(statmap.get("winPercent", 0) or 0.0)
            lines.append(f"{n}: **{wins}-{losses}-{ties}** ({pct:.3f})")
        if lines:
            out_blocks.append("### 🏈 " + div_name + "\n" + "\n".join(lines))
    # Playoff projections per conference
    def playoff_block(conf: str) -> str:
        teams = conferences.get(conf, []) or []
        if not teams:
            return f"## 🔥 {conf} Playoff Projection\n\n_No data_"
        teams_sorted = sorted(teams, key=lambda t: (-t["pct"], -t["wins"]))
        lines = []
        for i, t in enumerate(teams_sorted[:7]):
            lines.append(f"**{i+1}. {t['name']}** — {t['wins']}-{t['losses']}-{t['ties']} ({t['pct']:.3f}) {t['arrow']}{t['clinch']}")
        return f"## 🔥 {conf} Playoff Projection\n\n" + "\n".join(lines)
    def wildcard_block(conf: str) -> str:
        teams = conferences.get(conf, []) or []
        teams_sorted = sorted(teams, key=lambda t: (-t["pct"], -t["wins"]))
        bubble = teams_sorted[7:12]
        if not bubble: return ""
        return "### 🌟 " + conf + " Wild Card Race (Seeds 8–12)\n" + "\n".join([f"- {t['name']}: {t['wins']}-{t['losses']}-{t['ties']} ({t['pct']:.3f}) {t['arrow']}" for t in bubble])
    def bubble_teams(conf: str) -> str:
        teams = conferences.get(conf, []) or []
        trending_up = [t for t in teams if t["arrow"] == "↑"]
        trending_down = [t for t in teams if t["arrow"] == "↓"]
        up_block = "\n".join([f"- {t['name']} ({t['pct']:.3f})" for t in trending_up[:5]]) or "_(none)_"
        down_block = "\n".join([f"- {t['name']} ({t['pct']:.3f})" for t in trending_down[:5]]) or "_(none)_"
        return f"### 📈 {conf} Trending Up\n{up_block}\n\n### 📉 {conf} Trending Down\n{down_block}"
    # assemble
    out_blocks.append(playoff_block("AFC"))
    wc_afc = wildcard_block("AFC")
    if wc_afc: out_blocks.append(wc_afc)
    out_blocks.append(bubble_teams("AFC"))
    out_blocks.append(playoff_block("NFC"))
    wc_nfc = wildcard_block("NFC")
    if wc_nfc: out_blocks.append(wc_nfc)
    out_blocks.append(bubble_teams("NFC"))
    return "\n\n".join(out_blocks)




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
# Player profile smart lookup
# -------------------------
# small in-memory DataFrame to save looked-up profiles (optional)
player_df = pd.DataFrame(columns=["Name", "Age", "Position", "Team", "College", "Years_in_NFL"])

def save_player_profile(profile: Dict[str, Any]):
    global player_df
    player_df = pd.concat([player_df, pd.DataFrame([profile])], ignore_index=True)

def _normalize_player_query(q: str) -> str:
    q = (q or "").lower().strip()
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    # strip common long phrases
    for w in ("who is", "tell me about", "player", "fantasy", "stats", "for"):
        q = q.replace(w, " ")
    q = re.sub(r"\s+", " ", q).strip()
    return q

def get_player_profile_smart(user_input: str, debug: bool = False) -> str:
    _ensure_player_cache()
    if not user_input or not user_input.strip():
        return "Please provide a player name."
    q = _normalize_player_query(user_input)
    tokens = [t for t in q.split() if t]
    if not tokens:
        return "Please include the player's name."
    matches = []
    for pid, meta in _PLAYER_CACHE.items():
        if not isinstance(meta, dict): continue
        full = (meta.get("full_name") or "").lower()
        if all(tok in full for tok in tokens):
            matches.append(meta)
    # fuzzy fallback
    if not matches:
        for pid, meta in _PLAYER_CACHE.items():
            if not isinstance(meta, dict): continue
            full = (meta.get("full_name") or "").lower()
            if any(tok in full for tok in tokens):
                matches.append(meta)
    if not matches:
        return f"Player '{user_input}' not found."
    outputs = []
    for p in matches[:10]:
        full = (p.get("full_name") or f"{p.get('first_name','')} {p.get('last_name','')}").title()
        profile = {
            "Name": full,
            "Age": p.get("age", "N/A"),
            "Position": (p.get("position") or "N/A").upper(),
            "Team": p.get("team") or "N/A",
            "College": p.get("college") or "N/A",
            "Years_in_NFL": p.get("years_exp") or "N/A"
        }
        save_player_profile(profile)
        outputs.append(profile)
    if len(outputs) == 1:
        p = outputs[0]
        return (f"**Name:** {p['Name']}\n- **Age:** {p['Age']}\n- **Position:** {p['Position']}\n- **Team:** {p['Team']}\n- **College:** {p['College']}\n- **Years in NFL:** {p['Years_in_NFL']}")
    lines = ["Multiple players found (be more specific):"]
    for p in outputs[:5]:
        lines.append(f"- {p['Name']} ({p['Position']} — {p['Team']})")
    if len(outputs) > 5:
        lines.append(f"...and {len(outputs)-5} more")
    return "\n".join(lines)
