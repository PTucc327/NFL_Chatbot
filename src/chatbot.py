"""
NFL Chatbot Router — Production Version
Features:
  1. Streaming responses       — tokens render as they arrive (st.write_stream)
  2. Concurrent data fetch     — API calls run in parallel
  3. Dual context memory       — last_player + last_team tracked separately
  4. Injury intent             — injury_status/body_part/depth chart
  5. Weekly player stats       — per-game stat lines by position
  6. Fantasy sit/start         — matchup-aware with Gemini reasoning
  7. Player comparison         — side-by-side stats for two players
  8. Trade advice              — full data package for trade evaluation
  9. Stateful multi-turn       — conversation_state persists decisions across turns

Uses the google.genai SDK (v2+).
"""

import json
import logging
import os
from typing import Optional, Union, Dict, Any, Generator
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    get_player_injury,
    get_player_weekly_stats,
    get_fantasy_sit_start,
    get_fantasy_player_stats,
    get_player_comparison,
    get_trade_analysis,
    get_waiver_recommendations,
    get_game_odds,
    detect_team_from_query,
)

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"


# -------------------------------------------------------
# Gemini Client
# -------------------------------------------------------

def _get_gemini_client() -> genai.Client:
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
    """Blocking call — used for intent extraction."""
    try:
        client = _get_gemini_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user,
            config=types.GenerateContentConfig(system_instruction=system, temperature=0.3),
        )
        text = response.text.strip()
        if expect_json:
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return text
    except ValueError as e:
        logger.error(f"Gemini config error: {e}")
        return f"__CONFIG_ERROR__: {e}"
    except Exception as e:
        logger.error(f"Gemini API call failed: {e}")
        return "__API_ERROR__"


def _stream_gemini(system: str, user: str) -> Generator[str, None, None]:
    """Streaming call — yields tokens as they arrive."""
    try:
        client = _get_gemini_client()
        stream = client.models.generate_content_stream(
            model=GEMINI_MODEL,
            contents=user,
            config=types.GenerateContentConfig(system_instruction=system, temperature=0.7),
        )
        for chunk in stream:
            if chunk.text:
                yield chunk.text
    except ValueError as e:
        logger.error(f"Gemini stream config error: {e}")
        yield f"__CONFIG_ERROR__: {e}"
    except Exception as e:
        logger.error(f"Gemini stream failed: {e}")
        yield "__API_ERROR__"


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
  "player_b": "second player full name for comparisons or trades, or null",
  "raw_query": "the original user query unchanged"
}

Allowed intents (pick ALL that apply — multi-intent is supported):
  scores      — live or recent game scores
  last_game   — result of the most recently completed game
  standings   — win/loss records and division/conference rankings
  news        — team or league news and headlines
  schedule    — upcoming game schedule
  player      — player profile, career stats, or scouting report
  injury      — player injury status, practice participation, return timeline
  fantasy     — fantasy points, sit/start advice, or waiver recommendations
  comparison  — head-to-head comparison of two named players
  trade       — fantasy trade evaluation between two named players
  waiver      — waiver wire pickup recommendations, optionally filtered by position
  odds        — betting lines, spread, over/under
  general     — anything else NFL-related

Rules:
- Always return valid JSON. Never return plain text.
- If no team is mentioned, set "team" to null.
- If no player is mentioned, set "player" to null.
- Set "player_b" when TWO players are mentioned (comparisons, trades). Otherwise null.
- For comparisons: "compare X to Y" or "X vs Y" → intents=["comparison"], player=X, player_b=Y
- For trades: "trade X for Y" or "should I trade X for Y" → intents=["trade"], player=X, player_b=Y
- For waiver: "waiver wire", "who should I pick up", "best free agents" → intents=["waiver"]
  - If a position is mentioned (QB, RB, WR, TE), set "player" to that position string (e.g. "WR")
  - Otherwise set "player" to null
- For follow-up queries like "how about them?" use the context clues provided.
- Normalise team names to their full name (e.g. "pats" -> "New England Patriots").
- If the query mentions injury, hurt, questionable, IR, or practice → use "injury" intent.
- If the query mentions start, sit, bench, or lineup → use "fantasy" intent.
- If the query is ambiguous, pick the most likely intent.
"""

def _extract_intent(user_input: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the query into structured intent + entities via Gemini."""
    hints = []
    if context.get("last_player"):
        hints.append(f'last player discussed: "{context["last_player"]}"')
    if context.get("last_team"):
        hints.append(f'last team discussed: "{context["last_team"]}"')
    # #7 — inject active conversation state so follow-ups resolve correctly
    if context.get("conv_state"):
        cs = context["conv_state"]
        if cs.get("mode") == "trade":
            hints.append(f'active trade being discussed: {cs.get("player_give")} for {cs.get("player_receive")}')
        elif cs.get("mode") == "comparison":
            hints.append(f'active comparison: {cs.get("player_a")} vs {cs.get("player_b")}')

    context_hint = f'\nContext: {", ".join(hints)}.' if hints else ""
    user_prompt = f"{context_hint}\n\nUser query: {user_input}"
    raw = _call_gemini(_EXTRACTION_SYSTEM, user_prompt, expect_json=True)

    if raw.startswith("__"):
        team = detect_team_from_query(user_input)
        return {"intents": ["general"], "team": team, "player": None,
                "player_b": None, "raw_query": user_input, "__error": raw}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"Gemini returned non-JSON: {raw[:200]}")
        team = detect_team_from_query(user_input)
        return {"intents": ["general"], "team": team, "player": None,
                "player_b": None, "raw_query": user_input}


