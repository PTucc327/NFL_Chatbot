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

import datetime
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from collections.abc import Set as AbstractSet
from typing import Optional, Union, Dict, Any, Generator, Protocol
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeout

import streamlit as st
from google import genai
from google.genai import types

from src.api_client import (
    get_live_scores,
    get_standings,
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
    get_week_schedule,
    get_team_schedule,
    get_league_leaders,
    get_box_score,
    get_playoff_picture,
    get_team_rankings,
    get_postseason_results,
    get_draft_context,
    get_player_team,
    current_nfl_season_year,
    current_nfl_week,
)

logger = logging.getLogger(__name__)

# Free-tier models, tried in order. Each model has its own free quota
# (per Google Cloud project), so falling through a chain adds their daily
# capacity together. Measured on the free tier (first-token latency):
# 3.5-flash-lite ~0.6s with a generous quota; 2.5-flash ~0.4s but ~20
# requests/day; 3.5-flash 8-14s and only ~2 requests/minute — so it is a
# backup, not the default. The lite model once emitted a stray non-English
# word; _strip_foreign_script filters that. Override with
# GEMINI_EXTRACT_MODELS / GEMINI_FORMAT_MODELS, or GEMINI_MODELS for both.
def _model_list(env: str, default: tuple) -> list:
    raw = os.getenv(env) or os.getenv("GEMINI_MODELS") or ",".join(default)
    return [m.strip() for m in raw.split(",") if m.strip()]


GEMINI_EXTRACT_MODELS = _model_list("GEMINI_EXTRACT_MODELS", (
    "gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash",
    "gemini-3.1-flash-lite",   # slowest in testing (~7-16s); last resort
))
GEMINI_FORMAT_MODELS = _model_list("GEMINI_FORMAT_MODELS", (
    "gemini-3.5-flash-lite", "gemini-2.5-flash", "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
))
# Every model in use, for app-wide checks ("is anything still available?").
GEMINI_MODELS = list(dict.fromkeys(GEMINI_EXTRACT_MODELS + GEMINI_FORMAT_MODELS))

# Position strings that the extraction prompt places in the "player" slot
# for roster/waiver queries filtered by position.
VALID_POSITIONS = {"QB", "RB", "WR", "TE", "K", "DE", "DT", "LB", "CB", "S"}

# Subset of VALID_POSITIONS that are relevant for fantasy waiver filtering.
_WAIVER_POSITIONS = {"QB", "RB", "WR", "TE"}

# Intents answered at team level; a named player's team fills in when absent.
_TEAM_LEVEL_INTENTS = {"schedule", "last_game", "box_score", "odds", "scores", "standings",
                       "news", "team_stats"}

# Keywords that indicate a sit/start question rather than a raw stats lookup.
_SIT_START_KEYWORDS = frozenset({"start", "sit", "bench", "lineup", "waiver", "should i"})

# Number of recent conversation turns sent to the response formatter and the
# maximum characters per turn to include (prevents prompt bloat).
_HISTORY_TURNS = 6
_HISTORY_MAX_CHARS = 300

# Upper bound on data fetching per message. A single fetch retries for up to
# ~13s (utils.fetch_json); past this, answer with whatever has arrived.
_DISPATCH_TIMEOUT_SECONDS = 20


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
        opponent: Optional[str],
        stat: Optional[str],
        week: Optional[int],
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
            # Free-tier latency varies (2-7s typical; one request took 60s in
            # testing). Past the deadline a model is skipped for the next one.
            # Google's minimum is 10s.
            _gemini_client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=_GEMINI_TIMEOUT_MS),
            )
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
# App-wide Gemini budget + free-tier model chain
# -------------------------------------------------------
# The per-session limiter above resets on page refresh and can't see other
# users, but every session spends the same API key. Module globals are
# process-wide in Streamlit (one process per deployed app).
#
# On the free tier the real ceiling is Google's per-model quota, so each
# model in GEMINI_MODELS gets its own cooldown when it reports a 429 (until
# midnight Pacific for a daily quota) and requests fall through to the next
# model. The caps below only smooth bursts; 0 disables a cap.
_GLOBAL_MAX_PER_MINUTE = int(os.getenv("GEMINI_MAX_MSGS_PER_MIN", "10"))
_GLOBAL_MAX_PER_DAY    = int(os.getenv("GEMINI_MAX_MSGS_PER_DAY", "0"))
_QUOTA_COOLDOWN_SECONDS = 60          # per-minute quota with no retry hint
_GEMINI_TIMEOUT_MS = max(10_000, int(os.getenv("GEMINI_TIMEOUT_MS", "20000")))  # Google min: 10s
_RETIRED_MODEL_COOLDOWN_SECONDS = 24 * 60 * 60
_DAILY_COOLDOWN_THRESHOLD = 15 * 60   # cooldowns longer than this = daily quota

