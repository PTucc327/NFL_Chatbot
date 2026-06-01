"""
NFL Chatbot UI (Professional Conversational Version)
Built with Streamlit to simulate a "thinking" AI assistant.
"""

import os
import streamlit as st
from dotenv import load_dotenv
from src.chatbot import nfl_chatbot_with_context
from src.api_client import ensure_team_cache, _TEAM_CACHE

# Load environment variables from .env if present
load_dotenv()

# 1. Professional Page Configuration
st.set_page_config(
    page_title="NFL Pro-Bot",
    page_icon="🏈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 2. State Initialization
if "messages" not in st.session_state:
    st.session_state.messages = []

if "last_mentioned" not in st.session_state:
    st.session_state["last_mentioned"] = None

# 3. Sidebar: Quick Actions & Context
ensure_team_cache()
TEAM_NAMES = sorted(list({
    meta.get("displayName") for meta in _TEAM_CACHE.values() 
    if meta.get("displayName")
}))

with st.sidebar:
    st.title("🏈 Pro-Bot Tools")
    
    if st.session_state["last_mentioned"]:
        st.info(f"💬 **Focusing on:** {st.session_state['last_mentioned'].title()}")
    
    st.divider()
    
    action = st.selectbox(
        "Quick Research",
        ["Chat Mode", "Scores", "Standings", "Team News", "Next Game", "Fantasy Stats"]
    )

    sidebar_prompt = None
    
    if action in ["Standings", "Team News", "Next Game"]:
        team_choice = st.selectbox("Select Team", TEAM_NAMES)
        if st.button(f"Get {action}"):
            sidebar_prompt = f"How are the {team_choice} looking in the {action.lower()}?"
            
    elif action == "Scores":
        if st.button("Refresh Scoreboard"):
            sidebar_prompt = "What are the latest scores from today's games?"
            
    elif action == "Fantasy Stats":
        p_name = st.text_input("Player Name")
        if st.button("Search Fantasy") and p_name:
            sidebar_prompt = f"Can you give me a fantasy breakdown for {p_name}?"

    st.divider()
    if st.button("Clear Conversation"):
        st.session_state.messages = []
        st.session_state["last_mentioned"] = None
        st.rerun()

# 4. Main Chat Interface
st.title("🏈 NFL AI Assistant")
st.caption("Ask me anything about the NFL—I'll handle the data for you.")

# Display Chat History
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# 5. Input Handling
user_input = st.chat_input("Ex: 'How did the Giants do today?' or 'Tell me about Josh Allen'")
final_query = sidebar_prompt or user_input

if final_query:
    st.session_state.messages.append({"role": "user", "content": final_query})
    with st.chat_message("user"):
        st.markdown(final_query)

    with st.chat_message("assistant"):
        with st.status("Thinking...", expanded=False) as status:
            st.write("Searching NFL databases...")
            response = nfl_chatbot_with_context(final_query)
            status.update(label="Analysis Complete!", state="complete")

        # --- Handle missing API key ---
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

        # --- Handle Disambiguation Buttons ---
        elif isinstance(response, dict) and response.get("type") == "selection_required":
            player_list = response.get("matches", [])

            if player_list:
                disambiguation_msg = response.get("message", "I found a few players with that name. Who did you mean?")
                st.write(disambiguation_msg)
                # Save to history so it appears correctly on re-render
                st.session_state.messages.append({"role": "assistant", "content": disambiguation_msg})

                cols = st.columns(len(player_list))
                for idx, p in enumerate(player_list):
                    p_id = p.get("player_id") or p.get("id")
                    btn_label = f"**{p['full_name']}**\n({p.get('team', 'FA')} - {p['position']})"

                    if cols[idx].button(btn_label, key=f"sel_{p_id}"):
                        st.session_state["last_mentioned"] = p['full_name']
                        st.session_state.messages.append({
                            "role": "user",
                            "content": f"Show me the profile for {p['full_name']} on the {p.get('team')}"
                        })
                        st.rerun()
            else:
                fallback_msg = "I found multiple matches but had trouble loading the details. Try adding the team name to your search!"
                st.warning(fallback_msg)
                st.session_state.messages.append({"role": "assistant", "content": fallback_msg})

        # --- Standard conversational response ---
        else:
            st.markdown(response)
            st.session_state.messages.append({"role": "assistant", "content": response})