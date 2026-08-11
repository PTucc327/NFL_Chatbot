"""
Tests for src/chatbot.py

Covers: intent extraction parsing, dispatch routing, conversation state management.
Mocks Streamlit and Gemini — no live API calls needed.
"""
import sys
import json
import pytest
import unittest.mock as mock

# ─── Bootstrap mocks before any project imports ───────────────────

sys.modules["streamlit"] = mock.MagicMock()
sys.modules["google"] = mock.MagicMock()
sys.modules["google.genai"] = mock.MagicMock()
sys.modules["google.genai.types"] = mock.MagicMock()

# Mock api_client so chatbot can be imported without a player cache
_api_mock = mock.MagicMock()
_api_mock.get_live_scores.return_value = "Bills 24 @ Patriots 17"
_api_mock.get_standings.return_value   = "AFC East standings"
_api_mock.get_next_game.return_value   = "Bills vs Chiefs Sunday"
_api_mock.get_last_game.return_value   = "Bills 24 - Patriots 17 (Final)"
_api_mock.get_team_news.return_value   = "Bills sign new WR"
_api_mock.get_league_headlines.return_value = "Around the NFL right now..."
_api_mock.get_player_profile_smart.return_value = "### Josh Allen\n- Team: BUF"
_api_mock.get_player_injury.return_value        = "🏥 Josh Allen — Healthy"
_api_mock.get_player_weekly_stats.return_value  = "Wk 17: 30 pts"
_api_mock.get_fantasy_sit_start.return_value    = "Start Josh Allen"
_api_mock.get_fantasy_player_stats.return_value = "Josh Allen: 312 PPR"
_api_mock.get_player_comparison.return_value    = "PLAYER 1 vs PLAYER 2"
_api_mock.get_trade_analysis.return_value       = "GIVING AWAY vs RECEIVING"
_api_mock.get_waiver_recommendations.return_value = "Top Waiver Pickups"
_api_mock.get_game_odds.return_value            = "Bills -6.5"
_api_mock.detect_team_from_query.return_value   = "Buffalo Bills"
sys.modules["src.api_client"] = _api_mock
sys.modules["src.utils"] = mock.MagicMock()

sys.path.insert(0, ".")
import importlib.util as _ilu
import os as _os
_chatbot_path = _os.path.join(_os.path.dirname(__file__), "..", "src", "chatbot.py")
_spec = _ilu.spec_from_file_location("src.chatbot", _chatbot_path)
chatbot = _ilu.module_from_spec(_spec)
sys.modules["src.chatbot"] = chatbot
_spec.loader.exec_module(chatbot)


# ─── Intent extraction schema validation ──────────────────────────

class TestExtractIntentSchema:
    """Validate the JSON schema Gemini is expected to return."""

    def _parse(self, raw_json: str) -> dict:
        return json.loads(raw_json)

    def test_single_intent(self):
        parsed = self._parse('{"intents":["scores"],"team":"Buffalo Bills","player":null,"player_b":null,"raw_query":"bills scores"}')
        assert parsed["intents"] == ["scores"]
        assert parsed["team"] == "Buffalo Bills"
        assert parsed["player"] is None

    def test_multi_intent(self):
        parsed = self._parse('{"intents":["scores","standings"],"team":"Buffalo Bills","player":null,"player_b":null,"raw_query":"test"}')
        assert "scores" in parsed["intents"]
        assert "standings" in parsed["intents"]

    def test_comparison_has_player_b(self):
        parsed = self._parse('{"intents":["comparison"],"team":null,"player":"Josh Allen","player_b":"Lamar Jackson","raw_query":"compare them"}')
        assert parsed["player"] == "Josh Allen"
        assert parsed["player_b"] == "Lamar Jackson"

    def test_trade_has_player_b(self):
        parsed = self._parse('{"intents":["trade"],"team":null,"player":"Travis Kelce","player_b":"CeeDee Lamb","raw_query":"trade kelce for lamb"}')
        assert "trade" in parsed["intents"]
        assert parsed["player_b"] == "CeeDee Lamb"


# ─── _dispatch routing ────────────────────────────────────────────