_budget_lock = threading.Lock()
_budget = {"minute": [], "day": None, "day_count": 0}
_model_cooldown: Dict[str, float] = {}   # model -> unix time it may be retried

QUOTA_ERROR = "__QUOTA_ERROR__"
API_ERROR = "__API_ERROR__"
CONFIG_ERROR = "__CONFIG_ERROR__"
_ERROR_SENTINELS = (QUOTA_ERROR, API_ERROR, CONFIG_ERROR)
FALLBACK_NOTICE = "_The AI assistant is busy, so here's the raw data I found:_\n\n"
CUT_OFF_NOTICE = "\n\n_(The response was cut off — please try again.)_"

BUSY_MESSAGE = (
    "⚠️ NFL Pro-Bot is getting more questions than it can answer right now. "
    "Please try again in about a minute."
)
DAILY_LIMIT_MESSAGE = (
    "⚠️ NFL Pro-Bot has used up its free AI quota for today. It resets at "
    "midnight Pacific time — please come back then!"
)


def _seconds_until_pacific_midnight() -> float:
    """Free-tier daily quotas reset at midnight Pacific time."""
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        pacific = now.astimezone(ZoneInfo("America/Los_Angeles"))
    except Exception:  # no tz database (e.g. Windows without tzdata)
        pacific = now.astimezone(datetime.timezone(datetime.timedelta(hours=-7)))
    midnight = (pacific + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return (midnight - pacific).total_seconds()


def _available_models(models: Optional[list] = None) -> list:
    """Models in `models` (default: all in use) not cooling down, in order."""
    now = time.time()
    with _budget_lock:
        return [m for m in (models or GEMINI_MODELS) if _model_cooldown.get(m, 0) <= now]


def _check_global_budget() -> Optional[str]:
    """Returns a user-facing message if the app can't take a question now, else None."""
    now = time.time()
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _budget_lock:
        waits = [_model_cooldown.get(m, 0) - now for m in GEMINI_MODELS]
        if waits and min(waits) > 0:  # every model is cooling down
            return DAILY_LIMIT_MESSAGE if min(waits) > _DAILY_COOLDOWN_THRESHOLD else BUSY_MESSAGE
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


def _error_details(exc: Exception) -> list:
    details = (getattr(exc, "details", None) or {})
    details = details.get("error", details) if isinstance(details, dict) else {}
    return [d for d in details.get("details", []) if isinstance(d, dict)]


def _model_failed(model: str, exc: Exception) -> bool:
    """
    Records a failed call on `model`. Returns True when the next model in the
    chain should be tried (quota spent, model retired, or Google overloaded),
    False for errors another model wouldn't fix. Logs ids and limits only —
    never the key or the prompt.
    """
    code = getattr(exc, "code", None)
    if code == 429:
        details = _error_details(exc)
        violations = [v for d in details for v in d.get("violations", []) if isinstance(v, dict)]
        daily = any("PerDay" in (v.get("quotaId") or "") for v in violations)
        retry = next((d.get("retryDelay") for d in details if d.get("retryDelay")), None)
        if daily:
            cooldown = _seconds_until_pacific_midnight()
        else:
            try:
                cooldown = float(str(retry).rstrip("s")) + 1 if retry else _QUOTA_COOLDOWN_SECONDS
            except ValueError:
                cooldown = _QUOTA_COOLDOWN_SECONDS
        logger.warning(
            "Gemini quota exhausted (429) model=%s limits=%s - model paused %ds",
            model,
            [f"{v.get('quotaId')}={v.get('quotaValue')}" for v in violations],
            cooldown,
        )
    elif code == 404:
        cooldown = _RETIRED_MODEL_COOLDOWN_SECONDS
        logger.error("Gemini model unavailable (404) model=%s - skipping it for 24h", model)
    elif isinstance(code, int) and code >= 500:
        cooldown = _QUOTA_COOLDOWN_SECONDS
        logger.warning("Gemini server error code=%s model=%s - trying next model", code, model)
    elif code is None and "timeout" in f"{type(exc).__name__} {exc}".lower():
        cooldown = _QUOTA_COOLDOWN_SECONDS
        logger.warning("Gemini timed out after %sms model=%s - trying next model",
                       _GEMINI_TIMEOUT_MS, model)
    else:
        logger.error("Gemini API call failed: %s code=%s model=%s", type(exc).__name__, code, model)
        return False
    with _budget_lock:
        _model_cooldown[model] = time.time() + cooldown
    return True


# Scripts that never belong in an English NFL answer: Devanagari, Arabic,
# Hebrew, Cyrillic, Thai, CJK, Hangul, Japanese kana. Latin with accents
# (player names) and emoji are kept.
_FOREIGN_SCRIPT_RE = re.compile(
    "[\u0400-\u04FF\u0590-\u05FF\u0600-\u06FF\u0900-\u097F\u0E00-\u0E7F"
    "\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF]+"
)


def _strip_foreign_script(text: str) -> str:
    """Drops stray non-Latin script the lite models occasionally emit ("big बोर्डs")."""
    cleaned = _FOREIGN_SCRIPT_RE.sub("", text)
    if cleaned != text:
        logger.warning("Removed stray non-Latin characters from model output")
    return cleaned


# Test mode for the browser (e2e) tests: deterministic stand-ins for Gemini,
# so the UI, routing and live data can be exercised in CI without an API key
# or free-tier quota. Never set NFL_BOT_FAKE_LLM in production.
_FAKE_LLM = os.getenv("NFL_BOT_FAKE_LLM") == "1"
if _FAKE_LLM:
    logger.warning("NFL_BOT_FAKE_LLM=1 — Gemini is replaced by test stand-ins")

_FAKE_PLAYER_RE = re.compile(r"tell me about (.+?)[?.!]*$", re.I)


def _fake_extract(user_prompt: str) -> str:
    """Typed questions in test mode: 'Tell me about X' → player intent, else general."""
    query = user_prompt.rsplit("User query:", 1)[-1].strip()
    m = _FAKE_PLAYER_RE.search(query)
    parsed = {"intents": ["player"] if m else ["general"], "team": None,
              "player": m.group(1).strip() if m else None, "player_b": None,
              "season": None, "raw_query": query}
    return json.dumps(parsed)


def _fake_answer(user_prompt: str) -> str:
    """Formatting in test mode: echo the fetched data, unmodified."""
    data = user_prompt.split("Raw data to work with:", 1)[-1].split("Write your response:", 1)[0]
    return "[test mode] " + (data.strip() or "No data for that question.")


# Thinking off: answers here restate fetched data, and Flash's default
# "thinking" delayed the first word by ~4s (0.6s without). Some models reject
# the setting (gemini-3.5-flash-lite: 400); those are retried once without it
# and remembered.
_NO_THINKING_CONFIG: set = set()


def _gen_config(model: str, system: str, temperature: float) -> "types.GenerateContentConfig":
    if model in _NO_THINKING_CONFIG:
        return types.GenerateContentConfig(system_instruction=system, temperature=temperature)
    return types.GenerateContentConfig(
        system_instruction=system, temperature=temperature,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )


def _rejects_thinking_config(model: str, exc: Exception) -> bool:
    """True (and remembered) when `model` refused the thinking setting."""
    if getattr(exc, "code", None) == 400 and model not in _NO_THINKING_CONFIG:
        logger.info("model=%s rejected thinking_budget=0; retrying without it", model)
        _NO_THINKING_CONFIG.add(model)
        return True
    return False


def _call_gemini(system: str, user: str, expect_json: bool = False) -> str:
    """Blocking call — used for intent extraction. Falls through GEMINI_EXTRACT_MODELS."""
    if _FAKE_LLM:
        return _fake_extract(user)
    try:
        client = _get_gemini_client()
    except ValueError:
        logger.error("Gemini config error: GEMINI_API_KEY is not set")
        return CONFIG_ERROR
    for model in _available_models(GEMINI_EXTRACT_MODELS):
        response = None
        for _attempt in range(2):
            try:
                response = client.models.generate_content(
                    model=model, contents=user, config=_gen_config(model, system, 0.3),
                )
                break
            except Exception as e:
                if _rejects_thinking_config(model, e):
                    continue
                if _model_failed(model, e):
                    break
                return API_ERROR
        if response is None:
            continue
        text = (response.text or "").strip()
        if expect_json:
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return text
    return QUOTA_ERROR


def _stream_gemini(system: str, user: str) -> Generator[str, None, None]:
    """
    Streaming call — yields tokens as they arrive. Falls through GEMINI_FORMAT_MODELS
    while nothing has been yielded; a failure mid-answer yields API_ERROR.
    """
    if _FAKE_LLM:
        yield _fake_answer(user)
        return
    try:
        client = _get_gemini_client()
    except ValueError:
        logger.error("Gemini config error: GEMINI_API_KEY is not set")
        yield CONFIG_ERROR
        return
    for model in _available_models(GEMINI_FORMAT_MODELS):
        for _attempt in range(2):
            started = False
            try:
                stream = client.models.generate_content_stream(
                    model=model, contents=user, config=_gen_config(model, system, 0.7),
                )
                for chunk in stream:
                    text = _strip_foreign_script(chunk.text or "")
                    if text:
                        started = True
                        yield text
                return
            except Exception as e:
                if not started and _rejects_thinking_config(model, e):
                    continue  # same model, without the thinking setting
                retry_next = _model_failed(model, e)
                if retry_next and not started:
                    break  # next model
                yield API_ERROR
                return
    yield QUOTA_ERROR


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
  "opponent": "second TEAM full name for team-vs-team questions, or null",
  "stat": "for leaders: one of pass_yd, pass_td, pass_int, rush_yd, rush_td, rec, rec_yd, rec_td, pts_ppr, pts_half_ppr, pts_std — or null",
  "week": "regular-season week number as integer when the user names one, or null",
  "season": "4-digit season year as integer, or null",
  "raw_query": "the original user query unchanged"
}

