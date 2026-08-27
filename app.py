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
import streamlit as st
from dotenv import load_dotenv
from streamlit_mic_recorder import speech_to_text

from src.chatbot import nfl_chatbot_with_context

load_dotenv()

# ------------------------------------------------------------------
# Local profile persistence — favorite team/player survive app
# restarts, not just the current browser session. Deliberately simple
# (a small JSON file next to the user's home dir) since this is a
# single-user local app, not something needing a real database.
# ------------------------------------------------------------------
_PREFS_PATH = os.path.join(os.path.expanduser("~"), ".nfl_chatbot_prefs.json")

def _load_prefs() -> dict:
    try:
        with open(_PREFS_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_prefs(prefs: dict) -> None:
    try:
        with open(_PREFS_PATH, "w") as f:
            json.dump(prefs, f)
    except Exception:
        pass  # non-fatal — profile just won't persist across restarts

# ------------------------------------------------------------------
# Page Configuration
# ------------------------------------------------------------------
st.set_page_config(
    page_title="NFL Pro-Bot",
    page_icon="🏈",
    layout="wide",
    initial_sidebar_state="collapsed",  # collapsed by default — mobile-first
)

# ------------------------------------------------------------------
# Custom Styling
# ------------------------------------------------------------------
st.markdown("""
<style>
    #MainMenu, footer, header {visibility: hidden;}

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
        color: #5c6b7e;
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

    .empty-state {
        text-align: center;
        padding: 36px 20px 16px 20px;
        color: #7c8ba0;
        font-size: 14.5px;
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

        /* Timestamps slightly lighter on small screens — contrast ok at this size */
        .msg-time { color: #6b7d93; }
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
if "onboarding_done" not in st.session_state:
    st.session_state["onboarding_done"] = False

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
                   font-size:20px;">Welcome to NFL Pro-Bot</h2>
        <p style="text-align:center; color:#8ea0b5; font-size:13.5px;
                  margin:0 0 24px 0;">
            AI-powered NFL data — live scores, injuries, fantasy stats, and more.
        </p>
        <div style="background:#0d1420; border-radius:8px; padding:14px 16px;
                    font-size:13px; color:#8ea0b5; margin-bottom:20px;
                    line-height:1.6;">
            <strong style="color:#c8d6e5;">Before you continue:</strong><br>
            • Responses are AI-generated and may be inaccurate or delayed.<br>
            • Do not use this App for sports betting or high-stakes fantasy decisions.<br>
            • No personal data is collected. Chat history lives only in your browser session.<br>
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
        f"By continuing you agree to the "
        f"<a href='{_tos_url}' target='_blank' style='color:#4f8ff0;'>Terms of Service</a> and "
        f"<a href='{_priv_url}' target='_blank' style='color:#4f8ff0;'>Privacy Policy</a>."
        if _repo else
        "By continuing you agree to the Terms of Service and Privacy Policy."
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
            f"<p style='text-align:center; font-size:12.5px; color:#5c6b7e; margin-bottom:8px;'>"
            f"{_legal_links}</p>",
            unsafe_allow_html=True,
        )
        if st.button("✅ I agree — let's go", use_container_width=True, type="primary"):
            st.session_state["terms_accepted"] = True
            st.rerun()
    st.stop()  # Render nothing else until accepted

# ------------------------------------------------------------------
# Task 8 — First-run onboarding panel (once per session only)
# ------------------------------------------------------------------
if not st.session_state["onboarding_done"]:
    st.markdown("""
    <div style="max-width:600px; margin:0 auto 18px auto; background:#1a2636;
                border:1px solid #2f6fed; border-radius:12px; padding:20px 24px;">
        <div style="font-size:15px; font-weight:600; color:#f4f6f8; margin-bottom:10px;">
            👋 Here's what NFL Pro-Bot can do
        </div>
        <div style="font-size:13.5px; color:#8ea0b5; line-height:1.8;">
            💬 <strong style="color:#c8d6e5;">Ask anything in the chat</strong> —
            scores, standings, injuries, fantasy advice, player comparisons.<br>
            📋 <strong style="color:#c8d6e5;">Use the sidebar</strong> (☰ top-left)
            for one-click team briefings, fantasy tools, and waiver wire.<br>
            ⭐ <strong style="color:#c8d6e5;">Set a favourite team</strong> in
            "My Profile" for a personalised daily update with one tap.
        </div>
    </div>
    """, unsafe_allow_html=True)
    _, btn_col, skip_col, _ = st.columns([2, 2, 1, 2])
    with btn_col:
        if st.button("✅ Got it — show me the app", use_container_width=True, type="primary"):
            st.session_state["onboarding_done"] = True
            st.rerun()
    with skip_col:
        if st.button("Skip", use_container_width=True):
            st.session_state["onboarding_done"] = True
            st.rerun()

THINKING_MESSAGES = [
    "Checking the box score...",
    "Pulling the latest from the league office...",
    "Cross-referencing the depth chart...",
    "Digging through the play-by-play...",
]

def _typewriter(chunk_generator, delay: float = 0.02):
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

EXAMPLE_PROMPTS = [
    "How did the Eagles do today?",
    "Tell me about Josh Allen",
    "What are Bills fantasy stats this week?",
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

def team_logo_url(display_name: str) -> str:
    meta = _TEAM_LOOKUP.get(display_name or "")
    abbr = (meta or {}).get("abbr", "")
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png" if abbr else ""

# ------------------------------------------------------------------
# Sidebar: One-Click Quick Actions
# ------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🏈 Pro-Bot Tools")

    # Voice input lives here now instead of the main chat area — keeps
    # the chat surface focused purely on the conversation itself.
    voice_input = speech_to_text(
        language="en",
        start_prompt="🎙️ Tap to speak",
        stop_prompt="⏹️ Stop recording",
        just_once=True,           # auto-clears after one recording, so it
                                   # won't keep resubmitting on reruns
        use_container_width=True,
        key="voice_input",
    )

    if st.session_state["last_mentioned"]:
        st.info(f"💬 Focused on **{st.session_state['last_mentioned'].title()}**")

    sidebar_prompt = None

    # ------------------------------------------------------------
    # My Profile — set once, remembered across restarts. Separate
    # from the Quick Lookup dropdown below, which is for browsing
    # *any* team on demand rather than personalizing to "my team".
    # ------------------------------------------------------------
    st.divider()
    st.caption("MY PROFILE")

    profile = st.session_state["profile"]
    fav_team = profile.get("team")
    fav_player = profile.get("player")

    if fav_team or fav_player:
        if fav_team:
            flog = team_logo_url(fav_team)
            pcol, tcol = st.columns([1, 3])
            with pcol:
                if flog:
                    # Task 5 — alt text for accessibility
                    st.markdown(
                        f'<img src="{flog}" width="36" alt="{fav_team} logo" style="display:block;">',
                        unsafe_allow_html=True,
                    )
            with tcol:
                st.markdown(f"**{fav_team}**")
        if fav_player:
            st.caption(f"⭐ {fav_player}")

        if st.button("🔔 Get My Updates", use_container_width=True, type="primary"):
            asks = []
            if fav_team:
                asks.append(
                    f"For the {fav_team}: how they did in their last game, when "
                    f"their next game is, the latest news, and where they stand "
                    f"in the standings."
                )
            if fav_player:
                asks.append(
                    f"For {fav_player}: their latest stats, fantasy outlook, and "
                    f"injury status."
                )
            asks.append("Also give me the biggest storylines around the league right now.")
            sidebar_prompt = "Give me my personalized update. " + " ".join(asks)

        with st.expander("Edit profile"):
            options = ["(none)"] + TEAM_NAMES
            new_team = st.selectbox(
                "Favorite team", options,
                index=options.index(fav_team) if fav_team in options else 0,
                key="profile_team_edit",
            )
            new_player = st.text_input("Favorite player", value=fav_player or "",
                                        key="profile_player_edit")
            if st.button("Save", key="save_profile_edit", use_container_width=True):
                st.session_state["profile"] = {
                    "team": None if new_team == "(none)" else new_team,
                    "player": new_player.strip() or None,
                }
                _save_prefs(st.session_state["profile"])
                st.rerun()
    else:
        st.caption("Set a favorite team or player for one-click personalized updates.")
        new_team = st.selectbox("Favorite team", ["(none)"] + TEAM_NAMES, key="profile_team_setup")
        new_player = st.text_input("Favorite player (optional)", placeholder="e.g. Josh Allen",
                                    key="profile_player_setup")
        if st.button("Save Profile", use_container_width=True):
            st.session_state["profile"] = {
                "team": None if new_team == "(none)" else new_team,
                "player": new_player.strip() or None,
            }
            _save_prefs(st.session_state["profile"])
            st.rerun()

    st.divider()
    st.caption("QUICK LOOKUP")

    team_choice = st.selectbox("Team", TEAM_NAMES, label_visibility="collapsed",
                                placeholder="Choose a team")
    logo = team_logo_url(team_choice)
    if logo:
        # Task 5 — alt text for accessibility
        st.markdown(
            f'<img src="{logo}" width="64" alt="{team_choice} logo" style="display:block; margin-bottom:6px;">',
            unsafe_allow_html=True,
        )

    # The one clear default action — everything else is one click away
    # inside the expanders below, not competing for attention up front.
    if st.button("📋 Daily Briefing", use_container_width=True, type="primary"):
        sidebar_prompt = (
            f"Give me a quick daily briefing for the {team_choice}: how they did "
            f"in their last game, when their next game is, the latest news, and "
            f"where they stand in the division. Also give me the biggest "
            f"storylines around the league right now."
        )

    with st.expander(f"More for the {team_choice.split()[-1]}"):
        c1, c2 = st.columns(2)
        if c1.button("📊 Standings", use_container_width=True):
            sidebar_prompt = f"How are the {team_choice} looking in the standings?"
        if c2.button("📰 News", use_container_width=True):
            sidebar_prompt = f"What's the latest news for the {team_choice}?"
        c3, c4 = st.columns(2)
        if c3.button("⏭️ Next Game", use_container_width=True):
            sidebar_prompt = f"When is the next game for the {team_choice}?"
        if c4.button("⏮️ Last Game", use_container_width=True):
            sidebar_prompt = f"How did the {team_choice} do in their last game?"
        c5, c6 = st.columns(2)
        if c5.button("🔴 Live Scores", use_container_width=True):
            sidebar_prompt = "What are the latest scores from today's games?"
        if c6.button("🌎 League News", use_container_width=True):
            sidebar_prompt = "What are the biggest storylines around the NFL right now?"

    with st.expander("🏆 Fantasy Tools"):
        st.caption("PLAYER LOOKUP")
        p_name = st.text_input("Player name", label_visibility="collapsed",
                                placeholder="Player name, e.g. CeeDee Lamb")
        fc1, fc2 = st.columns(2)
        if fc1.button("💰 Fantasy", use_container_width=True) and p_name:
            sidebar_prompt = f"Can you give me a fantasy breakdown for {p_name}?"
        if fc2.button("🏥 Injury", use_container_width=True) and p_name:
            sidebar_prompt = f"What is the injury status for {p_name}?"

        st.caption("COMPARE & TRADE")
        p1 = st.text_input("Player 1", label_visibility="collapsed",
                            placeholder="Player 1", key="cmp_p1")
        p2 = st.text_input("Player 2", label_visibility="collapsed",
                            placeholder="Player 2", key="cmp_p2")
        cc1, cc2 = st.columns(2)
        if cc1.button("⚔️ Compare", use_container_width=True) and p1 and p2:
            sidebar_prompt = f"Compare {p1} vs {p2}"
        if cc2.button("🔄 Trade", use_container_width=True) and p1 and p2:
            sidebar_prompt = f"Should I trade {p1} for {p2}?"

        st.caption("WAIVER WIRE")
        waiver_pos = st.selectbox("Position", ["Any", "QB", "RB", "WR", "TE"],
                                   label_visibility="collapsed", key="waiver_pos")
        if st.button("Waiver Targets", use_container_width=True):
            sidebar_prompt = (
                "Who are the best waiver wire pickups right now?"
                if waiver_pos == "Any"
                else f"Who are the best {waiver_pos} waiver wire pickups right now?"
            )

    st.divider()
    # Task 9 — Export Chat button
    _has_msgs = len(st.session_state.messages) > 0
    if _has_msgs:
        _export_lines = []
        for _m in st.session_state.messages:
            _role = "You" if _m["role"] == "user" else "NFL Pro-Bot"
            _ts   = _m.get("time", "")
            _prefix = f"[{_ts}] {_role}:" if _ts else f"{_role}:"
            _export_lines.append(f"{_prefix}\n{_m['content']}\n")
        _export_str = "\n".join(_export_lines)
        _export_name = f"nfl-probot-chat-{datetime.date.today()}.txt"
        st.download_button(
            label="📥 Export Chat",
            data=_export_str,
            file_name=_export_name,
            mime="text/plain",
            use_container_width=True,
        )
    else:
        st.button("📥 Export Chat", disabled=True, use_container_width=True,
                  help="Nothing to export yet — start a conversation first.")
    if st.button("🗑️ Clear Conversation", use_container_width=True):
        st.session_state.messages = []
        st.session_state["last_mentioned"] = None
        st.rerun()

# ------------------------------------------------------------------
# Header
# ------------------------------------------------------------------
st.markdown("""
<div class="hero">
    <div class="badge">🏈</div>
    <div>
        <h1>NFL AI Assistant</h1>
        <p>Live scores, news, standings, and fantasy stats — just ask.</p>
        <p style="font-size:11.5px; color:#5c6b7e; margin-top:3px;">
            ⚠️ AI-generated — verify before acting. Not for betting.
            Data: ESPN · Sleeper · RSS feeds.
        </p>
    </div>
</div>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------
# Empty State (first visit) — clickable example prompts
# ------------------------------------------------------------------
example_prompt = None
if not st.session_state.messages:
    st.markdown("""
    <div class="empty-state">
        <div>Ask about scores, standings, news, schedules, or fantasy stats.</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="chip-row">', unsafe_allow_html=True)
    cols = st.columns(len(EXAMPLE_PROMPTS))
    for i, prompt in enumerate(EXAMPLE_PROMPTS):
        if cols[i].button(prompt, key=f"ex_{i}", use_container_width=True):
            example_prompt = prompt
    st.markdown('</div>', unsafe_allow_html=True)

# ------------------------------------------------------------------
# Chat History
# ------------------------------------------------------------------
for message in st.session_state.messages:
    avatar = "🏈" if message["role"] == "assistant" else "🙋"
    with st.chat_message(message["role"], avatar=avatar):
        st.markdown(message["content"])
        if ts := message.get("time"):
            st.markdown(f'<div class="msg-time">{ts}</div>', unsafe_allow_html=True)

# ------------------------------------------------------------------
# Input Handling — text (voice input lives in the sidebar now)
# ------------------------------------------------------------------
user_input = st.chat_input("Ex: 'How did the Giants do today?' or 'Tell me about Josh Allen'")

final_query = sidebar_prompt or example_prompt or voice_input or user_input

if final_query:
    now = datetime.datetime.now().strftime("%I:%M %p")
    st.session_state.messages.append({"role": "user", "content": final_query, "time": now})
    with st.chat_message("user", avatar="🙋"):
        st.markdown(final_query)
        st.markdown(f'<div class="msg-time">{now}</div>', unsafe_allow_html=True)

    with st.chat_message("assistant", avatar="🏈"):
        with st.status(random.choice(THINKING_MESSAGES), expanded=False) as status:
            # Intent extraction + data fetching happen here (blocking).
            # For normal replies this returns a *generator* — actual Gemini
            # formatting/streaming is lazy and hasn't started yet, so this
            # status only covers "gathering data", not "writing the answer".
            response = nfl_chatbot_with_context(final_query)
            status.update(label="Done", state="complete")

        reply_time = datetime.datetime.now().strftime("%I:%M %p")

        # --- Streaming text response (the normal case) ---
        if hasattr(response, "__iter__") and not isinstance(response, (str, dict, list)):
            try:
                first_chunk = next(response)
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

            elif isinstance(first_chunk, str) and first_chunk.startswith("__API_ERROR__"):
                error_msg = "⚠️ I'm having trouble reaching Gemini right now. Please try again in a moment."
                st.error(error_msg)
                st.session_state.messages.append({"role": "assistant", "content": error_msg, "time": reply_time})

            else:
                full_response = st.write_stream(
                    _typewriter(itertools.chain([first_chunk], response))
                )
                st.markdown(f'<div class="msg-time">{reply_time}</div>', unsafe_allow_html=True)
                st.session_state.messages.append({"role": "assistant", "content": full_response, "time": reply_time})

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
                st.write(disambiguation_msg)
                st.session_state.messages.append({"role": "assistant", "content": disambiguation_msg, "time": reply_time})

                cols = st.columns(len(player_list))
                for idx, p in enumerate(player_list):
                    p_id = p.get("player_id") or p.get("id")
                    with cols[idx]:
                        logo = team_logo_url(p.get("team", ""))
                        safe_name = html.escape(str(p.get("full_name", "Unknown")))
                        safe_team = html.escape(str(p.get("team") or "FA"))
                        safe_pos  = html.escape(str(p.get("position", "")))
                        st.markdown(
                            f'<div class="player-card">'
                            f'<div class="pname">{safe_name}</div>'
                            f'<div class="pmeta">{safe_team} · {safe_pos}</div>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                        if logo:
                            st.image(logo, width=40)
                        if st.button("Select", key=f"sel_{p_id}", use_container_width=True):
                            st.session_state["last_mentioned"] = p["full_name"]
                            st.session_state.messages.append({
                                "role": "user",
                                "content": f"Show me the profile for {p['full_name']} on the {p.get('team')}",
                                "time": datetime.datetime.now().strftime("%I:%M %p"),
                            })
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
<div style="text-align:center; font-size:12px; color:#4a5568; padding:8px 0 16px 0; line-height:2;">
    NFL Pro-Bot is an independent fan tool — not affiliated with the NFL, ESPN, or Sleeper.<br>
    Responses are AI-generated and may be inaccurate. Not for use in sports betting.<br>
    Data sourced from <strong>ESPN</strong> · <strong>Sleeper</strong> · <strong>Yahoo Sports</strong> · <strong>NBC Sports PFT</strong><br>
    <a href="{_tos_url}" target="_blank" style="color:#4f8ff0; text-decoration:none;">Terms of Service</a>
    &nbsp;·&nbsp;
    <a href="{_priv_url}" target="_blank" style="color:#4f8ff0; text-decoration:none;">Privacy Policy</a>
    &nbsp;·&nbsp;
    <span style="color:#4a5568;">© 2026 NFL Pro-Bot</span>
</div>
""",
    unsafe_allow_html=True,
)