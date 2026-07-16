"""
NFL Chatbot UI — Production Version
Improvements:
  - Streaming responses via st.write_stream()
  - Richer session state (last_player + last_team tracked separately)
  - Config error surfaced clearly with setup instructions
"""

import os
import streamlit as st
from dotenv import load_dotenv
from streamlit_mic_recorder import speech_to_text
from src.chatbot import nfl_chatbot_with_context
from src.api_client import ensure_team_cache, _TEAM_CACHE

load_dotenv()

# -------------------------------------------------------
# Page Config
# -------------------------------------------------------
st.set_page_config(
    page_title="NFL Pro-Bot",
    page_icon="🏈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# -------------------------------------------------------
# Session State Initialisation
# -------------------------------------------------------
if "messages" not in st.session_state:
    st.session_state.messages = []

# Improvement #3 — track player and team separately; #7 — conversation state
for key in ("last_mentioned", "last_player", "last_team"):
    if key not in st.session_state:
        st.session_state[key] = None
if "conv_state" not in st.session_state:
    st.session_state["conv_state"] = {}

# -------------------------------------------------------
# Sidebar
# -------------------------------------------------------
ensure_team_cache()
TEAM_NAMES = sorted({
    meta.get("displayName")
    for meta in _TEAM_CACHE.values()
    if meta.get("displayName")
})

with st.sidebar:
    st.title("🏈 Pro-Bot Tools")

    # Show what the bot is currently focused on
    focus_parts = []
    if st.session_state["last_player"]:
        focus_parts.append(f"👤 {st.session_state['last_player'].title()}")
    if st.session_state["last_team"]:
        focus_parts.append(f"🏟️ {st.session_state['last_team'].title()}")
    if focus_parts:
        st.info("💬 **Focusing on:** " + "  |  ".join(focus_parts))

    st.divider()

    action = st.selectbox(
        "Quick Research",
        ["Chat Mode", "Scores", "Standings", "Team News", "Next Game",
         "Last Game", "Fantasy Stats", "Injury Report", "Compare Players",
         "Trade Advice", "Waiver Wire"]
    )

    sidebar_prompt = None

    if action in ["Standings", "Team News", "Next Game", "Last Game"]:
        team_choice = st.selectbox("Select Team", TEAM_NAMES)
        if st.button(f"Get {action}"):
            sidebar_prompt = f"How are the {team_choice} looking in the {action.lower()}?"

    elif action == "Scores":
        if st.button("Refresh Scoreboard"):
            sidebar_prompt = "What are the latest scores from today's games?"

    elif action == "Fantasy Stats":
        p_name = st.text_input("Player Name", key="fantasy_input")
        if st.button("Search Fantasy") and p_name:
            sidebar_prompt = f"Can you give me a fantasy breakdown for {p_name}?"

    elif action == "Injury Report":
        p_name = st.text_input("Player Name", key="injury_input")
        if st.button("Check Injury") and p_name:
            sidebar_prompt = f"What is the injury status for {p_name}?"

    elif action == "Compare Players":
        p1 = st.text_input("Player 1", key="compare_p1")
        p2 = st.text_input("Player 2", key="compare_p2")
        if st.button("Compare") and p1 and p2:
            sidebar_prompt = f"Compare {p1} vs {p2}"

    elif action == "Trade Advice":
        p_give = st.text_input("Player you're giving", key="trade_give")
        p_get  = st.text_input("Player you're getting", key="trade_get")
        if st.button("Analyse Trade") and p_give and p_get:
            sidebar_prompt = f"Should I trade {p_give} for {p_get}?"

    elif action == "Waiver Wire":
        pos_filter = st.selectbox("Position (optional)", ["All", "QB", "RB", "WR", "TE"],
                                  key="waiver_pos")
        if st.button("Get Waiver Targets"):
            if pos_filter == "All":
                sidebar_prompt = "Who are the best waiver wire pickups right now?"
            else:
                sidebar_prompt = f"Who are the best {pos_filter} waiver wire pickups right now?"

    st.divider()
    if st.button("Clear Conversation"):
        st.session_state.messages = []
        st.session_state["last_mentioned"] = None
        st.session_state["last_player"] = None
        st.session_state["last_team"] = None
        st.session_state["conv_state"] = {}
        st.rerun()

# -------------------------------------------------------
# Main Chat Interface
# -------------------------------------------------------
st.title("🏈 NFL AI Assistant")
st.caption("Ask me anything about the NFL — scores, stats, injuries, fantasy, and more.")

# Render chat history
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# -------------------------------------------------------
# Input Handling — text OR voice
# -------------------------------------------------------

# Voice input — renders a mic button; returns transcribed text when done
voice_input = speech_to_text(
    language="en",
    start_prompt="🎙️ Speak",
    stop_prompt="⏹️ Stop",
    just_once=True,           # auto-clears after one recording
    use_container_width=False,
    key="voice_input",
)

user_input = st.chat_input(
    "Try: 'Is Josh Allen playing Sunday?' or 'Should I start Tyreek Hill?'"
)

# Voice takes priority if the user just recorded; sidebar prompt takes priority
# over both since it's an explicit button click
final_query = sidebar_prompt or voice_input or user_input

if final_query:
    st.session_state.messages.append({"role": "user", "content": final_query})
    with st.chat_message("user"):
        st.markdown(final_query)

    with st.chat_message("assistant"):

        # Run the pipeline — returns a generator, a disambiguation dict, or an error string
        with st.spinner("Thinking..."):
            response = nfl_chatbot_with_context(final_query)

        # ---- Config error ----
        if isinstance(response, str) and response.startswith("__CONFIG_ERROR__"):
            error_msg = (
                "⚠️ **Gemini API key not configured.**\n\n"
                "To enable the AI assistant:\n"
                "1. Get a free key at [Google AI Studio](https://aistudio.google.com/app/apikey)\n"
                "2. Copy `template.env` to `.env` and add your key\n"
                "3. Restart the app"
            )
            st.error(error_msg)
            st.session_state.messages.append({"role": "assistant", "content": error_msg})

        # ---- Disambiguation buttons ----
        elif isinstance(response, dict) and response.get("type") == "selection_required":
            player_list = response.get("matches", [])

            if player_list:
                disambiguation_msg = response.get(
                    "message", "I found a few players with that name. Who did you mean?"
                )
                st.write(disambiguation_msg)
                st.session_state.messages.append(
                    {"role": "assistant", "content": disambiguation_msg}
                )

                cols = st.columns(len(player_list))
                for idx, p in enumerate(player_list):
                    p_id = p.get("player_id") or p.get("id")
                    btn_label = f"**{p['full_name']}**\n({p.get('team', 'FA')} - {p['position']})"
                    if cols[idx].button(btn_label, key=f"sel_{p_id}"):
                        st.session_state["last_player"] = p["full_name"]
                        st.session_state["last_mentioned"] = p["full_name"]
                        st.session_state.messages.append({
                            "role": "user",
                            "content": f"Show me the profile for {p['full_name']} on the {p.get('team')}"
                        })
                        st.rerun()
            else:
                fallback_msg = (
                    "I found multiple matches but had trouble loading the details. "
                    "Try adding the team name to your search!"
                )
                st.warning(fallback_msg)
                st.session_state.messages.append({"role": "assistant", "content": fallback_msg})

        # ---- Improvement #1: Streaming response ----
        elif hasattr(response, "__iter__") and not isinstance(response, str):
            # st.write_stream consumes the generator and renders tokens live,
            # then returns the full assembled string for history storage.
            full_text = st.write_stream(response)
            st.session_state.messages.append({"role": "assistant", "content": full_text})

        # ---- Plain string fallback (e.g. API error message) ----
        else:
            st.markdown(response)
            st.session_state.messages.append({"role": "assistant", "content": response})
