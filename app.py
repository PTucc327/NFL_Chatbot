"""
NFL Chatbot UI (Enhanced UX Version)
Drop-in replacement for app.py — no changes needed to src/.
Fixes: player disambiguation buttons now render correctly (plain text,
no unrendered Markdown), quick actions are one click instead of two,
and the interface has a distinct visual identity instead of default
Streamlit chrome.
"""

import os
import re
import json
import html
import time
import random
import datetime
import itertools
import threading
import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv
from streamlit_mic_recorder import speech_to_text

from src.chatbot import nfl_chatbot_with_context, ChatbotResponse, QUOTA_ERROR, BUSY_MESSAGE
from src import api_client

load_dotenv()

# ------------------------------------------------------------------
# Local profile persistence — favorite team/player survive app restarts.
# OPT-IN (ENABLE_LOCAL_PREFS=1) for single-user local runs only: the file
# lives on the machine running the server, so on any hosted deployment
# every visitor would read and overwrite the same favorites. Off by
# default, the profile lives in session state only.
# ------------------------------------------------------------------
_PREFS_ENABLED = os.getenv("ENABLE_LOCAL_PREFS") == "1"
_PREFS_PATH = os.path.join(os.path.expanduser("~"), ".nfl_chatbot_prefs.json")

def _load_prefs() -> dict:
    if not _PREFS_ENABLED:
        return {}
    try:
        with open(_PREFS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_prefs(prefs: dict) -> None:
    if not _PREFS_ENABLED:
        return  # profile lives in session_state only
    try:
        with open(_PREFS_PATH, "w") as f:
            json.dump(prefs, f)
    except Exception:
        pass  # non-fatal — profile just won't persist across restarts

# ------------------------------------------------------------------
# Viewer-local time. The server clock is UTC on Streamlit Cloud, so
# timestamps use the browser's timezone (st.context), falling back to
# its UTC offset, then to US Eastern (NFL kickoff times are listed in ET).
# ------------------------------------------------------------------
def _now_local() -> datetime.datetime:
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    try:
        tz_name = st.context.timezone
        if tz_name:
            from zoneinfo import ZoneInfo
            return utc_now.astimezone(ZoneInfo(tz_name))
    except Exception:
        pass
    try:
        offset = st.context.timezone_offset  # JS getTimezoneOffset(): minutes *behind* UTC
        if offset is not None:
            return utc_now.astimezone(datetime.timezone(datetime.timedelta(minutes=-offset)))
    except Exception:
        pass
    try:
        from zoneinfo import ZoneInfo
        return utc_now.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        return utc_now


def _clock() -> str:
    return _now_local().strftime("%I:%M %p").lstrip("0")


# ------------------------------------------------------------------
# Cache warm-up — once per server process, in the background, so the
# first visitor after a cold start (Streamlit Cloud sleeps idle apps)
# doesn't wait ~3s for the player list, teams, week and season stats.
# ------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def _start_cache_warmup() -> bool:
    def warm():
        for step in (api_client._ensure_player_cache, api_client.ensure_team_cache,
                     api_client.current_nfl_week,
                     lambda: api_client._get_stats(api_client.current_nfl_season_year())):
            try:
                step()
            except Exception as e:  # warm-up is best-effort
                api_client.logger.warning("cache warm-up step failed: %s", e)
    threading.Thread(target=warm, name="cache-warmup", daemon=True).start()
    return True


_start_cache_warmup()

# ------------------------------------------------------------------
# Input Sanitization — applied to all free-text player name fields.
# Strips whitespace, enforces a length cap, and removes characters that
# have no place in a player name. This prevents blank/whitespace-only
# inputs from triggering API calls and limits the blast radius of any
# unexpected input reaching Gemini prompts.
# ------------------------------------------------------------------
_PLAYER_INPUT_MAX = 80  # chars — long enough for any real player name

def _sanitize_player(raw: str) -> str:
    """Return a cleaned player name, or empty string if input is invalid."""
    cleaned = raw.strip()[:_PLAYER_INPUT_MAX]
    # Allow letters, spaces, hyphens, apostrophes, and periods (e.g. "D.K. Metcalf")
    cleaned = re.sub(r"[^A-Za-z\s\-'.]", "", cleaned).strip()
    return cleaned

# ------------------------------------------------------------------
# Page Configuration
# ------------------------------------------------------------------
st.set_page_config(
    page_title="Sideline · Football Assistant",
    page_icon="🏈",
    layout="wide",
    initial_sidebar_state="auto",  # open on desktop, collapsed on phones
)

# ------------------------------------------------------------------
# Custom Styling
# ------------------------------------------------------------------
st.markdown("""
<style>
    /* Hide Streamlit chrome, but NOT the whole header: it contains the
       only control that opens the (initially collapsed) sidebar. */
    #MainMenu, footer,
    [data-testid="stToolbarActions"], [data-testid="stAppDeployButton"],
    [data-testid="stDecoration"] {visibility: hidden;}
    header[data-testid="stHeader"] {background: transparent;}

    .stApp {
        background: radial-gradient(circle at 20% 0%, #16202b 0%, #0d1420 55%, #0a0f18 100%);
    }

    section[data-testid="stSidebar"] {
        background: #0f1722;
        border-right: 1px solid #1f2b3a;
    }

    .hero {
        display: flex;
        align-items: center;
        gap: 14px;
        padding: 18px 22px;
        margin-bottom: 6px;
        background: linear-gradient(120deg, #1a2636 0%, #101923 100%);
        border: 1px solid #24344a;
        border-radius: 14px;
    }
    .hero .badge {
        font-size: 34px;
        line-height: 1;
    }
    .hero h1 {
        font-size: 22px;
        margin: 0;
        color: #f4f6f8;
        letter-spacing: 0.2px;
    }
    .hero p {
        margin: 2px 0 0 0;
        color: #8ea0b5;
        font-size: 13.5px;
    }

    .chip-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 14px 0 4px 0; }

    div[data-testid="stChatMessage"] {
        background: #131c28;
        border: 1px solid #1f2b3a;
        border-radius: 12px;
        padding: 4px 6px;
    }

    .msg-time {
        font-size: 11px;
        color: #8b9bb0;   /* 6.1:1 on the message background (WCAG AA) */
        margin-top: 2px;
    }

    div.stButton > button {
        border-radius: 9px;
        border: 1px solid #26374d;
        background: #17212f;
        color: #dbe4ee;
        font-size: 13.5px;
        padding: 6px 12px;
    }
    div.stButton > button:hover {
        border-color: #4f8ff0;
        color: #ffffff;
        background: #1c2b3f;
    }
    div.stButton > button[kind="primary"] {
        background: linear-gradient(120deg, #2f6fed 0%, #1f4fc4 100%);
        border: none;
        color: #ffffff;
        font-weight: 600;
        padding: 9px 12px;
    }
    div.stButton > button[kind="primary"]:hover {
        background: linear-gradient(120deg, #3f7bfa 0%, #2a5cd6 100%);
        color: #ffffff;
    }

    .player-card {
        border: 1px solid #26374d;
        border-radius: 10px;
        background: #131c28;
        padding: 10px 12px;
        text-align: center;
        margin-bottom: 6px;
    }
    .player-card .pname { font-weight: 600; color: #f0f4f8; font-size: 14px; }
    .player-card .pmeta { color: #8ea0b5; font-size: 12px; margin-top: 2px; }

    .hero .hero-note {
        font-size: 11.5px;
        color: #93a3b8;   /* 5.9:1 on the hero background */
        margin-top: 3px;
    }

    .empty-state {
        text-align: center;
        padding: 36px 20px 16px 20px;
        color: #7c8ba0;
        font-size: 14.5px;
    }

    /* ── Sidebar ─────────────────────────────────────────────── */
    /* Trim Streamlit's empty band above the sidebar content */
    [data-testid="stSidebarHeader"] { height: 2.75rem; padding-top: 0.5rem; padding-bottom: 0; }
    [data-testid="stSidebarUserContent"] { padding-top: 0.25rem; }

    /* Tabs: compact, full width */
    section[data-testid="stSidebar"] [data-baseweb="tab-list"] { gap: 0; }
    section[data-testid="stSidebar"] button[data-baseweb="tab"] {
        padding: 6px 2px; flex: 1 1 0; min-width: 0; justify-content: center;
    }
    section[data-testid="stSidebar"] button[data-baseweb="tab"] p { font-size: 12.5px; white-space: nowrap; }

    /* Keep two-column button grids side by side on phones (Streamlit
       otherwise stacks columns below 640px, doubling the length) */
    section[data-testid="stSidebar"] [data-testid="stHorizontalBlock"] {
        flex-wrap: nowrap !important; gap: 0.5rem;
    }
    section[data-testid="stSidebar"] [data-testid="stColumn"] {
        min-width: 0 !important; width: auto !important; flex: 1 1 0 !important;
    }

    /* Team card: logo + name + record line */
    .team-card {
        display: flex; align-items: center; gap: 12px;
        background: #131c28; border: 1px solid #24344a; border-radius: 10px;
        padding: 10px 12px; margin: 4px 0 10px 0;
    }
    .team-badge {
        display: inline-flex; align-items: center; justify-content: center; flex: none;
        border-radius: 50%; background: #1c2b3f; border: 1px solid #2f4a6b;
        color: #dbe4ee; font-weight: 700; letter-spacing: 0.5px;
    }
    .badge-row { display: flex; justify-content: center; margin-bottom: 6px; }
    .team-card .tc-name { font-weight: 600; color: #f0f4f8; font-size: 14.5px; line-height: 1.25; }
    .team-card .tc-meta { color: #93a3b8; font-size: 12.5px; margin-top: 2px; }

    /* Small section headings inside tabs */
    .sb-sub { font-size: 12px; font-weight: 600; letter-spacing: 0.4px; color: #93a3b8;
              text-transform: uppercase; margin: 14px 0 6px 0; }

    /* Sidebar grid buttons: one line each (icon + label), compact */
    /* Descendant selectors: a button with a tooltip (help=) sits inside an
       extra wrapper, so `div.stButton > button` would miss it */
    section[data-testid="stSidebar"] div.stButton button {
        padding: 6px 8px;
        font-size: 13px;
    }
    section[data-testid="stSidebar"] div.stButton button p {
        white-space: nowrap;
    }

    /* ── Touch targets: minimum 44px height on all buttons ─────── */
    div.stButton > button {
        min-height: 44px;
    }

    /* ── Keyboard focus rings ───────────────────────────────────── */
    div.stButton > button:focus-visible {
        outline: 2px solid #4f8ff0;
        outline-offset: 2px;
    }
    input:focus-visible, select:focus-visible, textarea:focus-visible {
        outline: 2px solid #4f8ff0;
        outline-offset: 2px;
    }

    /* ── Tablet breakpoint (≤ 768px) ───────────────────────────── */
    @media (max-width: 768px) {
        .stApp { overflow-x: hidden; }

        .hero { padding: 14px 16px; gap: 10px; }
        .hero h1 { font-size: 18px; }

        /* Cap all images so nothing causes horizontal overflow */
        img { max-width: 48px !important; }

        div[data-testid="stChatMessage"] { padding: 4px 4px; }

        /* Clear the sidebar toggle, which floats over the top-left corner */
        [data-testid="stMainBlockContainer"], .block-container { padding-top: 3.25rem; }
    }

    /* ── Mobile breakpoint (≤ 480px) ───────────────────────────── */
    @media (max-width: 480px) {
        .hero { padding: 10px 12px; gap: 8px; }
        .hero h1 { font-size: 16px; }
        .hero p  { font-size: 12px; }
        .empty-state { padding: 20px 12px 10px 12px; }
    }
</style>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------
# State Initialization
# ------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_mentioned" not in st.session_state:
    st.session_state["last_mentioned"] = None
if "profile" not in st.session_state:
    st.session_state["profile"] = _load_prefs()  # {"team": ..., "player": ...}
if "terms_accepted" not in st.session_state:
    st.session_state["terms_accepted"] = False
# Player choices awaiting a click, and a query queued by that click. Kept in
# session state so the Select buttons are re-rendered on the rerun their
# click triggers — otherwise Streamlit drops the click.
if "pending_selection" not in st.session_state:
    st.session_state["pending_selection"] = None
if "queued_query" not in st.session_state:
    st.session_state["queued_query"] = None

# ------------------------------------------------------------------
# Consent Gate — shown once per session before any interaction.
# Keeps the UI blocked until the user explicitly accepts.
# ------------------------------------------------------------------
if not st.session_state["terms_accepted"]:
    st.markdown("""
    <div style="max-width:560px; margin:80px auto 0 auto; background:#131c28;
                border:1px solid #26374d; border-radius:14px; padding:32px 36px;">
        <div style="font-size:32px; text-align:center; margin-bottom:12px;">🏈</div>
        <h2 style="text-align:center; color:#f4f6f8; margin:0 0 6px 0;
                   font-size:20px;">Welcome to Sideline</h2>
        <p style="text-align:center; color:#8ea0b5; font-size:13.5px;
                  margin:0 0 24px 0;">
            AI-powered NFL data — live scores, injuries, fantasy stats, and more.
        </p>
        <div style="background:#0d1420; border-radius:8px; padding:14px 16px;
                    font-size:13px; color:#8ea0b5; margin-bottom:20px;
                    line-height:1.6;">
            <strong style="color:#c8d6e5;">Before you continue:</strong><br>
            • <strong style="color:#c8d6e5;">You must be 18 or older</strong> to use this App
              (required by Google's Gemini API terms).<br>
            • Responses are AI-generated and may be inaccurate or delayed.<br>
            • Do not use this App for sports betting or high-stakes fantasy decisions.<br>
            • No account needed. Your chat lives only in this browser tab.<br>
            • Questions are answered with Google Gemini; on its free tier Google may
              use and human-review them, so don't include personal information.<br>
            • Independent fan project — not affiliated with the NFL, its teams, ESPN or Sleeper.<br>
            • Data is sourced from ESPN, Sleeper, and public RSS feeds.
        </div>
    </div>
    """, unsafe_allow_html=True)

    # Centre the buttons using columns
    # REPO_URL is set in Streamlit secrets (or .env locally). If absent,
    # the legal links just don't render — the disclaimer text still shows.
    _repo = os.getenv("REPO_URL", "")
    _tos_url  = f"{_repo}/blob/main/TERMS_OF_SERVICE.md"  if _repo else ""
    _priv_url = f"{_repo}/blob/main/PRIVACY_POLICY.md"    if _repo else ""
    _legal_links = (
        f"By continuing you confirm you're 18 or older and agree to the "
        f"<a href='{_tos_url}' target='_blank' style='color:#4f8ff0;'>Terms of Service</a> and "
        f"<a href='{_priv_url}' target='_blank' style='color:#4f8ff0;'>Privacy Policy</a>."
        if _repo else
        "By continuing you confirm you're 18 or older and agree to the Terms of Service and Privacy Policy."
    )

    _, col, _ = st.columns([2, 3, 2])
    with col:
        # Task 7 — show API key error before the agree button so a
        # misconfigured deployment is obvious before the user starts typing.
        if not os.getenv("GEMINI_API_KEY"):
            st.error(
                "⚠️ **Gemini API key not configured.** "
                "Add `GEMINI_API_KEY` to your `.env` file or Streamlit Secrets before using the app. "
                "[Get a free key →](https://aistudio.google.com/app/apikey)"
            )
        st.markdown(
            f"<p style='text-align:center; font-size:12.5px; color:#8492a6; margin-bottom:8px;'>"
            f"{_legal_links}</p>",
            unsafe_allow_html=True,
        )
        # Gemini API terms: no use by, or apps likely to be accessed by, under-18s.
        age_ok = st.checkbox("I confirm I'm 18 or older", key="age_confirmed")
        if st.button("✅ I agree — let's go", use_container_width=True, type="primary",
                     disabled=not age_ok,
                     help=None if age_ok else "Please confirm you're 18 or older first"):
            st.session_state["terms_accepted"] = True
            st.rerun()
    st.stop()  # Render nothing else until accepted

THINKING_MESSAGES = [
    "Checking the box score...",
    "Pulling the latest from the league office...",
    "Cross-referencing the depth chart...",
    "Digging through the play-by-play...",
]

def _typewriter(chunk_generator, delay: float = 0.005):  # ~1.5s added on a 300-word reply
    """
    Wraps a raw token/chunk generator and re-emits it word-by-word with a
    small delay between each, so replies visibly "type themselves out"
    instead of popping in as large bursts (which is how the underlying
    Gemini stream actually arrives — a handful of words per network chunk).
    """
    for chunk in chunk_generator:
        if not chunk:
            continue
        # Split on whitespace but keep the trailing space attached to each
        # word so spacing/newlines render naturally as they're rebuilt.
        for piece in re.findall(r"\S+\s*|\s+", chunk):
            yield piece
            time.sleep(delay)

EXAMPLES = [
    ("🗓️ Who's playing this week?",
     {"intents": ["schedule"]}),
    ("🏆 What's the playoff picture?",
     {"intents": ["playoffs"]}),
    ("🏥 Is Patrick Mahomes playing this week?",
     {"intents": ["injury", "schedule"], "player": "Patrick Mahomes"}),
    ("⚔️ Compare CeeDee Lamb and Ja'Marr Chase",
     {"intents": ["comparison"], "player": "CeeDee Lamb", "player_b": "Ja'Marr Chase"}),
    ("📈 Which team has the best defense?",
     {"intents": ["team_stats"]}),
    ("🌟 Who are the best rookies this season?",
     {"intents": ["leaders"], "stat": "pts_ppr"}),
]

# ------------------------------------------------------------------
# Team Reference Data — loaded from a bundled static file, not a live
# ESPN request. Team names/abbreviations/IDs don't change mid-season,
# and the live /teams endpoint returns a huge payload (16 logo variants
# + 6 links per team x 32 teams) that app.py never actually used — the
# logo URL is built from a hardcoded CDN pattern regardless. This makes
# the sidebar team list load instantly with zero network dependency.
# ------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def _load_team_data() -> dict:
    path = os.path.join(os.path.dirname(__file__), "data", "teams.json")
    with open(path, "r") as f:
        teams = json.load(f)
    return {t["displayName"]: t for t in teams}

_TEAM_LOOKUP = _load_team_data()
TEAM_NAMES = sorted(_TEAM_LOOKUP.keys())
# Sleeper player records carry abbreviations ("MIN"); Washington is "WAS"
# in Sleeper but "wsh" in ESPN's data.
_ABBR_LOOKUP = {t["abbr"].upper(): t for t in _TEAM_LOOKUP.values()}
_ABBR_LOOKUP["WAS"] = _ABBR_LOOKUP.get("WSH")

def _team_meta(name_or_abbr: str) -> dict:
    key = name_or_abbr or ""
    return _TEAM_LOOKUP.get(key) or _ABBR_LOOKUP.get(key.upper()) or {}

def team_badge(name_or_abbr: str, size: int = 44) -> str:
    """
    Text badge with the team's abbreviation ("KC"). Used instead of team
    logos: logos are the teams' trademarks and were loaded from ESPN's image
    servers. Abbreviations simply identify the team.
    """
    meta = _team_meta(name_or_abbr)
    abbr = (meta.get("abbr") or name_or_abbr or "FA")[:3].upper()
    label = html.escape(meta.get("displayName") or name_or_abbr or "Free agent")
    return (f'<span class="team-badge" role="img" aria-label="{label}" '
            f'style="width:{size}px;height:{size}px;font-size:{max(11, size // 3)}px">'
            f'{html.escape(abbr)}</span>')

# ------------------------------------------------------------------
# Sidebar: One-Click Quick Actions
# ------------------------------------------------------------------
@st.cache_data(ttl=600, show_spinner=False)
def _team_records_cached() -> dict:
    records = api_client.get_team_records()
    if not records:
        # Raising keeps a failed lookup out of the cache (exceptions aren't
        # cached), so the next rerun retries instead of showing no records
        # for 10 minutes.
        raise RuntimeError("standings unavailable")
    return records


def _team_records() -> dict:
    """Record / division / seed per team for the sidebar card (10-min cache)."""
    try:
        return _team_records_cached()
    except Exception as e:
        api_client.logger.warning("team records unavailable for sidebar card: %r", e)
        return {}


with st.sidebar:
    sidebar_prompt = None
    # Intent for each button, so the chatbot can skip Gemini intent extraction.
    sidebar_preset = None

    profile = st.session_state["profile"]
    fav_team = profile.get("team")
    fav_player = profile.get("player")

    def _grid(buttons, disabled: bool = False, help_text: str = None) -> None:
        """Two-column grid of (label, prompt, preset) buttons."""
        global sidebar_prompt, sidebar_preset
        for row in range(0, len(buttons), 2):
            cols = st.columns(2)
            for col, (label, prompt, preset) in zip(cols, buttons[row:row + 2]):
                if col.button(label, use_container_width=True, key=f"sb_{label}",
                              disabled=disabled, help=help_text if disabled else None):
                    sidebar_prompt, sidebar_preset = prompt, preset

    tab_team, tab_league, tab_fantasy, tab_me = st.tabs(["🏈 Team", "🌎 League", "🏆 Fantasy", "⭐ Me"])

    # ── Team ─────────────────────────────────────────────────────
    with tab_team:
        # A just-saved favorite becomes the lookup team. It must be applied
        # before the selectbox is created: Streamlit keeps a keyed widget's
        # value across reruns and ignores `index` after the first run.
        if pending_team := st.session_state.pop("pending_team_choice", None):
            st.session_state["team_choice"] = pending_team
        team_choice = st.selectbox(
            "Team", TEAM_NAMES, label_visibility="collapsed",
            index=TEAM_NAMES.index(fav_team) if fav_team in TEAM_NAMES else None,
            placeholder="Choose a team…", key="team_choice",
        )

        if team_choice:
            rec = _team_records().get(team_choice, {})
            details = " · ".join(x for x in (
                rec.get("record"), rec.get("division"),
                f"#{rec['seed']} seed" if rec.get("seed") else None) if x)
            logo_html = team_badge(team_choice)
            st.markdown(
                f'<div class="team-card">{logo_html}<div>'
                f'<div class="tc-name">{html.escape(team_choice)}</div>'
                f'<div class="tc-meta">{html.escape(details)}</div></div></div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("Pick a team to see its briefing, schedule, stats and more.")

        no_team = team_choice is None
        if st.button("📋 Daily Briefing", use_container_width=True, type="primary",
                     disabled=no_team, help="Choose a team first" if no_team else None):
            sidebar_prompt = (
                f"Give me a quick daily briefing for the {team_choice}: how they did "
                f"in their last game, when their next game is, the latest news, and "
                f"where they stand in the division. Also give me the biggest "
                f"storylines around the league right now."
            )
            sidebar_preset = {"intents": ["last_game", "schedule", "news", "standings", "league_news"],
                              "team": team_choice}

        _team = team_choice or ""
        _nick = _team.split()[-1] if _team else ""
        _grid([
            ("🗓️ Schedule", f"What's the {_team} schedule?",
             {"intents": ["schedule"], "team": _team}),
            ("⏮️ Last Game", f"How did the {_team} do in their last game?",
             {"intents": ["box_score"], "team": _team}),
            ("📊 Standings", f"How are the {_team} looking in the standings?",
             {"intents": ["standings"], "team": _team}),
            ("📈 Team Stats", f"How do the {_nick} rank on offense and defense?",
             {"intents": ["team_stats"], "team": _team}),
            ("📰 News", f"What's the latest news for the {_team}?",
             {"intents": ["news"], "team": _team}),
            ("👥 Roster", f"What does the {_team} depth chart look like?",
             {"intents": ["roster"], "team": _team}),
        ], disabled=no_team, help_text="Choose a team first")

    # ── League ───────────────────────────────────────────────────
    with tab_league:
        st.caption("Across all 32 teams")
        _grid([
            ("🗓️ This Week", "What's on the NFL schedule this week?",
             {"intents": ["schedule"]}),
            ("🏆 Playoffs", "What does the playoff picture look like?",
             {"intents": ["playoffs"]}),
            ("🏅 Leaders", "Who are the top fantasy scorers this season?",
             {"intents": ["leaders"], "stat": "pts_ppr"}),
            ("🌎 Headlines", "What are the biggest storylines around the NFL right now?",
             {"intents": ["league_news"]}),
            ("🛡️ Defenses", "Which team has the best defense?",
             {"intents": ["team_stats"]}),
            ("🌟 Rookies", "Who are the best rookies this season?",
             {"intents": ["leaders"], "stat": "pts_ppr"}),
        ])

    # ── Fantasy ──────────────────────────────────────────────────
    with tab_fantasy:
        p_name = st.text_input("Player", placeholder="e.g. CeeDee Lamb", key="fan_player")
        _sp = _sanitize_player(p_name or "")
        fc1, fc2 = st.columns(2)
        if fc1.button("💰 Outlook", use_container_width=True):
            if _sp:
                sidebar_prompt = f"Can you give me a fantasy breakdown for {_sp}?"
                sidebar_preset = {"intents": ["fantasy"], "player": _sp}
            else:
                st.toast("Type a player's name first.", icon="✏️")
        if fc2.button("🏥 Injury", use_container_width=True):
            if _sp:
                sidebar_prompt = f"What is the injury status for {_sp}?"
                sidebar_preset = {"intents": ["injury"], "player": _sp}
            else:
                st.toast("Type a player's name first.", icon="✏️")

        st.markdown('<div class="sb-sub">Compare or trade</div>', unsafe_allow_html=True)
        p1 = st.text_input("Player 1", label_visibility="collapsed", placeholder="Player 1", key="cmp_p1")
        p2 = st.text_input("Player 2", label_visibility="collapsed", placeholder="Player 2", key="cmp_p2")
        _sp1, _sp2 = _sanitize_player(p1 or ""), _sanitize_player(p2 or "")
        cc1, cc2 = st.columns(2)
        if cc1.button("⚔️ Compare", use_container_width=True):
            if _sp1 and _sp2:
                sidebar_prompt = f"Compare {_sp1} vs {_sp2}"
                sidebar_preset = {"intents": ["comparison"], "player": _sp1, "player_b": _sp2}
            else:
                st.toast("Enter both players to compare.", icon="✏️")
        if cc2.button("🔄 Trade", use_container_width=True):
            if _sp1 and _sp2:
                sidebar_prompt = f"Should I trade {_sp1} for {_sp2}?"
                sidebar_preset = {"intents": ["trade"], "player": _sp1, "player_b": _sp2}
            else:
                st.toast("Enter both players: the one you give, then the one you get.", icon="✏️")

        st.markdown('<div class="sb-sub">Waiver wire</div>', unsafe_allow_html=True)
        wc1, wc2 = st.columns(2)
        waiver_pos = wc1.selectbox("Position", ["Any", "QB", "RB", "WR", "TE"],
                                   label_visibility="collapsed", key="waiver_pos")
        if wc2.button("🔥 Pickups", use_container_width=True):
            sidebar_prompt = (
                "Who are the best waiver wire pickups right now?"
                if waiver_pos == "Any"
                else f"Who are the best {waiver_pos} waiver wire pickups right now?"
            )
            sidebar_preset = {"intents": ["waiver"],
                              "player": None if waiver_pos == "Any" else waiver_pos}

    # ── Me ───────────────────────────────────────────────────────
    with tab_me:
        if fav_team or fav_player:
            if fav_team:
                img = team_badge(fav_team)
                sub = f"⭐ {html.escape(fav_player)}" if fav_player else "Favorite team"
                st.markdown(f'<div class="team-card">{img}<div><div class="tc-name">'
                            f'{html.escape(fav_team)}</div><div class="tc-meta">{sub}</div></div></div>',
                            unsafe_allow_html=True)
            elif fav_player:
                st.markdown(f"**⭐ {fav_player}**")
            if st.button("🔔 Get My Updates", use_container_width=True, type="primary"):
                asks = []
                if fav_team:
                    asks.append(f"For the {fav_team}: how they did in their last game, when "
                                f"their next game is, the latest news, and where they stand "
                                f"in the standings.")
                if fav_player:
                    asks.append(f"For {fav_player}: their latest stats, fantasy outlook, and "
                                f"injury status.")
                asks.append("Also give me the biggest storylines around the league right now.")
                sidebar_prompt = "Give me my personalized update. " + " ".join(asks)
                sidebar_preset = {
                    "intents": (["last_game", "schedule", "news", "standings", "league_news"] if fav_team else ["league_news"])
                               + (["player", "injury"] if fav_player else []),
                    "team": fav_team, "player": fav_player,
                }
            form_box = st.expander("Edit favorites")
        else:
            st.caption("Save a favorite team and player for one-tap personalized updates.")
            form_box = st.container()

        with form_box:
            options = ["(none)"] + TEAM_NAMES
            new_team = st.selectbox("Favorite team", options,
                                    index=options.index(fav_team) if fav_team in options else 0,
                                    key="profile_team")
            new_player = st.text_input("Favorite player (optional)", value=fav_player or "",
                                       placeholder="e.g. Josh Allen", key="profile_player")
            if st.button("Save Profile", use_container_width=True):
                st.session_state["profile"] = {
                    "team": None if new_team == "(none)" else new_team,
                    "player": _sanitize_player(new_player) or None,
                }
                _save_prefs(st.session_state["profile"])
                if new_team != "(none)":
                    st.session_state["pending_team_choice"] = new_team
                st.rerun()

    # ── Footer: always visible ──────────────────────────────────
    st.divider()
    # Voice input: an embedded component, kept out of the prime spot.
    voice_input = speech_to_text(
        language="en",
        start_prompt="🎙️ Ask by voice",
        stop_prompt="⏹️ Stop recording",
        just_once=True,           # auto-clears after one recording, so it
                                   # won't keep resubmitting on reruns
        use_container_width=True,
        key="voice_input",
    )
    ec1, ec2 = st.columns(2)
    _has_msgs = len(st.session_state.messages) > 0
    if _has_msgs:
        _export_lines = []
        for _m in st.session_state.messages:
            _role = "You" if _m["role"] == "user" else "Sideline"
            _ts   = _m.get("time", "")
            _prefix = f"[{_ts}] {_role}:" if _ts else f"{_role}:"
            _export_lines.append(f"{_prefix}\n{_m['content']}\n")
        ec1.download_button(
            label="📥 Export",
            data="\n".join(_export_lines),
            file_name=f"sideline-chat-{datetime.date.today()}.txt",
            mime="text/plain",
            use_container_width=True,
        )
    else:
        ec1.button("📥 Export", disabled=True, use_container_width=True,
                   help="Nothing to export yet — start a conversation first.")
    if ec2.button("🗑️ Clear", use_container_width=True, disabled=not _has_msgs,
                  help="Start a new conversation"):
        st.session_state.messages = []
        st.session_state["last_mentioned"] = None
        st.session_state["pending_selection"] = None
        st.rerun()

# Phones: the sidebar overlays the chat, so close it after a tool is used —
# otherwise the answer is hidden behind it.
if sidebar_prompt:
    components.html(
        """<script>
        const doc = window.parent.document;
        if (window.parent.innerWidth < 768) {
            const btn = doc.querySelector('[data-testid="stSidebarCollapseButton"] button')
                     || doc.querySelector('[data-testid="stSidebarCollapseButton"]');
            if (btn) btn.click();
        }
        </script>""",
        height=0,
    )

# ------------------------------------------------------------------
# Header
# ------------------------------------------------------------------
st.markdown("""
<div class="hero">
    <div class="badge">🏈</div>
    <div>
        <h1>Sideline</h1>
        <p>Scores, schedules, standings, box scores, injuries and fantasy advice — just ask.</p>
        <p class="hero-note">
            ⚠️ AI-generated — verify before acting. Not for betting.
            Data: ESPN · Sleeper · news feeds.
        </p>
    </div>
</div>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------
# Empty State (first visit) — example questions
# ------------------------------------------------------------------
# chat_input always renders pinned to the bottom; reading it here lets the
# examples hide on the run that answers a typed first question.
user_input = st.chat_input("Ask anything — e.g. \"How's the Bills defense?\" or \"Who's on bye?\"")

_question_pending = bool(user_input or sidebar_prompt or voice_input
                         or st.session_state.get("queued_query"))
if not st.session_state.messages and not _question_pending:
    st.markdown('<div class="empty-state">Try one of these, or type your own question below 👇</div>',
                unsafe_allow_html=True)
    for row in range(0, len(EXAMPLES), 3):
        cols = st.columns(3)
        for col, (label, preset) in zip(cols, EXAMPLES[row:row + 3]):
            if col.button(label, key=f"ex_{label}", use_container_width=True):
                # Strip the emoji: the label doubles as the question text.
                st.session_state["queued_query"] = label.split(" ", 1)[1]
                st.session_state["queued_preset"] = preset
                st.rerun()

# ------------------------------------------------------------------
# Chat History
# ------------------------------------------------------------------
def _render_chart(chart: dict) -> None:
    """Weekly PPR line chart, one labeled line per player."""
    series = chart.get("series") or {}
    if not series or len(chart.get("weeks", [])) < 4:
        return
    import pandas as pd
    # Numeric week index keeps "Wk 10" after "Wk 9" (text labels sort alphabetically).
    weeks = [int(w.split()[-1]) for w in chart["weeks"]]
    df = pd.DataFrame(series, index=pd.Index(weeks, name="Week"))
    st.caption("📈 Weekly PPR fantasy points")
    # Explicit colors: the dark theme's default first two are both blues.
    palette = ["#4f8ff0", "#f5a524", "#3ecf8e", "#e5484d"]
    st.line_chart(df, x_label="Week", y_label="PPR points",
                  color=palette[:len(df.columns)])


for message in st.session_state.messages:
    avatar = "🏈" if message["role"] == "assistant" else "🙋"
    with st.chat_message(message["role"], avatar=avatar):
        st.markdown(message["content"])
        if message.get("chart"):
            _render_chart(message["chart"])
        if ts := message.get("time"):
            st.markdown(f'<div class="msg-time">{ts}</div>', unsafe_allow_html=True)

# ------------------------------------------------------------------
# Input Handling — text (voice input lives in the sidebar now)
# ------------------------------------------------------------------
queued_query = st.session_state.pop("queued_query", None)
queued_preset = st.session_state.pop("queued_preset", None)
final_query = queued_query or sidebar_prompt or voice_input or user_input
final_preset = queued_preset if queued_query else (sidebar_preset if sidebar_prompt else None)


def _render_pending_selection() -> None:
    """Player cards + Select buttons for an ambiguous name. A click queues a
    team-qualified query that runs through the normal chatbot pipeline."""
    pending = st.session_state.get("pending_selection")
    if not pending:
        return
    with st.chat_message("assistant", avatar="🏈"):
        for idx, p in enumerate(pending):
            meta = _team_meta(p.get("team", ""))
            team_label = meta.get("displayName") or p.get("team") or "FA"
            safe_name = html.escape(str(p.get("full_name", "Unknown")))
            safe_team = html.escape(str(team_label))
            safe_pos  = html.escape(str(p.get("position", "")))
            logo_html = f'<div class="badge-row">{team_badge(p.get("team") or "", 40)}</div>'
            st.markdown(
                f'<div class="player-card">{logo_html}'
                f'<div class="pname">{safe_name}</div>'
                f'<div class="pmeta">{safe_team} · {safe_pos}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
            p_id = p.get("player_id") or p.get("id") or idx
            if st.button(f"Select {p.get('full_name', '')} ({p.get('team') or 'FA'})",
                         key=f"sel_{p_id}", use_container_width=True):
                st.session_state["pending_selection"] = None
                st.session_state["last_mentioned"] = p.get("full_name")
                where = f"{p.get('position', '')} for the {team_label}" if meta else "free agent"
                st.session_state["queued_query"] = f"Tell me about {p.get('full_name')} ({where})"
                st.session_state["queued_preset"] = {
                    "intents": ["player"], "player": p.get("full_name"), "team": p.get("team"),
                }
                st.rerun()


if final_query:
    # Any new question supersedes an unanswered player choice.
    st.session_state["pending_selection"] = None
else:
    _render_pending_selection()

# Enforce a hard input cap — prevents runaway Gemini token costs and prompt
# injection via extremely long pasted text. 500 chars is well above any
# natural NFL question; anything longer is either a paste error or abuse.
if final_query:
    final_query = final_query.strip()[:500]

if final_query:
    now = _clock()
    st.session_state.messages.append({"role": "user", "content": final_query, "time": now})
    with st.chat_message("user", avatar="🙋"):
        st.markdown(final_query)
        st.markdown(f'<div class="msg-time">{now}</div>', unsafe_allow_html=True)

    with st.chat_message("assistant", avatar="🏈"):
        with st.spinner(random.choice(THINKING_MESSAGES)):
            # Intent extraction + data fetching happen here (blocking).
            # For normal replies this returns a *generator* — Gemini's
            # streaming is lazy, so the spinner covers "gathering data" and
            # disappears as the answer starts typing out.
            response = nfl_chatbot_with_context(final_query, preset=final_preset)

        reply_time = _clock()

        # --- Streaming text response (the normal case) ---
        if isinstance(response, ChatbotResponse):
            try:
                first_chunk = next(response.stream)
            except StopIteration:
                first_chunk = ""

            if isinstance(first_chunk, str) and first_chunk.startswith("__CONFIG_ERROR__"):
                error_msg = (
                    "⚠️ **Gemini API key not configured.**\n\n"
                    "To enable the AI assistant:\n"
                    "1. Get a free key at [Google AI Studio](https://aistudio.google.com/app/apikey)\n"
                    "2. Copy `template.env` to `.env` and add your key\n"
                    "3. Restart the app"
                )
                st.error(error_msg)
                st.session_state.messages.append({"role": "assistant", "content": error_msg, "time": reply_time})

            elif first_chunk == QUOTA_ERROR:
                st.warning(BUSY_MESSAGE)
                st.session_state.messages.append({"role": "assistant", "content": BUSY_MESSAGE, "time": reply_time})

            elif isinstance(first_chunk, str) and first_chunk.startswith("__API_ERROR__"):
                error_msg = "⚠️ I'm having trouble reaching Gemini right now. Please try again in a moment."
                st.error(error_msg)
                st.session_state.messages.append({"role": "assistant", "content": error_msg, "time": reply_time})

            else:
                full_response = st.write_stream(
                    _typewriter(itertools.chain([first_chunk], response.stream))
                )
                # Weekly PPR chart, kept with the message so it survives reruns.
                if response.chart_data:
                    _render_chart(response.chart_data)
                st.markdown(f'<div class="msg-time">{reply_time}</div>', unsafe_allow_html=True)
                st.session_state.messages.append({"role": "assistant", "content": full_response,
                                                  "time": reply_time, "chart": response.chart_data})

        # --- Missing API key (non-streaming path, e.g. a future blocking call) ---
        elif isinstance(response, str) and response.startswith("__CONFIG_ERROR__"):
            error_msg = (
                "⚠️ **Gemini API key not configured.**\n\n"
                "To enable the AI assistant:\n"
                "1. Get a free key at [Google AI Studio](https://aistudio.google.com/app/apikey)\n"
                "2. Copy `template.env` to `.env` and add your key\n"
                "3. Restart the app"
            )
            st.error(error_msg)
            st.session_state.messages.append({"role": "assistant", "content": error_msg, "time": reply_time})

        # --- Player disambiguation ---
        elif isinstance(response, dict) and response.get("type") == "selection_required":
            player_list = response.get("matches", [])

            if player_list:
                disambiguation_msg = response.get("message", "I found a few players with that name. Who did you mean?")
                st.session_state.messages.append({"role": "assistant", "content": disambiguation_msg, "time": reply_time})
                # Keep only the fields the cards need — the raw Sleeper
                # record is large. Rerun so the cards render via
                # _render_pending_selection, where their clicks survive.
                st.session_state["pending_selection"] = [
                    {k: p.get(k) for k in ("player_id", "full_name", "team", "position")}
                    for p in player_list
                ]
                st.rerun()
            else:
                fallback_msg = "I found multiple matches but had trouble loading the details. Try adding the team name to your search!"
                st.warning(fallback_msg)
                st.session_state.messages.append({"role": "assistant", "content": fallback_msg, "time": reply_time})

        # --- Standard response ---
        else:
            st.markdown(response)
            st.markdown(f'<div class="msg-time">{reply_time}</div>', unsafe_allow_html=True)
            st.session_state.messages.append({"role": "assistant", "content": response, "time": reply_time})

# ------------------------------------------------------------------
# Footer — legal links, attribution, AI disclaimer
# Always rendered below the chat, regardless of conversation state.
# ------------------------------------------------------------------
_repo     = os.getenv("REPO_URL", "")
_tos_url  = f"{_repo}/blob/main/TERMS_OF_SERVICE.md" if _repo else "#"
_priv_url = f"{_repo}/blob/main/PRIVACY_POLICY.md"   if _repo else "#"

st.markdown("---")
st.markdown(
    f"""
<div style="text-align:center; font-size:12px; color:#8492a6; padding:8px 0 16px 0; line-height:2;">
    Sideline is an independent fan project — not affiliated with the NFL, its teams, ESPN, Sleeper, or Google.<br>
    Responses are AI-generated and may be inaccurate. Not for use in sports betting.<br>
    Data sourced from <strong>ESPN</strong> · <strong>Sleeper</strong> · <strong>Yahoo Sports</strong> · <strong>NBC Sports PFT</strong><br>
    <a href="{_tos_url}" target="_blank" style="color:#4f8ff0; text-decoration:none;">Terms of Service</a>
    &nbsp;·&nbsp;
    <a href="{_priv_url}" target="_blank" style="color:#4f8ff0; text-decoration:none;">Privacy Policy</a>
    &nbsp;·&nbsp;
    <span style="color:#8492a6;">© 2026 Sideline</span>
</div>
""",
    unsafe_allow_html=True,
)