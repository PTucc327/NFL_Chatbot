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
import time
import random
import datetime
import itertools
import streamlit as st
from dotenv import load_dotenv

from src.chatbot import nfl_chatbot_with_context
from src.api_client import ensure_team_cache, _TEAM_CACHE

load_dotenv()

# ------------------------------------------------------------------
# Page Configuration
# ------------------------------------------------------------------
st.set_page_config(
    page_title="NFL Pro-Bot",
    page_icon="🏈",
    layout="wide",
    initial_sidebar_state="expanded",
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
        padding: 60px 20px 20px 20px;
        color: #7c8ba0;
    }
    .empty-state .icon { font-size: 46px; margin-bottom: 10px; }
</style>
""", unsafe_allow_html=True)

# ------------------------------------------------------------------
# State Initialization
# ------------------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []
if "last_mentioned" not in st.session_state:
    st.session_state["last_mentioned"] = None

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
    "Who's leading the AFC East?",
]

# ------------------------------------------------------------------
# Team Cache + Logo Helper
# ------------------------------------------------------------------
ensure_team_cache()
TEAM_NAMES = sorted({
    meta.get("displayName") for meta in _TEAM_CACHE.values()
    if meta.get("displayName")
})

def team_logo_url(display_name: str) -> str:
    meta = _TEAM_CACHE.get((display_name or "").lower())
    abbr = (meta or {}).get("abbr", "")
    return f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr}.png" if abbr else ""

# ------------------------------------------------------------------
# Sidebar: One-Click Quick Actions
# ------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🏈 Pro-Bot Tools")

    if st.session_state["last_mentioned"]:
        st.info(f"💬 Focused on **{st.session_state['last_mentioned'].title()}**")

    st.divider()
    st.caption("QUICK LOOKUP")

    team_choice = st.selectbox("Team", TEAM_NAMES, label_visibility="collapsed",
                                placeholder="Choose a team")
    logo = team_logo_url(team_choice)
    if logo:
        st.image(logo, width=64)

    sidebar_prompt = None
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

    if st.button("🔴 Refresh Live Scores", use_container_width=True):
        sidebar_prompt = "What are the latest scores from today's games?"

    st.divider()
    st.caption("FANTASY")
    p_name = st.text_input("Player name", label_visibility="collapsed",
                            placeholder="Player name, e.g. CeeDee Lamb")
    if st.button("💰 Fantasy Breakdown", use_container_width=True) and p_name:
        sidebar_prompt = f"Can you give me a fantasy breakdown for {p_name}?"

    st.divider()
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
        <div class="icon">🎙️</div>
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
# Input Handling
# ------------------------------------------------------------------
user_input = st.chat_input("Ex: 'How did the Giants do today?' or 'Tell me about Josh Allen'")
final_query = sidebar_prompt or example_prompt or user_input

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
                        st.markdown(
                            f'<div class="player-card">'
                            f'<div class="pname">{p["full_name"]}</div>'
                            f'<div class="pmeta">{p.get("team", "FA")} · {p["position"]}</div>'
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