# -------------------------------------------------------
# Step 2 — Concurrent Data Dispatch
# -------------------------------------------------------

def _fetch_one(intent: str, team: Optional[str], player: Optional[str],
               player_b: Optional[str], raw_query: str) -> tuple[str, Any]:
    """Fetch data for a single intent. Runs in a thread pool."""
    try:
        if intent == "scores":
            return intent, get_live_scores(team)

        elif intent == "last_game":
            return intent, get_last_game(team) if team else "Please specify a team."

        elif intent == "standings":
            return intent, get_standings(team)

        elif intent == "news":
            return intent, get_team_news(team or "NFL")

        elif intent == "schedule":
            return intent, get_next_game(team) if team else "Please specify a team."

        elif intent == "player":
            name = player or team
            return intent, get_player_profile_smart(name) if name else "Which player?"

        elif intent == "injury":
            name = player or team
            return intent, get_player_injury(name) if name else "Which player's injury status?"

        elif intent == "fantasy":
            name = player or raw_query
            sit_start_kw = {"start", "sit", "bench", "lineup", "waiver", "should i"}
            if any(kw in raw_query.lower() for kw in sit_start_kw):
                return intent, get_fantasy_sit_start(name, team)
            return intent, get_fantasy_player_stats(name)

        elif intent == "comparison":
            # #3 — player comparison
            if player and player_b:
                return intent, get_player_comparison(player, player_b)
            elif player:
                return intent, f"I need two players to compare. Who should I compare {player} against?"
            return intent, "Please name two players to compare."

        elif intent == "trade":
            # #5 — trade advice
            if player and player_b:
                return intent, get_trade_analysis(player, player_b)
            elif player:
                return intent, f"I need both players in the trade. Who would you get in return for {player}?"
            return intent, "Please name both players in the trade."

        elif intent == "waiver":
            # position hint stored in player slot by the extraction prompt
            pos = player if player and player.upper() in {"QB", "RB", "WR", "TE"} else None
            return intent, get_waiver_recommendations(position=pos)

        elif intent == "odds":
            return intent, get_game_odds(team) if team else "Which team's betting lines?"

        else:
            return intent, None  # general — Gemini answers from knowledge

    except Exception as e:
        logger.error(f"Dispatch error for intent '{intent}': {e}")
        return intent, f"I ran into a problem fetching {intent} data."


