import streamlit as st
from api_client import ensure_team_cache, _TEAM_CACHE, nfl_chatbot_with_context

# 1. Page Configuration
st.set_page_config(page_title="NFL Chatbot", page_icon="🏈", layout="wide")

# 2. State Initialization
# message history for the UI
if "messages" not in st.session_state:
    st.session_state.messages = []

# contextual memory for turn-based subject tracking
if "last_mentioned" not in st.session_state:
    st.session_state["last_mentioned"] = None

# 3. Load Team Data for Sidebar
ensure_team_cache()
TEAM_NAMES = sorted(list({meta.get("displayName") for meta in _TEAM_CACHE.values() if meta.get("displayName")}))

# 4. Sidebar Quick Actions
st.sidebar.header("🏈 Quick Actions")

action = st.sidebar.selectbox(
    "Select an Action",
    [
        "Chat",
        "Get Live Scores",
        "Standings",
        "Team News",
        "Next Game",
        "Last Game",
        "Fantasy Stats",
        "Player Profile"
    ]
)

# Sidebar input logic
prompt_from_sidebar = None

# Team-based actions
if action in ["Standings", "Team News", "Next Game", "Last Game"]:
    team_selection = st.sidebar.selectbox("Select Team", TEAM_NAMES)
    if st.sidebar.button("Run Action"):
        if action == "Standings": prompt_from_sidebar = f"{team_selection} standings"
        elif action == "Team News": prompt_from_sidebar = f"{team_selection} news"
        elif action == "Next Game": prompt_from_sidebar = f"next game for {team_selection}"
        elif action == "Last Game": prompt_from_sidebar = f"last game for {team_selection}"

# Scoreboard (Global)
elif action == "Get Live Scores":
    if st.sidebar.button("Run Action"):
        prompt_from_sidebar = "scores"

# Player-based actions
elif action in ["Fantasy Stats", "Player Profile"]:
    player_input = st.sidebar.text_input("Enter Player Name")
    if st.sidebar.button("Run Action") and player_input:
        if action == "Fantasy Stats": prompt_from_sidebar = f"fantasy stats for {player_input}"
        elif action == "Player Profile": prompt_from_sidebar = f"who is {player_input}"

# 5. Main Chat Interface
st.title("🏈 NFL Chatbot")
st.markdown("---")

# Display existing conversation history
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# 6. User Input Handling
user_prompt = st.chat_input("Ask me about players, teams, or games...")

# Decide which prompt to process (Sidebar vs Chat Input)
final_prompt = prompt_from_sidebar or user_prompt

if final_prompt:
    # A. Display user message
    st.session_state.messages.append({"role": "user", "content": final_prompt})
    with st.chat_message("user"):
        st.markdown(final_prompt)

    with st.spinner("Analyzing NFL data..."):
        response = nfl_chatbot_with_context(final_prompt)

    # UI LOGIC: Handle Multi-Match Selection
    if isinstance(response, dict) and response.get("type") == "selection_required":
        with st.chat_message("assistant"):
            st.write("I found multiple players. Which one did you mean?")
            
            # Use columns for a clean button layout
            cols = st.columns(len(response["matches"]))
            for idx, p in enumerate(response["matches"]):
                label = f"{p['full_name']}\n({p['position']} - {p.get('team', 'FA')})"
                
                if cols[idx].button(label, key=f"sel_{p['id']}"):
                    # 1. Lock in the NAME only (Josh Allen), not the whole metadata
                    st.session_state["last_mentioned"] = p['full_name']
                    
                    # 2. Add a specialized system prompt to history to show it worked
                    st.session_state.messages.append({"role": "assistant", "content": f"Locked in: **{p['full_name']}**. How can I help with them?"})
                    
                    # 3. Optional: Automatically show their profile now that context is set
                    # This prevents the "I get nothing" error
                    st.session_state["messages"].append({"role": "user", "content": f"Who is {p['full_name']}"})
                    
                    st.rerun()
    else:
        # Standard text response
        with st.chat_message("assistant"):
            st.markdown(response)
        st.session_state.messages.append({"role": "assistant", "content": response})

        
# 7. Sidebar Subject Indicator (Optional)
if st.session_state["last_mentioned"]:
    st.sidebar.markdown("---")
    st.sidebar.write(f"💬 **Current Subject:** {st.session_state['last_mentioned'].title()}")
