"""
NFL Chatbot Router (Conversational Version)
Handles intent recognition, entity extraction, and stateful orchestration.
"""

import re
import logging
from typing import Optional
from src.utils import clean_query
from src.api_client import (
    get_live_scores,
    get_standings,
    get_next_game,
    get_last_game,
    get_team_news,
    get_player_profile_smart,
    get_fantasy_player_stats,
    get_game_odds,
    find_team,
    resolve_contextual_query,
    detect_team_from_query,
    _normalize_player_query
)

logger = logging.getLogger(__name__)

# -------------------------------------
# Intent & Entity Logic
# -------------------------------------

def extract_team_advanced(text: str) -> Optional[str]:
    """
    Scans query for known team names/abbreviations.
    """
    detected = detect_team_from_query(text)
    if detected:
        return detected
        
    # Fallback: simple extraction of the last significant word
    words = text.strip().split()
    return words[-1] if words else None

# -------------------------------------
# Main Orchestration
# -------------------------------------

def nfl_chatbot_with_context(user_input: str):
    import streamlit as st 
    
    # 1. Retrieve memory
    last_subject = st.session_state.get("last_mentioned")
    
    # 2. Contextual Resolution (e.g., 'how are they doing?' -> 'Cowboys standings')
    resolved_query = resolve_contextual_query(user_input, last_subject)
    logger.info(f"Original: {user_input} | Resolved: {resolved_query}")
    
    # 3. Process Query
    response = handle_user_query(resolved_query)
    
    # 4. Subject Update Guard
    new_entity = detect_team_from_query(user_input) or _normalize_player_query(user_input)
    
    # Don't let generic action words become the 'subject'
    forbidden_subjects = {"news", "stats", "scores", "standing", "fantasy", "odds", "game", "updates", "latest"}
    if new_entity and new_entity.lower() not in forbidden_subjects:
        st.session_state["last_mentioned"] = new_entity
        
    return response

def handle_user_query(q: str):
    q_low = q.lower().strip()

    # Intent: Scores & Results (Human-like: "How'd they do?", "Did the Bills win?")
    if re.search(r"\b(score|how did|results|win|lose|playing|do today)\b", q_low):
        team = extract_team_advanced(q_low)
        return get_live_scores(team)

    # Intent: News & Rumors (Human-like: "What's the word?", "Any buzz?")
    if re.search(r"\b(news|latest|buzz|updates|happening|word|rumor)\b", q_low):
        team = extract_team_advanced(q_low)
        return get_team_news(team or "NFL")

    # Intent: Player Scouting (Human-like: "Who is...", "Tell me about...")
    if re.search(r"\b(who is|tell me|profile|about|scouting|look like)\b", q_low) or len(q_low.split()) <= 2:
        # Strip conversational filler to find the name
        clean_name = q_low.replace("who is", "").replace("tell me about", "").replace("on the", "").strip()
        return get_player_profile_smart(clean_name)

    # Intent: Future Matchups
    if re.search(r"\b(next|upcoming|when|schedule|play)\b", q_low):
        team = extract_team_advanced(q_low)
        return get_next_game(team)

    return "I'm not exactly sure what you're looking for, but I'm happy to check on scores, news, or player stats! 🏈"