class TestDispatch:

    def _run(self, intents, team=None, player=None, player_b=None, raw="test"):
        parsed = {"intents": intents, "team": team, "player": player,
                  "player_b": player_b, "raw_query": raw}
        return chatbot._dispatch(parsed)

    def test_scores_intent(self):
        result = self._run(["scores"], team="Buffalo Bills")
        assert "scores" in result

    def test_standings_intent(self):
        result = self._run(["standings"])
        assert "standings" in result

    def test_player_intent(self):
        result = self._run(["player"], player="Josh Allen")
        assert "player" in result

    def test_injury_intent(self):
        result = self._run(["injury"], player="Josh Allen")
        assert "injury" in result
        _api_mock.get_player_injury.assert_called_with("Josh Allen")

    def test_comparison_intent_calls_comparison(self):
        result = self._run(["comparison"], player="Josh Allen", player_b="Lamar Jackson")
        assert "comparison" in result
        _api_mock.get_player_comparison.assert_called_with("Josh Allen", "Lamar Jackson")

    def test_trade_intent_calls_trade(self):
        result = self._run(["trade"], player="Travis Kelce", player_b="CeeDee Lamb")
        assert "trade" in result
        _api_mock.get_trade_analysis.assert_called_with("Travis Kelce", "CeeDee Lamb")

    def test_comparison_missing_player_b_returns_message(self):
        result = self._run(["comparison"], player="Josh Allen", player_b=None)
        assert "comparison" in result
        assert isinstance(result["comparison"], str)
        assert "compare" in result["comparison"].lower() or "need" in result["comparison"].lower()

    def test_fantasy_sit_start_triggered_by_keyword(self):
        result = self._run(["fantasy"], player="Tyreek Hill", raw="should i start tyreek hill")
        _api_mock.get_fantasy_sit_start.assert_called()

    def test_general_intent_returns_none(self):
        result = self._run(["general"])
        assert result.get("general") is None

    def test_multi_intent_returns_all_keys(self):
        result = self._run(["scores", "standings"])
        assert "scores" in result
        assert "standings" in result

    def test_waiver_intent_calls_waiver(self):
        result = self._run(["waiver"], player=None)
        assert "waiver" in result
        _api_mock.get_waiver_recommendations.assert_called()

    def test_waiver_with_position_filter(self):
        _api_mock.get_waiver_recommendations.reset_mock()
        result = self._run(["waiver"], player="WR")
        _api_mock.get_waiver_recommendations.assert_called_with(position="WR")

    def test_league_news_intent_calls_league_headlines(self):
        result = self._run(["league_news"])
        assert "league_news" in result
        _api_mock.get_league_headlines.assert_called()

    def test_news_and_league_news_are_independent(self):
        # A briefing-style request should be able to pull team news AND
        # league-wide headlines in the same dispatch — they hit different
        # backend functions and shouldn't collide on the same result key.
        result = self._run(["news", "league_news"], team="Buffalo Bills")
        assert "news" in result and "league_news" in result
        _api_mock.get_team_news.assert_called_with("Buffalo Bills")
        _api_mock.get_league_headlines.assert_called()

    def test_fantasy_without_player_asks_clarifying_question(self):
        # Regression test — this intent used to silently pass the entire
        # raw query string as a player name instead of asking who.
        _api_mock.get_fantasy_sit_start.reset_mock()
        _api_mock.get_fantasy_player_stats.reset_mock()
        result = self._run(["fantasy"], player=None, raw="who should i start this week")
        assert "fantasy" in result
        assert isinstance(result["fantasy"], str)
        assert "which player" in result["fantasy"].lower()
        _api_mock.get_fantasy_sit_start.assert_not_called()
        _api_mock.get_fantasy_player_stats.assert_not_called()


# ─── _update_conv_state ───────────────────────────────────────────

class TestUpdateConvState:

    def test_sets_trade_mode(self):
        parsed = {"intents": ["trade"], "player": "Kelce", "player_b": "CeeDee Lamb"}
        state = chatbot._update_conv_state(parsed, {})
        assert state["mode"] == "trade"
        assert state["player_give"] == "Kelce"
        assert state["player_receive"] == "CeeDee Lamb"

    def test_sets_comparison_mode(self):
        parsed = {"intents": ["comparison"], "player": "Josh Allen", "player_b": "Lamar Jackson"}
        state = chatbot._update_conv_state(parsed, {})
        assert state["mode"] == "comparison"
        assert state["player_a"] == "Josh Allen"
        assert state["player_b"] == "Lamar Jackson"

    def test_preserves_trade_state_on_followup(self):
        current = {"mode": "trade", "player_give": "Kelce", "player_receive": "CeeDee Lamb"}
        parsed  = {"intents": ["general"], "player": None, "player_b": None}
        state = chatbot._update_conv_state(parsed, current)
        assert state["mode"] == "trade"

    def test_clears_state_on_unrelated_intent(self):
        current = {"mode": "trade", "player_give": "Kelce", "player_receive": "CeeDee Lamb"}
        parsed  = {"intents": ["scores"], "player": None, "player_b": None}
        state = chatbot._update_conv_state(parsed, current)
        assert state == {}

    def test_clears_state_on_news(self):
        current = {"mode": "comparison", "player_a": "Allen", "player_b": "Jackson"}
        parsed  = {"intents": ["news"], "player": None, "player_b": None}
        state = chatbot._update_conv_state(parsed, current)
        assert state == {}

    def test_new_trade_overwrites_old_comparison(self):
        current = {"mode": "comparison", "player_a": "Allen", "player_b": "Jackson"}
        parsed  = {"intents": ["trade"], "player": "Hill", "player_b": "Lamb"}
        state = chatbot._update_conv_state(parsed, current)
        assert state["mode"] == "trade"
        assert state["player_give"] == "Hill"


# ─── Rate limiting ─────────────────────────────────────────────────

