"""
NFL Chatbot Router (Professional Conversational Version)
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

# Professional Intent Patterns for Natural Language Understanding (NLU)
INTENT_MAP = {
    "scores": r"\b(score|how did|results|win|lose|playing|do today|beat)\b",
    "news": r"\b(news|latest|buzz|updates|happening|word|rumor|headlines)\b",
    "scouting": r"\b(who is|tell me|profile|about|scouting|look like|how well|stats|numbers)\b",
    "schedule": r"\b(next|upcoming|when|schedule|play|future)\b",
    "betting": r"\b(odds|spread|line|over/under|betting|favorite|underdog)\b",
    "fantasy": r"\b(fantasy|ppr|points|bench|start)\b"
}

def extract_team_advanced(text: str) -> Optional[str]:
    """
    Scans query for known team names/abbreviations using prioritized cache matching.
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
    """
    Decides whether to use conversational memory or switch focus to a new subject.
    """
    import streamlit as st 
    
    # 1. Get the previous subject from memory
    last_subject = st.session_state.get("last_mentioned")
    
    # 2. Identify if a NEW entity is mentioned in this specific turn
    new_entity = detect_team_from_query(user_input) or _normalize_player_query(user_input)
    
    # Check if the detected 'entity' is actually just a common action word
    actions = ["last game", "next game", "fantasy", "stats", "news", "scores", "standing", "schedule"]
    is_action_only = new_entity.lower() in actions if new_entity else False

    # 3. Decision Logic: Priority goes to NEW entities to prevent "Sticky Subject" syndrome
    if new_entity and not is_action_only:
        resolved_query = user_input
        st.session_state["last_mentioned"] = new_entity
        logger.info(f"Subject Switch: {new_entity}")
    else:
        # Use context if the query is vague (e.g., "how did they do?")
        resolved_query = resolve_contextual_query(user_input, last_subject)
    
    # 4. Route to the specialized handlers
    response = handle_user_query(resolved_query)
        
    return response

def handle_user_query(q: str):
    """
    Routes conversational prompts to specific API data functions.
    """
    q_low = q.lower().strip()

    # Intent: Scores & Results
    if re.search(INTENT_MAP["scores"], q_low):
        team = extract_team_advanced(q_low)
        # If no specific team is resolved, provide the general scoreboard
        return get_live_scores(team if team not in ["score", "results"] else None)

    # Intent: News & Rumors
    if re.search(INTENT_MAP["news"], q_low):
        team = extract_team_advanced(q_low)
        return get_team_news(team or "NFL")

    # Intent: Schedule & Future Games
    if re.search(INTENT_MAP["schedule"], q_low):
        team = extract_team_advanced(q_low)
        return get_next_game(team)

    # Intent: Betting & Odds
    if re.search(INTENT_MAP["betting"], q_low):
        team = extract_team_advanced(q_low)
        return get_game_odds(team)

    # Intent: Fantasy & Points
    if re.search(INTENT_MAP["fantasy"], q_low):
        return get_fantasy_player_stats(q_low)

    # Intent: Player Scouting (Catch-all for names and "Tell me about...")
    if re.search(INTENT_MAP["scouting"], q_low) or len(q_low.split()) <= 2:
        # Strip conversational filler to isolate the name for the fuzzy matcher
        clean_name = q_low.replace("who is", "").replace("tell me about", "").replace("on the", "").strip()
        return get_player_profile_smart(clean_name)

    return "I'm not exactly sure what you're looking for, but I'm happy to check on scores, news, or player stats! 🏈"