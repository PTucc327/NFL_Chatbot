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
