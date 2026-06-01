"""
NFL Chatbot Router — Gemini-Powered Production Version
Replaces regex intent matching with a Gemini LLM that handles:
  - Natural language understanding (intent + entity extraction)
  - Multi-intent queries ("scores AND next game for the Bills")
  - Conversational memory across turns
  - Human-quality response formatting

All existing API functions (api_client.py) are preserved as data tools.
Uses the google.genai SDK (v2+).
"""

import json
import logging
import os
from typing import Optional, Union, Dict, Any

import streamlit as st
from google import genai
from google.genai import types

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
)

logger = logging.getLogger(__name__)

# Model to use — gemini-2.5-flash is the latest stable free-tier model
GEMINI_MODEL = "gemini-2.5-flash"

logger = logging.getLogger(__name__)

# -------------------------------------------------------
# Gemini Client Setup
# -------------------------------------------------------

def _get_gemini_client() -> genai.Client:
    """Initialise and return the Gemini client, cached in session state."""
    if "gemini_client" not in st.session_state:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. "
                "Add it to your .env file — get a free key at https://aistudio.google.com/app/apikey"
            )
        st.session_state["gemini_client"] = genai.Client(api_key=api_key)
    return st.session_state["gemini_client"]


def _call_gemini(system: str, user: str, expect_json: bool = False) -> str:
    """
    Single wrapper for all Gemini calls with error handling.
    Separates system instructions from user content using the new SDK.
    Returns the text response, or a safe sentinel string on failure.
    """
    try:
        client = _get_gemini_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.3,   # Low temp for consistent JSON extraction
            ),
        )
        text = response.text.strip()
        # Strip markdown code fences if Gemini wraps JSON in them
        if expect_json:
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return text
    except ValueError as e:
        logger.error(f"Gemini config error: {e}")
        return f"__CONFIG_ERROR__: {e}"
    except Exception as e:
        logger.error(f"Gemini API call failed: {e}")
        return "__API_ERROR__"


# -------------------------------------------------------
# Step 1 — Intent & Entity Extraction
# -------------------------------------------------------

_EXTRACTION_SYSTEM = """
You are an NFL assistant that extracts structured intent from user queries.
Respond ONLY with a valid JSON object — no explanation, no markdown.

Schema:
{
  "intents": [list of intents from the allowed set],
  "team": "team name as a string, or null",
  "player": "player full name as a string, or null",
  "raw_query": "the original user query unchanged"
}

Allowed intents (pick ALL that apply — multi-intent is supported):
  scores      — live or recent game scores
  last_game   — result of the most recently completed game
  standings   — win/loss records and division/conference rankings
  news        — team or league news and headlines
  schedule    — upcoming game schedule
  player      — player profile, stats, or scouting report
  fantasy     — fantasy football points or recommendations
  odds        — betting lines, spread, over/under
  general     — anything else NFL-related

Rules:
- Always return valid JSON. Never return plain text.
- If no team is mentioned, set "team" to null.
- If no player is mentioned, set "player" to null.
- For follow-up queries like "how about them?" use the context clues provided.
- Normalise team names to their full name (e.g. "pats" -> "New England Patriots").
- If the query is ambiguous, pick the most likely intent.
"""

def _extract_intent(user_input: str, last_subject: Optional[str]) -> Dict[str, Any]:
    """
    Ask Gemini to parse the user query into structured intent + entities.
    Falls back to a safe default dict on any failure.
    """
    context_hint = f'\nContext: the user was previously asking about "{last_subject}".' if last_subject else ""
    user_prompt = f"{context_hint}\n\nUser query: {user_input}"

    raw = _call_gemini(_EXTRACTION_SYSTEM, user_prompt, expect_json=True)

    if raw.startswith("__"):
        # Gemini unavailable — fall back to a best-effort team detection
        team = detect_team_from_query(user_input)
        return {"intents": ["general"], "team": team, "player": None, "raw_query": user_input, "__error": raw}

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"Gemini returned non-JSON: {raw[:200]}")
        team = detect_team_from_query(user_input)
        return {"intents": ["general"], "team": team, "player": None, "raw_query": user_input}


# -------------------------------------------------------
# Step 2 — Data Dispatch (calls existing API functions)
# -------------------------------------------------------

