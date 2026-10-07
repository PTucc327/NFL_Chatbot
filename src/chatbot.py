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
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Set as AbstractSet
from typing import Optional, Union, Dict, Any, Generator, Protocol
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
    get_league_headlines,
    get_player_profile_smart,
    get_player_injury,
    get_player_weekly_stats,
    get_fantasy_sit_start,
    get_fantasy_player_stats,
    get_player_comparison,
    get_trade_analysis,
    get_waiver_recommendations,
    get_game_odds,
    get_team_roster,
    get_player_history,
    get_player_chart_data,
    detect_team_from_query,
)

logger = logging.getLogger(__name__)

GEMINI_MODEL = "gemini-2.5-flash"

# Position strings that the extraction prompt places in the "player" slot
# for roster/waiver queries filtered by position.
VALID_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DE", "DT", "LB", "CB", "S"}

# Subset of VALID_POSITIONS that are relevant for fantasy waiver filtering.
_WAIVER_POSITIONS = {"QB", "RB", "WR", "TE"}

# Keywords that indicate a sit/start question rather than a raw stats lookup.
_SIT_START_KEYWORDS = frozenset({"start", "sit", "bench", "lineup", "waiver", "should i"})

# Number of recent conversation turns sent to the response formatter and the
# maximum characters per turn to include (prevents prompt bloat).
_HISTORY_TURNS = 6
_HISTORY_MAX_CHARS = 300


class IntentHandler(Protocol):
    """
    Structural type for all entries in _INTENT_DISPATCH.
    Every handler (lambda or named function) must be callable with this signature.
    Having an explicit protocol makes the contract visible and lets type checkers
    catch mismatched handlers before they cause a runtime error in _fetch_one.
    """
    def __call__(
        self,
        team: Optional[str],
        player: Optional[str],
        player_b: Optional[str],
        raw_query: str,
        season: Optional[int],
    ) -> Any: ...


@dataclass
class ChatbotResponse:
    """
    Wraps a streaming response from nfl_chatbot_with_context.
    Carries the token generator and any supplemental chart data together
    so app.py doesn't need a side-channel through st.session_state.
    """
    stream: Generator
    chart_data: Optional[Dict[str, Any]] = field(default=None)


# -------------------------------------------------------
# Gemini Client  (module-level singleton, thread-safe)
# -------------------------------------------------------
# Storing a genai.Client in st.session_state works for single-user local
# runs but creates a pickling risk if Streamlit ever serializes session
# state (e.g., with a future state backend).  A module-level singleton
# guarded by a Lock is safer and consistent with the caching pattern used
# in api_client.py for _TEAM_CACHE / _PLAYER_CACHE.
_gemini_client: Optional[genai.Client] = None
_gemini_lock = threading.Lock()


def _get_gemini_client() -> genai.Client:
    global _gemini_client
    with _gemini_lock:
        if _gemini_client is None:
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise ValueError(
                    "GEMINI_API_KEY is not set. "
                    "Add it to your .env file — get a free key at https://aistudio.google.com/app/apikey"
                )
            _gemini_client = genai.Client(api_key=api_key)
    return _gemini_client

# -------------------------------------------------------
# Basic Rate Limiting (per browser session, no external infra needed)
# -------------------------------------------------------
# Every user message costs at least 2 Gemini calls (intent extraction +
# response formatting). Without a cap, a bug that loops, or someone just
# hammering the app, has an unbounded cost. This is intentionally simple —
# a rolling short-burst window plus a hard per-session ceiling — since
# st.session_state already gives free per-user isolation with zero setup.
_RATE_LIMIT_WINDOW_SECONDS = 60    # burst window length
_RATE_LIMIT_MAX_PER_WINDOW = 10    # max messages within that window
_RATE_LIMIT_SESSION_CAP    = 150   # hard cap for the whole session


def _check_rate_limit() -> Optional[str]:
    """Returns a user-facing message if the caller is rate-limited, else None."""
    now = time.time()
    rl = st.session_state.get(
        "rate_limit", {"window_start": now, "window_count": 0, "session_count": 0}
    )

    if rl["session_count"] >= _RATE_LIMIT_SESSION_CAP:
        st.session_state["rate_limit"] = rl
        return (
            f"⚠️ You've hit this session's message limit "
            f"({_RATE_LIMIT_SESSION_CAP} messages). Please refresh the page "
            f"to start a new session."
        )

    # Reset the burst window if it has expired.
    if now - rl["window_start"] >= _RATE_LIMIT_WINDOW_SECONDS:
        rl["window_start"] = now
        rl["window_count"] = 0

    # The window reset above already handles the expired-window case, so
    # wait is always positive here if we're still over the limit.
    if rl["window_count"] >= _RATE_LIMIT_MAX_PER_WINDOW:
        wait = int(_RATE_LIMIT_WINDOW_SECONDS - (now - rl["window_start"]))
        st.session_state["rate_limit"] = rl
        return f"⚠️ You're sending messages a bit fast — please wait ~{wait}s and try again."

    rl["window_count"]  += 1
    rl["session_count"] += 1
    st.session_state["rate_limit"] = rl
    return None



