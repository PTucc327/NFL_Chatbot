# ============================================================
# NFL CHATBOT — COMBINED UTILITIES MODULE
# Everything needed for: live scores, next game, last game.
# ============================================================

import requests
import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any

# -------------------------
# ESPN ENDPOINTS
# -------------------------
ESPN_SCOREBOARD_URL = "https://sports.core.api.espn.com/v2/sports/football/nfl/scoreboard"


# ============================================================
# 1. FETCHING UTILITIES
# ============================================================

def fetch_json(url: str) -> Dict[str, Any]:
    """
    Safely fetch JSON from ESPN's API.
    Handles all ESPN weirdness.
    """
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"__error": str(e)}


# ============================================================
# 2. TIMEZONE & DATE PARSING UTILITIES
# ============================================================

def _parse_iso_datetime(dt_str: Optional[str]) -> Optional[datetime.datetime]:
    """
    Parse ISO datetime safely. Handles ESPN's formats.
    """
    if not dt_str:
        return None
    try:
        return datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _to_et(dt: Optional[datetime.datetime]) -> str:
    """
    Convert datetime to ET formatted string.
    """
    if not dt:
        return "TBD"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)

    try:
        et = dt.astimezone(ZoneInfo("America/New_York"))
        return et.strftime("%I:%M %p EST")
    except Exception:
        return dt.isoformat()


# ============================================================
# 3. TEAM METADATA CACHE
# ============================================================

_TEAM_CACHE = {}

def _ensure_team_cache():
    """Cache all NFL teams from ESPN sports API."""
    global _TEAM_CACHE
    if _TEAM_CACHE:
        return

    url = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams"
    data = fetch_json(url)

    teams = data.get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
    for t in teams:
        info = t.get("team", {})
        if not info:
            continue

        name = info.get("displayName", "")
        abbr = info.get("abbreviation", "")
        slug = info.get("slug", "")

        # schedule endpoint requires pulling from ESPN's "team" API
        try:
            uid = info.get("id")
            schedule_url = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{uid}/schedule"
        except:
            schedule_url = None

        _TEAM_CACHE[name.lower()] = {
            "displayName": name,
            "abbreviation": abbr,
            "slug": slug,
            "schedule_url": schedule_url
        }


def _find_team(query: str) -> Optional[Dict[str, str]]:
    """Resolve team name/abbr/slug using the cache."""
    if not query:
        return None

    _ensure_team_cache()

    q = query.strip().lower()

    # direct displayName match
    if q in _TEAM_CACHE:
        return _TEAM_CACHE[q]

    # try abbreviation / slug
    for data in _TEAM_CACHE.values():
        if q == data["abbreviation"].lower() or q == data["slug"].lower():
            return data

    # partial match fallback
    for data in _TEAM_CACHE.values():
        if q in data["displayName"].lower():
            return data

    return None


# ============================================================
# 4. SCOREBOARD FORMATTING UTILITIES
# ============================================================

def format_scoreboard_event(home, away, status, timestamp):
    """
    Create formatted scoreboard line:
    Example:
    - Patriots 24 @ Bills 20 (08:20 PM EST, Final)
    """
    return f"- {away} @ {home} ({timestamp}, {status})"


# ============================================================
# 5. LIVE SCORES
# ============================================================

