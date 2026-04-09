"""
NFL Chatbot Router (Professional Orchestration Version)
Handles intent recognition, entity extraction, and stateful memory.
"""

import re
import logging
from typing import Optional
import streamlit as st
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
    detect_team_from_query,
    resolve_contextual_query,
    _normalize_player_query
)

logger = logging.getLogger(__name__)

# -------------------------------------
# Intent Mapping (NLU Layer)
# -------------------------------------

# These patterns allow the bot to understand "human" phrasing
INTENT_MAP = {
    "scores": r"\b(score|how did|results|win|lose|playing|do today|beat)\b",
    "news": r"\b(news|latest|buzz|updates|happening|word|rumor|headlines)\b",
    "scouting": r"\b(who is|tell me|profile|about|scouting|look like|how well|stats|numbers)\b",
    "schedule": r"\b(next|upcoming|when|schedule|play|future)\b",
    "betting": r"\b(odds|spread|line|over/under|betting|favorite|underdog)\b",
    "fantasy": r"\b(fantasy|ppr|points|bench|start)\b"
}

# -------------------------------------
# Helper Logic
# -------------------------------------

def extract_team_advanced(text: str) -> Optional[str]:
    """
    Scans query for known team names or abbreviations.
    """
    detected = detect_team_from_query(text)
    if detected:
        return detected
        
    # Fallback: simple extraction of the last significant word
    words = text.strip().split()
    return words[-1] if words else None

# -------------------------------------
# Main Orchestration ("The Brain")
# -------------------------------------

def nfl_chatbot_with_context(user_input: str):
    """
    The main entry point. Decides whether to use memory or switch subjects.
    """
    # 1. Subject Priority: Does the user mention a NEW team or player?
    # This prevents 'Sticky Subject' syndrome (e.g., switching from Patriots to Giants)
    new_entity = detect_team_from_query(user_input) or _normalize_player_query(user_input)
    last_subject = st.session_state.get("last_mentioned")
    
    # Guard: Don't treat common action words as new subjects
    actions = ["last game", "next game", "fantasy", "stats", "news", "scores", "standing", "schedule"]
    is_action_only = new_entity.lower() in actions if new_entity else False

    # 2. Memory Routing Logic
    if new_entity and not is_action_only:
        # Topic Switch detected
        resolved_query = user_input
        logger.info(f"Subject Switch detected: {new_entity}")
    else:
        # Vague/Follow-up query (e.g., 'How did they do?') -> Apply memory
        resolved_query = resolve_contextual_query(user_input, last_subject)
    
    # 3. Process the query through the Intent Router
    response = handle_user_query(resolved_query)
    
    # 4. State Management: Only "lock in" the new subject if the search was successful
    # We don't update memory if the result was a 'selection_required' dictionary
    if isinstance(response, str) and new_entity and not is_action_only:
        st.session_state["last_mentioned"] = new_entity
        
    return response

def handle_user_query(q: str):
    """
    Maps conversational intent to specific data functions.
    Handles both Narrative Strings and Selection Dictionaries.
    """
    q_low = q.lower().strip()

    # --- Intent: Player Scouting & Disambiguation ---
    # This block handles "Who is Josh Allen" or "Josh Allen on the Bills"
    if re.search(INTENT_MAP["scouting"], q_low) or len(q_low.split()) <= 2:
        # Strip conversational filler to isolate the name/team/position
        clean_name = q_low.replace("who is", "").replace("tell me about", "").replace("on the", "").strip()
        
        # Returns a Narrative String OR a dict for Selection Buttons
        return get_player_profile_smart(clean_name)

    # --- Intent: Scores & Live Results ---
    if re.search(INTENT_MAP["scores"], q_low):
        team = extract_team_advanced(q_low)
        return get_live_scores(team if team not in ["score", "results"] else None)

    # --- Intent: Team News & Buzz ---
    if re.search(INTENT_MAP["news"], q_low):
        team = extract_team_advanced(q_low)
        return get_team_news(team or "NFL")

    # --- Intent: Schedule & Matchups ---
    if re.search(INTENT_MAP["schedule"], q_low):
        team = extract_team_advanced(q_low)
        return get_next_game(team)

    # --- Intent: Betting & Odds ---
    if re.search(INTENT_MAP["betting"], q_low):
        team = extract_team_advanced(q_low)
        return get_game_odds(team)

    # --- Intent: Fantasy Performance ---
    if re.search(INTENT_MAP["fantasy"], q_low):
        return get_fantasy_player_stats(q_low)

    # Fallback response for unhandled queries
    return "I'm not exactly sure what you're looking for, but I'm happy to check on scores, news, or player stats! 🏈"