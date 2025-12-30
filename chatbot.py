"""
NFL Chatbot router
Imports api_client and routes user queries
"""

import re
from utils import clean_query
from api_client import (
    get_live_scores,
    get_standings,
    get_next_game,
    get_last_game,
    get_team_news,
    get_player_profile_smart,
    get_fantasy_player_stats,

)

# -------------------------------------
# Chatbot Query Handler
# -------------------------------------
def handle_user_query(q: str):
    """
    Docstring for handle_user_query
    
    :param q: Description
    :type q: str

    Works the following way:
    Query: News Cowboys
    result = get_team_news("Cowboys")

    Query: Standings Giants
    result = get_standings("Giants")
    """
    if not q:
        return "How can I help with NFL info?"

    q_low = q.strip().lower()

    # Scores
    if "score" in q_low or "scores" in q_low:
        return get_live_scores()

    # Standings
    if "standing" in q_low or "rank" in q_low or "record" in q_low:
        team = extract_team(q)
        return get_standings(team)

    # News
    if "news" in q_low or "article" in q_low or "headline" in q_low:
        team = extract_team(q)
        return get_team_news(team)

    # Next game / last game
    if ("next" in q_low or "upcoming" in q_low or ("when" in q_low and "play" in q_low)) and ("play" in q_low or "game" in q_low or "schedule" in q_low):
        t = extract_team(q)
        if not t:
            return "Please include a team name for 'next game' queries (e.g., 'Next game for Chiefs')."
        return get_next_game(t)

    if "last game" in q_low:
        team = extract_team(q)
        return get_last_game(team)

    # Player
    if "who is" in q_low or "player" in q_low or "about" in q_low or "profile" in q_low:
        # Extract the relevant name/query part after removing trigger phrases
        q = clean_query(q)
        if not q:
            return "Please provide a player's name and optional team/position."
        return get_player_profile_smart(q, debug=False) # Pass the cleaned query to the smart lookup

    # Fantasy
    if "fantasy" in q_low:
        return get_fantasy_player_stats(q)

    return "I’m not sure what you need — try asking about scores, standings, news, games, or a player."


def extract_team(text: str):
    words = text.split()
    return words[-1]