def get_live_scores(team_name: Optional[str] = None) -> str:
    """
    Fetch and format live NFL scoreboard.
    Supports filtering by team.
    """

    data = fetch_json(ESPN_SCOREBOARD_URL)
    if "__error" in data:
        return f"⚠️ Error fetching scores: {data['__error']}"

    events = data.get("events", [])
    results = {"in_progress": [], "final": [], "scheduled": []}

    team_q = (team_name or "").lower()

    for ev in events:
        try:
            competitions = ev.get("competitions", [])
            if not competitions:
                continue

            comp = competitions[0]
            competitors = comp.get("competitors", [])
            if len(competitors) != 2:
                continue

            home_team = competitors[0]
            away_team = competitors[1]

            home_name = home_team["team"]["displayName"]
            away_name = away_team["team"]["displayName"]

            home_score = home_team.get("score", "0")
            away_score = away_team.get("score", "0")

            # status
            status_info = comp.get("status", {})
            state = status_info.get("type", {}).get("state")
            status_text = status_info.get("type", {}).get("shortDetail", "")

            # timestamp
            dt = _parse_iso_datetime(ev.get("date"))
            timestamp = _to_et(dt)

            # always home = second, so format "Away @ Home"
            line = f"{away_name} {away_score} @ {home_name} {home_score}"

            # filtering if team provided
            if team_name:
                if team_q not in home_name.lower() and team_q not in away_name.lower():
                    continue

            # categorize
            if state == "in":
                results["in_progress"].append((line, timestamp, status_text))
            elif state == "post":
                results["final"].append((line, timestamp, status_text))
            else:
                results["scheduled"].append((line, timestamp, status_text))

        except Exception:
            continue

    # Build response
    out = ["🏈 **NFL Scoreboard**"]

    if results["in_progress"]:
        out.append("\n🟧 **IN PROGRESS**")
        for line, ts, status in results["in_progress"]:
            out.append(f"- {line} ({ts}, {status})")

    if results["final"]:
        out.append("\n🟥 **FINAL**")
        for line, ts, status in results["final"]:
            out.append(f"- {line} ({ts}, {status})")

    if results["scheduled"]:
        out.append("\n🟩 **SCHEDULED**")
        for line, ts, status in results["scheduled"]:
            out.append(f"- {line} ({ts}, {status})")

    return "\n".join(out)


# ============================================================
# 6. NEXT GAME
# ============================================================

def get_next_game(team_name: Optional[str]) -> str:
    if not team_name:
        return "Please include a team name."

    team_meta = _find_team(team_name)
    if not team_meta:
        return f"Unknown team '{team_name}'."

    data = fetch_json(team_meta["schedule_url"])
    if "__error" in data:
        return f"⚠️ Error fetching schedule: {data['__error']}"

    events = data.get("events", [])
    now = datetime.datetime.now(datetime.timezone.utc)

    future = []
    for ev in events:
        dt = _parse_iso_datetime(ev.get("date"))
        if dt and dt > now:
            future.append((dt, ev))

    if not future:
        return f"No future games found for {team_meta['displayName']}."

    future.sort(key=lambda x: x[0])
    dt, ev = future[0]

    comp = ev.get("competitions", [ev])[0]
    competitors = comp.get("competitors", [])

    team = team_meta["displayName"]
    opponent = "Unknown"
    homeaway = ""

    for c in competitors:
        name = c["team"]["displayName"]
        if team.lower() in name.lower():
            homeaway = c.get("homeAway")
        else:
            opponent = name

    when = _to_et(dt)

    ha = "home" if homeaway == "home" else "away" if homeaway == "away" else ""

    return f"Next game for {team}: {ha} vs {opponent} on {when}."


# ============================================================
# 7. LAST GAME
# ============================================================

def get_last_game(team_name: Optional[str]) -> str:
    if not team_name:
        return "Please include a team name."

    team_meta = _find_team(team_name)
    if not team_meta:
        return f"Unknown team '{team_name}'."

    data = fetch_json(team_meta["schedule_url"])
    if "__error" in data:
        return f"⚠️ Error fetching schedule: {data['__error']}"

    events = data.get("events", [])
    now = datetime.datetime.now(datetime.timezone.utc)

    past = []
    for ev in events:
        dt = _parse_iso_datetime(ev.get("date"))
        if dt and dt <= now:
            past.append((dt, ev))

    if not past:
        return f"No completed games found for {team_meta['displayName']}."

    past.sort(key=lambda x: x[0], reverse=True)
    dt, ev = past[0]

    comp = ev.get("competitions", [ev])[0]
    competitors = comp.get("competitors", [])

    lines = []
    for c in competitors:
        name = c["team"]["displayName"]
        score = c.get("score", "0")
        lines.append(f"{name} {score}")

    when = _to_et(dt)
    return f"Last game ({when}): " + " - ".join(lines)

    # -------------------------------------------------------