# -------------------------------------------------------
# App-wide Gemini budget (shared by every session in this process)
# -------------------------------------------------------
# The per-session limiter above resets on page refresh and can't see other
# users, but every session spends the same API key. These caps protect the
# key's quota (and bill) as a whole. Module globals are process-wide in
# Streamlit, which is one process per deployed app. Override via env vars;
# 0 disables a cap.
_GLOBAL_MAX_PER_MINUTE = int(os.getenv("GEMINI_MAX_MSGS_PER_MIN", "5"))
_GLOBAL_MAX_PER_DAY    = int(os.getenv("GEMINI_MAX_MSGS_PER_DAY", "200"))
_QUOTA_COOLDOWN_SECONDS = 60
_DAILY_QUOTA_COOLDOWN_SECONDS = 15 * 60

_budget_lock = threading.Lock()
_budget = {"minute": [], "day": None, "day_count": 0, "cooldown_until": 0.0}

QUOTA_ERROR = "__QUOTA_ERROR__"
API_ERROR = "__API_ERROR__"
CONFIG_ERROR = "__CONFIG_ERROR__"
_ERROR_SENTINELS = (QUOTA_ERROR, API_ERROR, CONFIG_ERROR)

BUSY_MESSAGE = (
    "⚠️ NFL Pro-Bot is getting more questions than it can answer right now. "
    "Please try again in about a minute."
)
DAILY_LIMIT_MESSAGE = (
    "⚠️ NFL Pro-Bot has reached its daily question limit. Please come back tomorrow!"
)


def _check_global_budget() -> Optional[str]:
    """Returns a user-facing message if the app-wide budget is spent, else None."""
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _budget_lock:
        if now < _budget["cooldown_until"]:
            return BUSY_MESSAGE
        if _budget["day"] != today:
            _budget["day"], _budget["day_count"] = today, 0
        if _GLOBAL_MAX_PER_DAY and _budget["day_count"] >= _GLOBAL_MAX_PER_DAY:
            return DAILY_LIMIT_MESSAGE
        _budget["minute"] = [t for t in _budget["minute"] if now - t < 60]
        if _GLOBAL_MAX_PER_MINUTE and len(_budget["minute"]) >= _GLOBAL_MAX_PER_MINUTE:
            return BUSY_MESSAGE
        _budget["minute"].append(now)
        _budget["day_count"] += 1
    return None


def _gemini_error(exc: Exception) -> str:
    """Maps an SDK exception to a sentinel and logs it without key or prompt text."""
    code = getattr(exc, "code", None)
    if code == 429:
        violations = [
            v.get("quotaId")
            for d in (getattr(exc, "details", None) or {}).get("error", {}).get("details", [])
            if isinstance(d, dict) for v in d.get("violations", []) if isinstance(v, dict)
        ]
        # A spent daily quota won't recover in a minute; back off longer.
        cooldown = (_DAILY_QUOTA_COOLDOWN_SECONDS
                    if any("PerDay" in (q or "") for q in violations)
                    else _QUOTA_COOLDOWN_SECONDS)
        with _budget_lock:
            _budget["cooldown_until"] = time.time() + cooldown
        logger.warning("Gemini quota exhausted (429) quota_ids=%s - cooling down %ss",
                       violations, cooldown)
        return QUOTA_ERROR
    logger.error("Gemini API call failed: %s code=%s", type(exc).__name__, code)
    return API_ERROR


def _call_gemini(system: str, user: str, expect_json: bool = False) -> str:
    """Blocking call — used for intent extraction."""
    try:
        client = _get_gemini_client()
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=user,
            config=types.GenerateContentConfig(system_instruction=system, temperature=0.3),
        )
        text = (response.text or "").strip()
        if expect_json:
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return text
    except ValueError:
        logger.error("Gemini config error: invalid configuration (check GEMINI_API_KEY and model name)")
        return CONFIG_ERROR
    except Exception as e:
        return _gemini_error(e)


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
    except ValueError:
        logger.error("Gemini stream config error: invalid configuration (check GEMINI_API_KEY and model name)")
        yield CONFIG_ERROR
    except Exception as e:
        yield _gemini_error(e)


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
  "season": "4-digit season year as integer, or null (e.g. 2024 for 'last year')",
  "raw_query": "the original user query unchanged"
}