def _dispatch(parsed: Dict[str, Any]) -> Dict[str, str]:
    """
    Calls the appropriate data functions based on extracted intents.
    Returns a dict of {intent: raw_data_string} for all matched intents.
    """
    intents = parsed.get("intents", ["general"])
    team    = parsed.get("team")
    player  = parsed.get("player")
    results = {}

    for intent in intents:
        try:
            if intent == "scores":
                results["scores"] = get_live_scores(team)

            elif intent == "last_game":
                if team:
                    results["last_game"] = get_last_game(team)
                else:
                    results["last_game"] = "Please specify a team to look up their last game."

            elif intent == "standings":
                results["standings"] = get_standings(team)

            elif intent == "news":
                results["news"] = get_team_news(team or "NFL")

            elif intent == "schedule":
                if team:
                    results["schedule"] = get_next_game(team)
                else:
                    results["schedule"] = "Please specify a team to look up their schedule."

            elif intent == "player":
                name = player or team  # fallback: treat team slot as player name
                if name:
                    raw = get_player_profile_smart(name)
                    # get_player_profile_smart can return a disambiguation dict
                    if isinstance(raw, dict):
                        results["player"] = raw  # pass through for app.py to render buttons
                    else:
                        results["player"] = raw
                else:
                    results["player"] = "Which player would you like to know about?"

            elif intent == "fantasy":
                name = player or parsed.get("raw_query", "")
                results["fantasy"] = get_fantasy_player_stats(name)

            elif intent == "odds":
                if team:
                    results["odds"] = get_game_odds(team)
                else:
                    results["odds"] = "Which team's betting lines would you like?"

            else:
                # general / unknown — no data fetch, let Gemini answer from knowledge
                results["general"] = None

        except Exception as e:
            logger.error(f"Dispatch error for intent '{intent}': {e}")
            results[intent] = f"I ran into a problem fetching {intent} data."

    return results


# -------------------------------------------------------
# Step 3 — Response Formatting
# -------------------------------------------------------

_FORMATTING_SYSTEM = """
You are NFL Pro-Bot, a knowledgeable and conversational NFL assistant.
Your job is to turn raw data into a natural, engaging response.

Guidelines:
- Be concise but informative. Use bullet points for lists, prose for single facts.
- Use football terminology naturally — don't over-explain basics.
- Add light personality (e.g. "That's a tough matchup" or "The defence has been shaky lately").
- If the data says "no games today" or similar, acknowledge it naturally.
- Format scores, records, and stats in bold using Markdown.
- Never make up stats or scores — only use what's in the provided data.
- If data is missing or errored, say so honestly and suggest what the user can try instead.
- Keep responses under 300 words unless the user asks for detail.
"""

def _format_response(user_input: str, data_results: Dict[str, Any],
                     conversation_history: list) -> str:
    """
    Ask Gemini to turn raw API data into a conversational response,
    using the conversation history for context.
    """
    # Build a compact history string (last 6 turns max to stay within token budget)
    history_str = ""
    if conversation_history:
        recent = conversation_history[-6:]
        history_str = "\n".join(
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in recent
        )
        history_str = f"\nConversation so far:\n{history_str}\n"

    # Serialise data — skip disambiguation dicts (handled by app.py)
    data_str = ""
    for intent, data in data_results.items():
        if isinstance(data, dict):
            continue  # disambiguation dict — app.py renders buttons, not Gemini
        if data:
            data_str += f"\n[{intent.upper()} DATA]\n{data}\n"

    # If all results were disambiguation dicts, skip formatting
    if not data_str.strip():
        return "__DISAMBIGUATION__"

    user_prompt = (
        f"{history_str}"
        f"\nUser just asked: {user_input}"
        f"\nRaw data to work with:{data_str}"
        f"\nWrite your response:"
    )

    result = _call_gemini(_FORMATTING_SYSTEM, user_prompt)
    if result.startswith("__"):
        # Gemini down — return the raw data directly as a fallback
        return "\n\n".join(str(v) for v in data_results.values() if v and not isinstance(v, dict))
    return result


# -------------------------------------------------------
# Main Entry Point
# -------------------------------------------------------

def nfl_chatbot_with_context(user_input: str) -> Union[str, Dict[str, Any]]:
    """
    Production entry point. Full pipeline:
      1. Extract intent + entities via Gemini
      2. Dispatch to existing data functions
      3. Format the response via Gemini
      4. Update session memory
    """
    last_subject = st.session_state.get("last_mentioned")
    conversation_history = st.session_state.get("messages", [])

    # --- Step 1: Understand the query ---
    parsed = _extract_intent(user_input, last_subject)
    logger.info(f"Parsed intent: {parsed}")

    # --- Step 2: Fetch data ---
    data_results = _dispatch(parsed)

    # --- Step 3: Check for disambiguation (pass straight to app.py) ---
    for intent, result in data_results.items():
        if isinstance(result, dict) and result.get("type") == "selection_required":
            return result  # app.py renders the selection buttons

    # --- Step 4: Format the response ---
    response = _format_response(user_input, data_results, conversation_history)

    # --- Step 5: Update memory with the most salient entity ---
    new_subject = parsed.get("player") or parsed.get("team")
    if new_subject and isinstance(response, str) and not response.startswith("__"):
        st.session_state["last_mentioned"] = new_subject

    return response