# TEAM-SPECIFIC MULTI-SOURCE NEWS
# -------------------------------------------------------
import feedparser
import requests

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}+NFL&hl=en-US&gl=US&ceid=US:en"
YAHOO_NFL_RSS = "https://sports.yahoo.com/nfl/rss.xml"
PFT_RSS = "https://profootballtalk.nbcsports.com/feed/"
ESPN_NEWS_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/news"
# Optional Bing search key
BING_API_KEY = None


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

import requests
from typing import Optional

STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/football/nfl/standings"


def fetch_json(url):
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"__error": str(e)}


# ----------------------------------------------------------
#   100% RELIABLE CONFERENCE DETECTOR
# ----------------------------------------------------------
def detect_conference(entry):
    """Detect AFC/NFC based solely on ESPN team.id prefix."""
    team = entry.get("team", {})
    tid = str(team.get("id", ""))

    if tid.startswith("1"):
        return "AFC"
    if tid.startswith("2"):
        return "NFC"

    # If ESPN ever changes ID scheme, use fallback by location keywords
    name = team.get("displayName", "").lower()
    if any(x in name for x in ["patriots", "bills", "jets", "dolphins", "chiefs", "broncos"]):
        return "AFC"
    if any(x in name for x in ["giants", "eagles", "cowboys", "packers", "rams"]):
        return "NFC"

    return "AFC"  # default fallback


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


# ----------------------------------------------------------
#   MAIN: get_standings()
# ----------------------------------------------------------
def get_standings(team_name: Optional[str] = None) -> str:
    data = fetch_json(STANDINGS_URL)

    if "__error" in data:
        return f"⚠️ Error fetching standings: {data['__error']}"

    divisions = data.get("children", [])
    if not divisions:
        return "Standings unavailable."

    team_q = team_name.lower() if team_name else None

    output = []
    conferences = {"AFC": [], "NFC": []}

    # -------------------------------
    # Process each division
    # -------------------------------
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

            # TEAM FILTER MODE
            if team_q:
                if team_q in name.lower() or team_q == abbr:
                    return f"📊 **{name} Standings**\n\n{wins}-{losses}-{ties} ({pct:.3f}) {arrow}{clinch}"

            # DIVISION OUTPUT MODE
            div_lines.append(f"{name}: **{wins}-{losses}-{ties}** ({pct:.3f}) {arrow}{clinch}")

            # Assign team to conference
            conf = detect_conference(entry)
            conferences[conf].append({
                "name": name,
                "wins": wins,
                "losses": losses,
                "ties": ties,
                "pct": pct,
                "arrow": arrow,
                "clinch": clinch
            })

        if not team_q:
            output.append(f"### 🏈 {div_name}\n" + "\n".join(div_lines))

    if team_q:
        return f"No standings found for '{team_name}'."

    # -------------------------------
    # Playoff Projection (Top 7)
    # -------------------------------
    def playoff_block(conf):
        teams = conferences.get(conf, [])
        if not teams:
            return f"## 🔥 {conf} Playoff Projection\n\n_No data_"

        teams_sorted = sorted(teams, key=lambda t: (-t["pct"], -t["wins"]))

        lines = [
            f"**{i+1}. {t['name']}** — {t['wins']}-{t['losses']}-{t['ties']} ({t['pct']:.3f}) {t['arrow']}{t['clinch']}"
            for i, t in enumerate(teams_sorted[:7])
        ]

        return f"## 🔥 {conf} Playoff Projection\n\n" + "\n".join(lines)

    output.append(playoff_block("AFC"))
    output.append(playoff_block("NFC"))

    return "\n\n".join(output)