Allowed intents (pick ALL that apply — multi-intent is supported):
  scores      — live or recent game scores
  last_game   — result of the most recently completed game
  standings   — win/loss records and division/conference rankings
  news        — team or league news and headlines
  league_news — general NFL news not tied to one team ("around the league",
                "biggest storylines", "what's happening in the NFL")
  schedule    — upcoming game schedule
  player      — player profile, career stats, or scouting report
  injury      — player injury status, practice participation, return timeline
  fantasy     — fantasy points, sit/start advice, or waiver recommendations
  comparison  — head-to-head comparison of two named players
  trade       — fantasy trade evaluation between two named players
  waiver      — waiver wire pickup recommendations, optionally filtered by position
  odds        — betting lines, spread, over/under
  roster      — team depth chart, "who is the backup QB", "list the receivers"
  history     — past performance, "last year", "career stats vs", "how did X do against Y",
                "game log", "stats over the season"
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
- If the query mentions roster, depth chart, backup, who starts, who plays → use "roster" intent.
  - If a position is mentioned (QB, RB, WR, TE, etc.) alongside the roster question,
    set "player" to that position string (e.g. "WR").
- For history intent: extract the season year into "season" when the user says "last year",
  "in 2024", "during the 2023 season", etc. If no year is mentioned, set "season" to null.
- If the query is ambiguous, pick the most likely intent.
- Use "news" when a specific team is named or implied. Use "league_news"
  when the question is about the NFL broadly — no specific team, phrases
  like "around the league", "biggest storylines". Both can appear
  together, e.g. intents=["news","league_news"].
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
                "player_b": None, "season": None, "raw_query": user_input, "__error": raw}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Gemini returned non-JSON for intent extraction (first 200 chars redacted from log)")
        team = detect_team_from_query(user_input)
        return {"intents": ["general"], "team": team, "player": None,
                "player_b": None, "season": None, "raw_query": user_input}


# -------------------------------------------------------
# Step 2 — Concurrent Data Dispatch
# -------------------------------------------------------
# Intent handlers — one function per intent that needs more than a single
# API call.  Simple intents are wired up directly in _INTENT_DISPATCH below.
# All handlers share the signature (team, player, player_b, raw_query, season).
# Unused parameters are collected into *_ to signal intent clearly.
# -------------------------------------------------------