Allowed intents (pick ALL that apply — multi-intent is supported):
  scores      — live or recent game scores
  last_game   — result of the most recently completed game
  standings   — win/loss records and division/conference rankings ("NFC East
                standings", "where are the Ravens in the division")
  playoffs    — playoff picture, seeding, wild card race, "who's in the playoffs",
                "would the Bills make the playoffs if the season ended today"
  news        — team or league news and headlines
  league_news — general NFL news not tied to one team ("around the league",
                "biggest storylines", "what's happening in the NFL")
  schedule    — game schedule: a team's next/remaining games or a matchup date
                ("when do the Cowboys play the Eagles"), OR — with no team —
                this week's league slate ("Thursday Night Football tonight",
                "who's on bye", "what games are on Sunday")
  box_score   — details of a specific game that has started or finished: box score,
                recap, "how did X beat Y", "who scored", "game stats", "what
                happened in the game", live in-game stats
  team_stats  — how a TEAM is doing statistically: offense/defense rankings,
                "how's the Bills defense", "best run defense in the league",
                "which team scores the most", "Eagles offense stats"
  postseason  — PAST playoff results and Super Bowls: "who won the Super Bowl
                last season", "2023 playoff results", "who won the AFC
                championship last year"
  draft       — upcoming NFL draft prospects, mock drafts, college players
  leaders     — league or position leaders and rankings ("who leads in passing
                yards", "top 5 fantasy QBs", "most receiving TDs")
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
- Seasons: use the current season given in the context line. "This season" or
  "this year" → null (the app defaults to the current season). "Last season" /
  "last year" → current season minus 1. Only set an explicit year the user names.
  An NFL season is named for the year it starts (Feb 2026 Super Bowl = 2025 season).
- For team-vs-team questions ("when do the Cowboys play the Eagles", "Bills vs
  Chiefs history"), put the first team in "team" and the second in "opponent".
- Rookie questions ("best rookies", "rookie of the year race") → "leaders".
- Upcoming draft prospects, mock drafts and college players → "draft".
- For leaders: set "stat" (fantasy rankings → pts_ppr unless half-PPR/standard is
  named) and put a position filter (QB, RB, WR, TE, K) in "player". "Top N" goes
  nowhere — the app shows the top 10.
- "How did X beat/lose to Y", "recap", "box score", "who scored", "what happened in
  the X game" → "box_score" (team = first team, opponent = second team if named,
  week if named). A bare "what was the score of the X game" → "last_game".
- If the query is ambiguous, pick the most likely intent.
- Use "news" when a specific team is named or implied. Use "league_news"
  when the question is about the NFL broadly — no specific team, phrases
  like "around the league", "biggest storylines". Both can appear
  together, e.g. intents=["news","league_news"].
"""

def _season_context() -> str:
    """
    Today's date and the current season for both prompts. Without it the model
    falls back to its training-data year ("this season" became 2024).
    """
    today = datetime.date.today()
    try:
        season, week = current_nfl_season_year(), current_nfl_week()
        season_str = f"Current NFL season: {season} (Week {week})."
    except Exception:  # stats service down — the date alone still helps
        season_str = ""
    return f"Today is {today:%A, %B} {today.day}, {today.year}. {season_str}".strip()


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
    user_prompt = f"{_season_context()}{context_hint}\n\nUser query: {user_input}"
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


def _build_chart(players: list) -> Optional[Dict[str, Any]]:
    """
    Weekly PPR chart for one or more (name, team) players:
    {"weeks": ["Wk 1", ...], "series": {"Full Name": [pts or None, ...]}}.
    Players with too little data are left out; None if none qualify.
    """
    per_player = []
    for name, team in players:
        try:
            data = get_player_chart_data(name, team=team)
        except Exception as e:
            logger.warning(f"chart_data build failed for {name}: {e}")
            data = None
        if data:
            per_player.append(data)
    if not per_player:
        return None
    weeks = sorted({w for d in per_player for w in d["weeks"]},
                   key=lambda w: int(w.split()[-1]))
    series = {}
    for d in per_player:
        pts = dict(zip(d["weeks"], d["pts"]))
        series[d.get("name", "PPR")] = [pts.get(w) for w in weeks]
    return {"weeks": weeks, "series": series}


def _with_chart(result: Any, players: list) -> Any:
    """Attach a chart to a plain-text result when chart data is available.

    Structured dicts (e.g. disambiguation) are returned unchanged.
    """
    if not isinstance(result, str):
        return result
    chart = _build_chart(players)
    return {"_text": result, "chart_data": chart} if chart else result


def _maybe_attach_chart(result: Any, player_name: str, team: Optional[str] = None) -> Any:
    return _with_chart(result, [(player_name, team)])


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
        return _with_chart(get_player_comparison(player, player_b),
                           [(player, None), (player_b, None)])
    if player:
        return f"I need two players to compare. Who should I compare {player} against?"
    return "Please name two players to compare."


def _handle_trade(_team, player, player_b, *_):
    if player and player_b:
        return _with_chart(get_trade_analysis(player, player_b),
                           [(player, None), (player_b, None)])
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


def _handle_schedule(team, _player, _player_b, _raw_query, _season, opponent=None, *_):
    # No team: the league-wide slate ("TNF tonight?", "who's on bye?").
    return get_team_schedule(team, opponent=opponent) if team else get_week_schedule()


_DIVISION_RE = re.compile(r"\b(AFC|NFC)\s+(East|North|South|West)\b", re.I)
_CONFERENCE_RE = re.compile(r"\b(AFC|NFC)\b", re.I)


def _conference_in(text: str) -> Optional[str]:
    m = _CONFERENCE_RE.search(text or "")
    return m.group(1).upper() if m else None


def _handle_standings(team, _player, _player_b, raw_query, *_):
    # Division names ("NFC East") come from the raw text; the extraction
    # schema has no slot for them and the regex is exact.
    div = _DIVISION_RE.search(raw_query or "")
    if div:
        return get_standings(division=f"{div.group(1).upper()} {div.group(2).title()}")
    if team:
        return get_standings(team)
    return get_standings(conference=_conference_in(raw_query))


def _handle_playoffs(team, _player, _player_b, raw_query, *_):
    result = get_playoff_picture(conference=_conference_in(raw_query))
    if team:  # "would the Bills make it?" — add their division/seed context
        result += "\n\n" + get_standings(team)
    return result


# Most specific first: "run defense" must win over "defense".
_TEAM_FOCUS_PATTERNS = (
    (re.compile(r"\b(run|rush(ing)?) d(efen[cs]e)?\b|against the run", re.I), "rush_defense"),
    (re.compile(r"\bpass(ing)? d(efen[cs]e)?\b|secondary|against the pass", re.I), "pass_defense"),
    (re.compile(r"\bsacks?\b|pass rush", re.I), "sacks"),
    (re.compile(r"turnover|takeaway", re.I), "turnovers"),
    (re.compile(r"third down|3rd down", re.I), "third_down"),
    (re.compile(r"red ?zone", re.I), "red_zone"),
    (re.compile(r"\bdefen[cs]e\b|points allowed", re.I), "defense"),
    (re.compile(r"\b(rushing|run game|ground game)\b", re.I), "rushing"),
    (re.compile(r"\b(passing|pass offense|air attack)\b", re.I), "passing"),
    (re.compile(r"\boffen[cs]e\b|scor", re.I), "offense"),
)


def _team_focus(text: str) -> Optional[str]:
    return next((focus for rx, focus in _TEAM_FOCUS_PATTERNS if rx.search(text or "")), None)


def _handle_team_stats(team, _player, _player_b, raw_query, *_):
    # With a team, the full profile (the formatter focuses on what was asked);
    # without one, a league leaderboard for the stat the question is about.
    if team:
        return get_team_rankings(team)
    return get_team_rankings(focus=_team_focus(raw_query))


def _handle_postseason(_team, _player, _player_b, raw_query, season, *_):
    # Default season (most recent completed playoffs) is resolved in api_client.
    super_bowl_only = "super bowl" in (raw_query or "").lower()
    return get_postseason_results(season if isinstance(season, int) else None,
                                  super_bowl_only=super_bowl_only)


def _handle_box_score(team, _player, _player_b, _raw_query, _season,
                      opponent=None, _stat=None, week=None, *_):
    if not team:
        return "Which game? Name a team (and the opponent or week if you like)."
    return get_box_score(team, opponent=opponent, week=week)


def _handle_leaders(_team, player, _player_b, raw_query, _season, _opponent=None, stat=None, *_):
    # Position filter rides in the player slot, as for roster/waiver.
    position = _position_hint(player, VALID_POSITIONS)
    if "rookie" in (raw_query or "").lower():
        return get_league_leaders(stat or "pts_ppr", position=position, rookies_only=True)
    return get_league_leaders(stat or "pts_ppr", position=position)


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
    "standings":   _handle_standings,
    "playoffs":    _handle_playoffs,
    "news":        lambda t, *_: get_team_news(t or "NFL"),
    "league_news": lambda *_: get_league_headlines(),
    "schedule":    _handle_schedule,
    "leaders":     _handle_leaders,
    "box_score":   _handle_box_score,
    "team_stats":  _handle_team_stats,
    "postseason":  _handle_postseason,
    "draft":       lambda *_: get_draft_context(),
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
               season: Optional[int] = None, opponent: Optional[str] = None,
               stat: Optional[str] = None, week: Optional[int] = None) -> tuple[str, Any]:
    """Fetch data for a single intent. Runs in a thread pool."""
    try:
        handler = _INTENT_DISPATCH.get(intent)
        if handler:
            return intent, handler(team, player, player_b, raw_query, season, opponent, stat, week)
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
    opponent = parsed.get("opponent")
    stat     = parsed.get("stat")
    week     = parsed.get("week") if isinstance(parsed.get("week"), int) else None
    raw      = parsed.get("raw_query", "")

    # "Is Mahomes playing this week?" — team-level data for a named player
    # uses his team instead of asking the user which team that is.
    if (not team and player and not _position_hint(player, VALID_POSITIONS)
            and set(intents) & _TEAM_LEVEL_INTENTS):
        try:
            team = get_player_team(player)
        except Exception as e:
            logger.warning("player team lookup failed: %s", e)

    results: Dict[str, Any] = {}
    pool = ThreadPoolExecutor(max_workers=min(len(intents), 5))
    futures = {
        pool.submit(_fetch_one, intent, team, player, player_b, raw, season, opponent, stat, week): intent
        for intent in intents
    }
    try:
        for future in as_completed(futures, timeout=_DISPATCH_TIMEOUT_SECONDS):
            intent_key, result = future.result()
            results[intent_key] = result
    except FuturesTimeout:
        for intent in futures.values():
            if intent not in results:
                logger.warning("Dispatch timeout for intent '%s' after %ss",
                               intent, _DISPATCH_TIMEOUT_SECONDS)
                results[intent] = f"The {intent} data source took too long to respond."
    finally:
        # Don't block the reply on stragglers; they finish in the background.
        pool.shutdown(wait=False, cancel_futures=True)

    # Extract chart_data in one pass here rather than forcing the caller to
    # iterate results a second time.
    # Prefer the chart with the most players (a comparison over a single profile).
    charts = [r["chart_data"] for r in results.values()
              if isinstance(r, dict) and r.get("chart_data")]
    chart_data = max(charts, key=lambda c: len(c.get("series", {})), default=None)

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
- Current-season facts (scores, stats, records, injuries, standings, rosters,
  schedules) come ONLY from the provided data — never invent them.
- With no data provided, answer timeless NFL knowledge (rules, history, records,
  legends, how things work) from your own knowledge. For anything that may have
  changed recently (rosters, coaches, contracts, recent champions), say it's as
  of your latest information.
- For injury data: clearly state status (Questionable/Out/IR) and expected return.
- For fantasy sit/start: clear recommendation first, then reasoning.
- For player comparisons: highlight the key statistical and contextual differences.
- For trade advice: give a clear verdict (Accept/Decline/Counter) first, then reasoning.
- For waiver wire: list players in rank order, give a one-line reason for each pickup.
- For draft prospects: do NOT list prospects from your own knowledge — it
  predates the latest drafts, and players you remember as college prospects
  are mostly in the NFL now. Name only the players in the curated prospect
  list, say plainly that a full, current big board isn't available here, and
  suggest a current big board (ESPN, NFL.com, The Athletic). NEVER present
  anyone in the ALREADY DRAFTED list as a prospect — they are NFL players now.
- For team rankings: answer the part asked about (offense, defense, run defense…)
  using the values and ranks given; rank 1 is the best of 32. Name a strength or
  weakness when it explains the team's record.
- For box scores: write a short game recap — final score and where it was played
  first, then the turning points from the scoring plays, the standout performers,
  and the team stat that decided it (turnovers, 3rd downs, red zone). Keep the
  line-score table if it helps. Only state home/away and venue as given.
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
        f"{_season_context()}\n"
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
                yield CUT_OFF_NOTICE
            elif chunk != CONFIG_ERROR and (raw := _raw_data_fallback(non_disambig)):
                # The data is already fetched; show it rather than an error.
                yield FALLBACK_NOTICE + raw
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
                  "last_game", "box_score", "leaders", "playoffs", "team_stats", "postseason",
                  "injury", "odds", "roster"}:
        return {}

    return current_state  # preserve state for ambiguous intents


# -------------------------------------------------------
# Preset answers cache
# -------------------------------------------------------
# Sidebar buttons ask the same questions for everyone ("Daily briefing for
# the Eagles"), so a recent Gemini answer can be reused instead of spending
# free-tier quota again. Only clean answers are cached — never raw-data
# fallbacks or cut-off replies.
_ANSWER_CACHE_TTL = 5 * 60
_ANSWER_CACHE_MAX = 200
_answer_cache: Dict[str, tuple] = {}   # key -> (stored_at, text, chart_data)
_answer_cache_lock = threading.Lock()


def _answer_cache_get(key: str) -> Optional[tuple]:
    with _answer_cache_lock:
        hit = _answer_cache.get(key)
    if hit and time.time() - hit[0] < _ANSWER_CACHE_TTL:
        return hit
    return None


def _cache_answer(stream: Generator, key: str,
                  chart_data: Optional[Dict[str, Any]]) -> Generator[str, None, None]:
    """Passes the stream through and caches the full text once it completes cleanly."""
    parts = []
    for chunk in stream:
        parts.append(chunk)
        yield chunk
    text = "".join(parts)
    if (not text or text in _ERROR_SENTINELS or text.startswith(FALLBACK_NOTICE)
            or CUT_OFF_NOTICE in text):
        return
    now = time.time()
    with _answer_cache_lock:
        for k in [k for k, v in _answer_cache.items() if now - v[0] >= _ANSWER_CACHE_TTL]:
            del _answer_cache[k]
        if len(_answer_cache) >= _ANSWER_CACHE_MAX:
            del _answer_cache[min(_answer_cache, key=lambda k: _answer_cache[k][0])]
        _answer_cache[key] = (now, text, chart_data)


# -------------------------------------------------------
# Main Entry Point
# -------------------------------------------------------

def _remember(parsed: Dict[str, Any]) -> None:
    """Session memory used to resolve follow-ups ("how about them?")."""
    new_player = parsed.get("player")
    if new_player and new_player.upper() in VALID_POSITIONS:
        new_player = None  # a position filter, not a player
    new_team = parsed.get("team")
    if new_player:
        st.session_state["last_player"]   = new_player
        st.session_state["last_mentioned"] = new_player
    if new_team:
        st.session_state["last_team"] = new_team
        if not new_player:
            st.session_state["last_mentioned"] = new_team


def nfl_chatbot_with_context(
    user_input: str, preset: Optional[Dict[str, Any]] = None,
) -> Union[str, Dict[str, Any], ChatbotResponse]:
    """
    Full pipeline:
      1. Extract intent + entities via Gemini (blocking) — skipped when the
         caller passes a `preset` (sidebar buttons already know the intent)
      2. Fetch all data concurrently
      3. Check for disambiguation → return dict for app.py
      4. Update conversation state
      5. Return ChatbotResponse(stream, chart_data) for app.py → st.write_stream()
      6. Update session memory

    `preset` holds any of intents/team/player/player_b/season. Preset answers
    are cached for a few minutes and shared across sessions.
    """
    context = {
        "last_player": st.session_state.get("last_player"),
        "last_team":   st.session_state.get("last_team"),
        "conv_state":  st.session_state.get("conv_state", {}),
    }
    conversation_history = st.session_state.get("messages", [])

    # Step 0 — rate limit check, before any Gemini call is made
    limit_msg = _check_rate_limit()
    if limit_msg:
        return limit_msg

    cache_key = json.dumps([user_input, preset], sort_keys=True) if preset else None
    if cache_key and (hit := _answer_cache_get(cache_key)):
        logger.info("preset answer served from cache intents=%s", preset.get("intents"))
        _remember(preset)
        return ChatbotResponse(stream=iter([hit[1]]), chart_data=hit[2])

    limit_msg = _check_global_budget()
    if limit_msg:
        return limit_msg

    # Step 1 — understand
    if preset:
        parsed = {"intents": [], "team": None, "player": None, "player_b": None,
                  "season": None, **preset, "raw_query": user_input}
    else:
        parsed = _extract_intent(user_input, context)
        if parsed.get("__error") == QUOTA_ERROR:
            # Without intents there's no data to fall back on, and formatting
            # would hit the same quota — answer now instead of spending a call.
            return BUSY_MESSAGE
    # Log structured intent metadata only — raw_query is omitted to avoid
    # persisting user message content in cloud log aggregators, which would
    # contradict the privacy policy ("no personal data collected").
    logger.info(
        "intent_extraction source=%s intents=%s team=%s player=%s player_b=%s season=%s",
        "preset" if preset else "gemini",
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
    if cache_key:
        generator = _cache_answer(generator, cache_key, chart_data)

    # Step 6 — memory
    _remember(parsed)

    return ChatbotResponse(stream=generator, chart_data=chart_data)
