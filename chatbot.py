"""
NFL Chatbot Router (Perfected Version)
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
    Falls back to the last word only if no known team is found.
    """
    detected = detect_team_from_query(text)
    if detected:
        return detected
        
    # Fallback: simple extraction
    words = text.strip().split()
    return words[-1] if words else None

# -------------------------------------
# Main Orchestration
# -------------------------------------

def nfl_chatbot_with_context(user_input: str):
    """
    Professional Orchestrator:
    1. Manages Streamlit session state
    2. Resolves pronouns (contextual memory)
    3. Routes to the correct API function
    """
    import streamlit as st 
    
    # 1. Retrieve memory
    last_subject = st.session_state.get("last_mentioned")
    
    # 2. Contextual Resolution (e.g., 'his stats' -> 'Patrick Mahomes stats')
    resolved_query = resolve_contextual_query(user_input, last_subject)
    logger.info(f"Original: {user_input} | Resolved: {resolved_query}")
    
    # 3. Process Query
    response = handle_user_query(resolved_query)
    
    # 4. Subject Update Guard
    # We update the subject only if a specific player or team was detected
    new_entity = detect_team_from_query(user_input) or _normalize_player_query(user_input)
    
    # Don't let generic action words become the 'subject'
    forbidden_subjects = {"news", "stats", "scores", "standing", "fantasy", "odds", "game"}
    if new_entity and new_entity.lower() not in forbidden_subjects:
        st.session_state["last_mentioned"] = new_entity
        
    return response

def handle_user_query(q: str):
    """
    Structured Intent Router using Regex patterns.
    """
    if not q:
        return "How can I help with NFL info?"

    q_low = q.strip().lower()

    # Intent: Live Scores
    if re.search(r"\b(score|scores|scoreboard|who won)\b", q_low):
        return get_live_scores()

    # Intent: Standings/Record
    if re.search(r"\b(standing|rank|record|playoff seed)\b", q_low):
        team = extract_team_advanced(q)
        # If 'standing' is mentioned without a team, get global standings
        return get_standings(team if team not in ["standing", "rank"] else None)

    # Intent: News
    if re.search(r"\b(news|article|headline|updates)\b", q_low):
        team = extract_team_advanced(q)
        return get_team_news(team)

    # Intent: Schedule (Next Game)
    if re.search(r"\b(next|upcoming|when|play|schedule)\b", q_low) and "game" in q_low:
        team = extract_team_advanced(q)
        return get_next_game(team)

    # Intent: Recent History (Last Game)
    if "last game" in q_low or "previous game" in q_low:
        team = extract_team_advanced(q)
        return get_last_game(team)

    # Intent: Fantasy Analysis
    if "fantasy" in q_low:
        return get_fantasy_player_stats(q)

    # Intent: Betting/Odds
    if re.search(r"\b(odds|spread|line|over/under|betting)\b", q_low):
        team = extract_team_advanced(q)
        return get_game_odds(team)

    # Intent: Player Profile (Catch-all for names)
    if re.search(r"\b(who is|player|profile|about)\b", q_low) or len(q_low.split()) <= 2:
        cleaned_name = clean_query(q)
        return get_player_profile_smart(cleaned_name)

    return "I’m not sure what you need — try asking about scores, standings, news, or a specific player."