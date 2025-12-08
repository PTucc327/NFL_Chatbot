"""
NFL Chatbot router
Imports api_client and routes user queries
"""

import re
from api_client import (
    get_live_scores,
    get_standings,
    get_next_game,
    get_last_game,
    get_team_news,
    get_player_profile_smart,
    get_fantasy_player_stats
)


def handle_user_query(q: str):
    if not q:
        return "How can I help with NFL info?"

    q_low = q.lower()

    # Scores
    if "score" in q_low or "game" in q_low:
        return get_live_scores()

    # Standings
    if "standing" in q_low or "rank" in q_low:
        team = extract_team(q)
        return get_standings(team)

    # News
    if "news" in q_low:
        team = extract_team(q)
        return get_team_news(team)

    # Next game / last game
    if "next game" in q_low:
        team = extract_team(q)
        return get_next_game(team)

    if "last game" in q_low:
        team = extract_team(q)
        return get_last_game(team)

    # Player
    if "who is" in q_low or "player" in q_low:
        return get_player_profile_smart(q)

    # Fantasy
    if "fantasy" in q_low:
        return get_fantasy_player_stats(q)

    return "I’m not sure what you need — try asking about scores, standings, news, games, or a player."


def extract_team(text: str):
    words = text.split()
    return words[-1]