class TestRateLimit:
    """
    A plain dict stands in for st.session_state here — real Streamlit
    session state supports the same .get()/[]= interface _check_rate_limit
    actually uses, so this exercises the real logic without needing a
    live Streamlit runtime.
    """

    def test_first_call_allowed(self):
        chatbot.st.session_state = {}
        assert chatbot._check_rate_limit() is None
        assert chatbot.st.session_state["rate_limit"]["session_count"] == 1

    def test_allows_up_to_the_burst_limit(self):
        chatbot.st.session_state = {}
        for _ in range(chatbot._RATE_LIMIT_MAX_PER_WINDOW):
            assert chatbot._check_rate_limit() is None

    def test_blocks_after_burst_limit_exceeded(self):
        chatbot.st.session_state = {}
        for _ in range(chatbot._RATE_LIMIT_MAX_PER_WINDOW):
            chatbot._check_rate_limit()
        msg = chatbot._check_rate_limit()
        assert msg is not None
        assert "fast" in msg.lower()

    def test_window_resets_after_expiry(self):
        chatbot.st.session_state = {}
        for _ in range(chatbot._RATE_LIMIT_MAX_PER_WINDOW):
            chatbot._check_rate_limit()
        assert chatbot._check_rate_limit() is not None  # blocked

        # Simulate the burst window having fully elapsed
        rl = chatbot.st.session_state["rate_limit"]
        rl["window_start"] -= chatbot._RATE_LIMIT_WINDOW_SECONDS + 1
        assert chatbot._check_rate_limit() is None  # allowed again

    def test_blocks_after_session_cap_exceeded(self):
        chatbot.st.session_state = {
            "rate_limit": {
                "window_start": __import__("time").time(),
                "window_count": 0,
                "session_count": chatbot._RATE_LIMIT_SESSION_CAP,
            }
        }
        msg = chatbot._check_rate_limit()
        assert msg is not None
        assert "session" in msg.lower()

    def test_session_cap_persists_even_after_window_reset(self):
        # A blocked session should stay blocked even once the burst
        # window would otherwise have reset — the hard cap is absolute.
        chatbot.st.session_state = {
            "rate_limit": {
                "window_start": 0,  # long expired
                "window_count": 0,
                "session_count": chatbot._RATE_LIMIT_SESSION_CAP,
            }
        }
        assert chatbot._check_rate_limit() is not None


# ─── stream_response ──────────────────────────────────────────────

class TestStreamResponse:
    """
    Validates the stream_response short-circuit:
    when the only data results are disambiguation dicts, stream_response
    must yield nothing so app.py falls through to the disambiguation UI.
    """

    def test_disambiguation_only_yields_nothing(self):
        disambig = {"type": "selection_required", "message": "Which Josh?", "matches": []}
        gen = chatbot.stream_response(
            "josh allen", {"player": disambig}, [], {}
        )
        chunks = list(gen)
        assert chunks == [], (
            "stream_response must yield no tokens when all results are disambiguation dicts"
        )

    def test_string_data_triggers_streaming(self):
        # When there's real string data, _stream_gemini is called.
        # We mock it to return a known token.
        with mock.patch.object(chatbot, "_stream_gemini", return_value=iter(["✅ test"])):
            gen = chatbot.stream_response(
                "bills score", {"scores": "Bills 24 – Pats 17"}, [], {}
            )
            chunks = list(gen)
        assert "✅ test" in chunks

    def test_mixed_dict_and_string_still_streams(self):
        # Disambiguation dict + a real string result: streaming should proceed
        # (the dict is skipped by the non_dict filter).
        disambig = {"type": "selection_required", "message": "Who?", "matches": []}
        with mock.patch.object(chatbot, "_stream_gemini", return_value=iter(["streamed"])):
            gen = chatbot.stream_response(
                "test", {"player": disambig, "scores": "Bills win"}, [], {}
            )
            chunks = list(gen)
        assert "streamed" in chunks


# ─── _extract_intent error fallback ───────────────────────────────

class TestExtractIntentFallback:
    """
    _extract_intent must degrade gracefully when Gemini returns an error
    sentinel or malformed JSON — never raise, always return a valid dict.
    """

    def test_config_error_returns_general_intent(self):
        with mock.patch.object(chatbot, "_call_gemini",
                               return_value="__CONFIG_ERROR__: no key"):
            result = chatbot._extract_intent("bills score", {})
        assert result["intents"] == ["general"]
        assert "__error" in result

    def test_api_error_returns_general_intent(self):
        with mock.patch.object(chatbot, "_call_gemini",
                               return_value="__API_ERROR__"):
            result = chatbot._extract_intent("bills score", {})
        assert result["intents"] == ["general"]

    def test_malformed_json_returns_general_intent(self):
        with mock.patch.object(chatbot, "_call_gemini",
                               return_value="this is not json {{{"):
            result = chatbot._extract_intent("bills score", {})
        assert result["intents"] == ["general"]
        assert "player" in result
        assert "team" in result

    def test_empty_intents_list_still_dispatches(self):
        # _dispatch must not crash when Gemini returns intents=[]
        parsed = {"intents": [], "team": None, "player": None,
                  "player_b": None, "raw_query": "test"}
        result = chatbot._dispatch(parsed)
        # Falls back to ["general"] internally — general returns None
        assert result.get("general") is None
