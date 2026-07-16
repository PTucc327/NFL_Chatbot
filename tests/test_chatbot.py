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
_spec = _ilu.spec_from_file_location("src.chatbot", "NFL_Chatbot/src/chatbot.py")
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