def _dispatch(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Run all intent fetches in parallel."""
    intents  = parsed.get("intents", ["general"])
    team     = parsed.get("team")
    player   = parsed.get("player")
    player_b = parsed.get("player_b")
    raw      = parsed.get("raw_query", "")

    results: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(len(intents), 5)) as pool:
        futures = {
            pool.submit(_fetch_one, intent, team, player, player_b, raw): intent
            for intent in intents
        }
        for future in as_completed(futures):
            intent_key, result = future.result()
            results[intent_key] = result
    return results


# -------------------------------------------------------
# Step 3 — Streaming Response Formatting
# -------------------------------------------------------

_FORMATTING_SYSTEM = """
You are NFL Pro-Bot, a knowledgeable and conversational NFL assistant.
Your job is to turn raw data into a natural, engaging response.

Guidelines:
- Be concise but informative. Bullet points for lists, prose for single facts.
- Use football terminology naturally.
- Add light personality ("That's a tough matchup", "The defence has been shaky").
- Format scores, records, and stats in bold Markdown.
- Never fabricate stats or scores — only use provided data.
- For injury data: clearly state status (Questionable/Out/IR) and expected return.
- For fantasy sit/start: clear recommendation first, then reasoning.
- For player comparisons: highlight the key statistical and contextual differences.
- For trade advice: give a clear verdict (Accept/Decline/Counter) first, then reasoning.
- For waiver wire: list players in rank order, give a one-line reason for each pickup.
- If data is missing, say so and suggest an alternative.
- Keep responses under 300 words unless detail is requested.
"""

def _build_format_prompt(user_input: str, data_results: Dict[str, Any],
                         conversation_history: list,
                         conv_state: Dict[str, Any]) -> str:
    """Builds the formatting prompt including history and conversation state."""
    history_str = ""
    if conversation_history:
        recent = conversation_history[-6:]
        lines = [
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content'][:300]}"
            for m in recent
        ]
        history_str = "\nConversation so far:\n" + "\n".join(lines) + "\n"

    # #7 — surface active conversation state for context
    state_str = ""
    if conv_state:
        if conv_state.get("mode") == "trade":
            state_str = (f"\nActive trade discussion: {conv_state.get('player_give')} "
                         f"for {conv_state.get('player_receive')}\n")
        elif conv_state.get("mode") == "comparison":
            state_str = (f"\nActive comparison: {conv_state.get('player_a')} "
                         f"vs {conv_state.get('player_b')}\n")

    data_str = ""
    for intent, data in data_results.items():
        if isinstance(data, dict):
            continue
        if data:
            data_str += f"\n[{intent.upper()} DATA]\n{data}\n"

    return (
        f"{history_str}{state_str}"
        f"\nUser just asked: {user_input}"
        f"\nRaw data to work with:{data_str}"
        f"\nWrite your response:"
    )


def stream_response(user_input: str, data_results: Dict[str, Any],
                    conversation_history: list,
                    conv_state: Dict[str, Any]) -> Generator[str, None, None]:
    """Yields streaming tokens from Gemini for app.py to pass to st.write_stream()."""
    non_dict = {k: v for k, v in data_results.items() if not isinstance(v, dict)}
    if not non_dict:
        return  # disambiguation only — app.py handles it

    prompt = _build_format_prompt(user_input, data_results, conversation_history, conv_state)
    yield from _stream_gemini(_FORMATTING_SYSTEM, prompt)


# -------------------------------------------------------
# #7 — Conversation State Management
# -------------------------------------------------------

def _update_conv_state(parsed: Dict[str, Any],
                       current_state: Dict[str, Any]) -> Dict[str, Any]:
    """
    Maintains a lightweight state dict so the bot remembers what decision
    is being built across turns (trade, comparison, lineup).

    Examples:
      - "Should I trade Kelce for CeeDee Lamb?" sets mode=trade
      - Follow-up "What about over the rest of the season?" uses that context
      - "Compare Josh Allen to Lamar Jackson" sets mode=comparison
      - Any new topic (scores, news) clears the state
    """
    intents = set(parsed.get("intents", []))
    player   = parsed.get("player")
    player_b = parsed.get("player_b")

    # Start a new trade session
    if "trade" in intents and player and player_b:
        return {"mode": "trade", "player_give": player, "player_receive": player_b}

    # Start a new comparison session
    if "comparison" in intents and player and player_b:
        return {"mode": "comparison", "player_a": player, "player_b": player_b}

    # Follow-up to an active trade (no new players named)
    if current_state.get("mode") == "trade" and not player_b:
        if intents & {"trade", "fantasy", "player", "general"}:
            return current_state  # keep the existing state

    # Follow-up to an active comparison
    if current_state.get("mode") == "comparison" and not player_b:
        if intents & {"comparison", "player", "general"}:
            return current_state  # keep the existing state

    # New unrelated intent — clear state
    if intents & {"scores", "standings", "news", "schedule", "last_game", "injury", "odds"}:
        return {}

    return current_state  # preserve state for ambiguous intents


# -------------------------------------------------------
# Main Entry Point
# -------------------------------------------------------

def nfl_chatbot_with_context(user_input: str) -> Union[str, Dict[str, Any], Generator]:
    """
    Full pipeline:
      1. Extract intent + entities via Gemini (blocking)
      2. Fetch all data concurrently
      3. Check for disambiguation → return dict for app.py
      4. Update conversation state (#7)
      5. Return streaming generator for app.py → st.write_stream()
      6. Update session memory
    """
    context = {
        "last_player": st.session_state.get("last_player"),
        "last_team":   st.session_state.get("last_team"),
        "conv_state":  st.session_state.get("conv_state", {}),
    }
    conversation_history = st.session_state.get("messages", [])

    # Step 1 — understand
    parsed = _extract_intent(user_input, context)
    logger.info(f"Parsed intent: {parsed}")

    # Step 2 — fetch
    data_results = _dispatch(parsed)

    # Step 3 — disambiguation
    for result in data_results.values():
        if isinstance(result, dict) and result.get("type") == "selection_required":
            return result

    # Step 4 — update conversation state (#7)
    new_conv_state = _update_conv_state(parsed, context.get("conv_state", {}))
    st.session_state["conv_state"] = new_conv_state

    # Step 5 — stream
    generator = stream_response(
        user_input, data_results, conversation_history, new_conv_state
    )

    # Step 6 — memory
    new_player = parsed.get("player")
    new_team   = parsed.get("team")
    if new_player:
        st.session_state["last_player"]   = new_player
        st.session_state["last_mentioned"] = new_player
    if new_team:
        st.session_state["last_team"] = new_team
        if not new_player:
            st.session_state["last_mentioned"] = new_team

    return generator