def _build_chart_data(player_name: str, team: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Delegates to api_client.get_player_chart_data — returns weekly PPR data
    for a sparkline chart, or None if insufficient data.
    """
    try:
        return get_player_chart_data(player_name, team=team)
    except Exception as e:
        logger.warning(f"chart_data build failed for {player_name}: {e}")
        return None


def _maybe_attach_chart(result: Any, player_name: str, team: Optional[str] = None) -> Any:
    """Attach chart_data to a plain-text result if sparkline data is available.

    Chart is only appended to string responses — structured dicts carry their
    own data and don't need a sparkline overlay.
    """
    if not isinstance(result, str):
        return result
    chart = _build_chart_data(player_name, team)
    return {"_text": result, "chart_data": chart} if chart else result


def _position_hint(player: Optional[str], valid: AbstractSet[str]) -> Optional[str]:
    """Return the upper-cased position token when *player* is a known position string, else None."""
    return player.upper() if player and player.upper() in valid else None


def _handle_player(team, player, *_):
    name = player or team
    if not name:
        return "Which player?"
    # Team narrows same-name players (e.g. after a disambiguation click).
    profile = get_player_profile_smart(name, team=team if player else None)
    # Profile dicts carry their own structured data; chart is only appended
    # to plain-text responses where a sparkline adds meaningful context.
    return _maybe_attach_chart(profile, name, team if player else None)


def _handle_fantasy(team, player, _player_b, raw_query, *_):
    if not player:
        return "Which player do you want fantasy info for?"
    if any(kw in raw_query.lower() for kw in _SIT_START_KEYWORDS):
        result = get_fantasy_sit_start(player, team)  # team used for matchup context
    else:
        result = get_fantasy_player_stats(player)
    return _maybe_attach_chart(result, player)


def _handle_comparison(_team, player, player_b, *_):
    if player and player_b:
        return get_player_comparison(player, player_b)
    if player:
        return f"I need two players to compare. Who should I compare {player} against?"
    return "Please name two players to compare."


def _handle_trade(_team, player, player_b, *_):
    if player and player_b:
        return get_trade_analysis(player, player_b)
    if player:
        return f"I need both players in the trade. Who would you get in return for {player}?"
    return "Please name both players in the trade."


def _handle_waiver(_team, player, *_):
    # Position hint is stored in the player slot by the extraction prompt.
    return get_waiver_recommendations(position=_position_hint(player, _WAIVER_POSITIONS))


def _handle_roster(team, player, *_):
    if not team:
        return "Which team's roster would you like to see?"
    # Position hint stored in the player slot (mirrors waiver pattern).
    return get_team_roster(team, position=_position_hint(player, VALID_POSITIONS))


def _handle_history(team, player, _player_b, _raw_query, season, *_):
    if not player:
        return "Which player's game history would you like to see?"
    return get_player_history(player, opponent_team=team, season_year=season)


# Maps each intent string to an IntentHandler with signature
# (team, player, player_b, raw_query, season) -> Any.
# Simple one-liner intents use lambdas that name only what they use;
# intents with branching logic use the named handler functions defined above.
_INTENT_DISPATCH: Dict[str, IntentHandler] = {
    "scores":      lambda t, *_: get_live_scores(t),
    "last_game":   lambda t, *_: get_last_game(t) if t else "Please specify a team.",
    "standings":   lambda t, *_: get_standings(t),
    "news":        lambda t, *_: get_team_news(t or "NFL"),
    "league_news": lambda *_: get_league_headlines(),
    "schedule":    lambda t, *_: get_next_game(t) if t else "Please specify a team.",
    "injury":      lambda t, p, *_: get_player_injury(p or t) if (p or t) else "Which player's injury status?",
    "odds":        lambda t, *_: get_game_odds(t) if t else "Which team's betting lines?",
    "player":      _handle_player,
    "fantasy":     _handle_fantasy,
    "comparison":  _handle_comparison,
    "trade":       _handle_trade,
    "waiver":      _handle_waiver,
    "roster":      _handle_roster,
    "history":     _handle_history,
}


def _fetch_one(intent: str, team: Optional[str], player: Optional[str],
               player_b: Optional[str], raw_query: str,
               season: Optional[int] = None) -> tuple[str, Any]:
    """Fetch data for a single intent. Runs in a thread pool."""
    try:
        handler = _INTENT_DISPATCH.get(intent)
        if handler:
            return intent, handler(team, player, player_b, raw_query, season)
        return intent, None  # "general" and unknown intents — Gemini answers from knowledge
    except Exception as e:
        logger.error(f"Dispatch error for intent '{intent}': {e}")
        return intent, f"I ran into a problem fetching {intent} data."


def _dispatch(parsed: Dict[str, Any]) -> tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """Run all intent fetches in parallel.

    Returns a (results, chart_data) tuple so the caller doesn't need a
    second pass over results to find the chart payload.
    """
    # .get(..., ["general"]) only falls back when the key is *missing* —
    # if Gemini returns "intents": [] (present but empty), that default
    # never kicks in and max_workers below becomes 0, which crashes
    # ThreadPoolExecutor outright. `or` catches both cases.
    intents  = parsed.get("intents") or ["general"]
    team     = parsed.get("team")
    player   = parsed.get("player")
    player_b = parsed.get("player_b")
    season   = parsed.get("season")
    raw      = parsed.get("raw_query", "")

    results: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(len(intents), 5)) as pool:
        futures = {
            pool.submit(_fetch_one, intent, team, player, player_b, raw, season): intent
            for intent in intents
        }
        for future in as_completed(futures):
            intent_key, result = future.result()
            results[intent_key] = result

    # Extract chart_data in one pass here rather than forcing the caller to
    # iterate results a second time.
    chart_data: Optional[Dict[str, Any]] = None
    for result in results.values():
        if isinstance(result, dict) and "chart_data" in result:
            chart_data = result["chart_data"]
            break

    return results, chart_data


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
- If the user's question has nothing to do with football, briefly say that's
  outside what you help with and steer back to NFL topics — don't just answer
  it as a general-purpose assistant.
"""

def _build_format_prompt(user_input: str, data_results: Dict[str, Any],
                         conversation_history: list,
                         conv_state: Dict[str, Any]) -> str:
    """Builds the formatting prompt including history and conversation state."""
    history_str = ""
    if conversation_history:
        recent = conversation_history[-_HISTORY_TURNS:]
        lines = [
            f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content'][:_HISTORY_MAX_CHARS]}"
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
            if "_text" in data:
                # chart_data wrapper from _maybe_attach_chart — extract the text portion
                data_str += f"\n[{intent.upper()} DATA]\n{data['_text']}\n"
            elif data.get("type") != "selection_required":
                # Real structured dict (e.g. trade analysis, comparison) — serialize it
                # so Gemini can read it.  Disambiguation dicts are excluded because
                # app.py handles those before stream_response is ever called.
                data_str += f"\n[{intent.upper()} DATA]\n{json.dumps(data, indent=2)}\n"
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
    # Filter out plain disambiguation dicts — those that carry _text (chart_data
    # wrappers from Task 14) are real data and should go through to Gemini.
    non_disambig = {
        k: v for k, v in data_results.items()
        if not isinstance(v, dict) or "_text" in v
    }
    if not non_disambig:
        return  # disambiguation only — app.py handles it

    prompt = _build_format_prompt(user_input, data_results, conversation_history, conv_state)
    first = True
    for chunk in _stream_gemini(_FORMATTING_SYSTEM, prompt):
        if chunk in _ERROR_SENTINELS:
            if not first:
                yield "\n\n_(The response was cut off — please try again.)_"
            elif chunk != CONFIG_ERROR and (raw := _raw_data_fallback(non_disambig)):
                # The data is already fetched; show it rather than an error.
                yield ("_The AI assistant is busy, so here's the raw data "
                       "I found:_\n\n" + raw)
            else:
                yield chunk  # app.py renders the matching error message
            return
        first = False
        yield chunk


def _raw_data_fallback(data_results: Dict[str, Any]) -> str:
    """Markdown from fetched data, used when Gemini can't format a reply."""
    parts = []
    for data in data_results.values():
        text = data.get("_text") if isinstance(data, dict) else data
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


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

    # New unrelated intent — clear state.
    # Note: "waiver" and "fantasy" are intentionally absent from this set so
    # that mid-trade/comparison context is preserved when the user asks a
    # fantasy follow-up (e.g. "who should I pick up at WR?" during a trade
    # discussion).  If that behaviour ever needs to change, add those intents
    # to the set below.
    if intents & {"scores", "standings", "news", "league_news", "schedule",
                  "last_game", "injury", "odds", "roster"}:
        return {}

    return current_state  # preserve state for ambiguous intents


# -------------------------------------------------------
# Main Entry Point
# -------------------------------------------------------

def nfl_chatbot_with_context(user_input: str) -> Union[str, Dict[str, Any], ChatbotResponse]:
    """
    Full pipeline:
      1. Extract intent + entities via Gemini (blocking)
      2. Fetch all data concurrently
      3. Check for disambiguation → return dict for app.py
      4. Update conversation state
      5. Return ChatbotResponse(stream, chart_data) for app.py → st.write_stream()
      6. Update session memory
    """
    context = {
        "last_player": st.session_state.get("last_player"),
        "last_team":   st.session_state.get("last_team"),
        "conv_state":  st.session_state.get("conv_state", {}),
    }
    conversation_history = st.session_state.get("messages", [])

    # Step 0 — rate limit check, before any Gemini call is made
    limit_msg = _check_rate_limit() or _check_global_budget()
    if limit_msg:
        return limit_msg

    # Step 1 — understand
    parsed = _extract_intent(user_input, context)
    if parsed.get("__error") == QUOTA_ERROR:
        # Without intents there's no data to fall back on, and formatting
        # would hit the same quota — answer now instead of spending a call.
        return BUSY_MESSAGE
    # Log structured intent metadata only — raw_query is omitted to avoid
    # persisting user message content in cloud log aggregators, which would
    # contradict the privacy policy ("no personal data collected").
    logger.info(
        "intent_extraction intents=%s team=%s player=%s player_b=%s season=%s",
        parsed.get("intents"),
        parsed.get("team"),
        parsed.get("player"),
        parsed.get("player_b"),
        parsed.get("season"),
    )

    # Step 2 — fetch
    data_results, chart_data = _dispatch(parsed)

    # Step 3 — disambiguation
    for result in data_results.values():
        if isinstance(result, dict) and result.get("type") == "selection_required":
            return result

    # Step 4 — update conversation state
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

    return ChatbotResponse(stream=generator, chart_data=chart_data)