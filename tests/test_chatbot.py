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
        results, _chart = chatbot._dispatch(parsed)
        return results

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
        results, _chart = chatbot._dispatch(parsed)
        # Falls back to ["general"] internally — general returns None
        assert results.get("general") is None


# ─── App-wide Gemini budget & quota handling ──────────────────────

class _FakeQuotaError(Exception):
    """Shaped like google.genai.errors.APIError for a 429."""
    code = 429
    details = {"error": {"details": [{"violations": [
        {"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]}]}}


@pytest.fixture
def fresh_budget():
    chatbot._budget.update(minute=[], day=None, day_count=0)
    chatbot._model_cooldown.clear()
    yield
    chatbot._budget.update(minute=[], day=None, day_count=0)
    chatbot._model_cooldown.clear()


class TestGlobalBudget:
    def test_per_minute_cap(self, fresh_budget):
        with mock.patch.object(chatbot, "_GLOBAL_MAX_PER_MINUTE", 2):
            assert chatbot._check_global_budget() is None
            assert chatbot._check_global_budget() is None
            assert chatbot._check_global_budget() == chatbot.BUSY_MESSAGE

    def test_daily_cap(self, fresh_budget):
        with mock.patch.object(chatbot, "_GLOBAL_MAX_PER_MINUTE", 0), \
             mock.patch.object(chatbot, "_GLOBAL_MAX_PER_DAY", 3):
            for _ in range(3):
                assert chatbot._check_global_budget() is None
            assert chatbot._check_global_budget() == chatbot.DAILY_LIMIT_MESSAGE

    def test_all_models_cooling_down_is_busy(self, fresh_budget):
        for m in chatbot.GEMINI_MODELS:
            chatbot._model_cooldown[m] = chatbot.time.time() + 30
        assert chatbot._check_global_budget() == chatbot.BUSY_MESSAGE

    def test_all_models_out_for_the_day(self, fresh_budget):
        for m in chatbot.GEMINI_MODELS:
            chatbot._model_cooldown[m] = chatbot.time.time() + 3 * 60 * 60
        assert chatbot._check_global_budget() == chatbot.DAILY_LIMIT_MESSAGE


class TestQuotaFallback:
    DATA = {"scores": "Bills **24** @ Patriots **17**"}

    def test_quota_before_any_text_shows_raw_data(self):
        with mock.patch.object(chatbot, "_stream_gemini",
                               return_value=iter([chatbot.QUOTA_ERROR])):
            out = "".join(chatbot.stream_response("scores?", self.DATA, [], {}))
        assert "Bills **24**" in out and "busy" in out
        assert chatbot.QUOTA_ERROR not in out

    def test_error_mid_stream_is_not_printed(self):
        with mock.patch.object(chatbot, "_stream_gemini",
                               return_value=iter(["The Bills won", chatbot.API_ERROR])):
            out = "".join(chatbot.stream_response("scores?", self.DATA, [], {}))
        assert out.startswith("The Bills won") and "cut off" in out
        assert chatbot.API_ERROR not in out

    def test_config_error_still_reaches_app(self):
        with mock.patch.object(chatbot, "_stream_gemini",
                               return_value=iter([chatbot.CONFIG_ERROR])):
            out = list(chatbot.stream_response("scores?", self.DATA, [], {}))
        assert out == [chatbot.CONFIG_ERROR]

    def test_quota_during_intent_extraction_returns_busy(self, fresh_budget):
        chatbot.st.session_state = {}
        with mock.patch.object(chatbot, "_call_gemini", return_value=chatbot.QUOTA_ERROR), \
             mock.patch.object(chatbot, "_dispatch") as mock_dispatch:
            result = chatbot.nfl_chatbot_with_context("how did the bills do")
        assert result == chatbot.BUSY_MESSAGE
        mock_dispatch.assert_not_called()


class TestDispatchTimeout:
    def test_slow_intent_does_not_block_reply(self):
        import threading
        release = threading.Event()

        def slow(*_):
            release.wait(5)
            return "late"

        parsed = {"intents": ["scores", "standings"], "team": "Buffalo Bills",
                  "player": None, "player_b": None, "raw_query": "test"}
        with mock.patch.dict(chatbot._INTENT_DISPATCH, {"standings": slow}), \
             mock.patch.object(chatbot, "_DISPATCH_TIMEOUT_SECONDS", 0.3):
            start = chatbot.time.time()
            results, _ = chatbot._dispatch(parsed)
            elapsed = chatbot.time.time() - start
        release.set()
        assert elapsed < 2
        assert results["scores"] == "Bills 24 @ Patriots 17"
        assert "took too long" in results["standings"]


# ─── Free-tier model chain ────────────────────────────────────────

class _ApiErr(Exception):
    def __init__(self, code, details=None):
        super().__init__(f"{code}")
        self.code, self.details = code, details or {}


def _quota(quota_id, retry=None):
    details = [{"violations": [{"quotaId": quota_id, "quotaValue": "20"}]}]
    if retry:
        details.append({"retryDelay": retry})
    return _ApiErr(429, {"error": {"details": details}})


class _FakeModels:
    """behavior: model -> Exception to raise, or text to return/stream."""
    def __init__(self, behavior):
        self.behavior, self.calls = behavior, []

    def _run(self, model):
        self.calls.append(model)
        b = self.behavior[model]
        if isinstance(b, Exception):
            raise b
        return b

    def generate_content(self, model, **_):
        return mock.Mock(text=self._run(model))

    def generate_content_stream(self, model, **_):
        b = self._run(model)
        if isinstance(b, list):  # [chunk, ..., Exception] = fails mid-stream
            def gen():
                for part in b:
                    if isinstance(part, Exception):
                        raise part
                    yield mock.Mock(text=part)
            return gen()
        return iter([mock.Mock(text=b)])


@pytest.fixture
def chain(fresh_budget):
    """Three-model chain with a fake client; yields a setter for behaviors."""
    models = ["m-lite", "m-flash", "m-last"]
    fake = mock.Mock()
    with mock.patch.object(chatbot, "GEMINI_MODELS", models), \
         mock.patch.object(chatbot, "_get_gemini_client", return_value=fake):
        def setup(behavior):
            fake.models = _FakeModels(behavior)
            return fake.models
        yield setup


class TestModelChain:
    def test_daily_quota_falls_through_and_pauses_until_midnight(self, chain):
        fm = chain({"m-lite": _quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier"),
                    "m-flash": '{"ok": 1}', "m-last": "unused"})
        assert chatbot._call_gemini("sys", "q") == '{"ok": 1}'
        assert fm.calls == ["m-lite", "m-flash"]
        assert chatbot._model_cooldown["m-lite"] - chatbot.time.time() > 60
        assert chatbot._available_models() == ["m-flash", "m-last"]

    def test_per_minute_quota_uses_retry_delay(self, chain):
        chain({"m-lite": _quota("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "7s"),
               "m-flash": "ok", "m-last": "unused"})
        chatbot._call_gemini("sys", "q")
        wait = chatbot._model_cooldown["m-lite"] - chatbot.time.time()
        assert 5 < wait <= 8.5

    def test_retired_model_is_skipped(self, chain):
        fm = chain({"m-lite": _ApiErr(404), "m-flash": "ok", "m-last": "unused"})
        assert chatbot._call_gemini("sys", "q") == "ok"
        chatbot._call_gemini("sys", "q")
        assert fm.calls == ["m-lite", "m-flash", "m-flash"]

    def test_bad_request_does_not_burn_other_models(self, chain):
        fm = chain({"m-lite": _ApiErr(400), "m-flash": "ok", "m-last": "ok"})
        assert chatbot._call_gemini("sys", "q") == chatbot.API_ERROR
        assert fm.calls == ["m-lite"]

    def test_every_model_exhausted_returns_quota_error(self, chain):
        q = _quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier")
        chain({"m-lite": q, "m-flash": q, "m-last": q})
        assert chatbot._call_gemini("sys", "q") == chatbot.QUOTA_ERROR
        assert chatbot._check_global_budget() == chatbot.DAILY_LIMIT_MESSAGE

    def test_stream_falls_through_before_first_token(self, chain):
        chain({"m-lite": _ApiErr(503), "m-flash": "streamed answer", "m-last": "unused"})
        assert list(chatbot._stream_gemini("sys", "q")) == ["streamed answer"]

    def test_stream_failure_mid_answer_is_not_retried(self, chain):
        fm = chain({"m-lite": ["Partial ", _ApiErr(503)], "m-flash": "x", "m-last": "x"})
        assert list(chatbot._stream_gemini("sys", "q")) == ["Partial ", chatbot.API_ERROR]
        assert fm.calls == ["m-lite"]

    def test_logs_quota_limit_value(self, chain, caplog):
        chain({"m-lite": _quota("GenerateRequestsPerDayPerProjectPerModel-FreeTier"),
               "m-flash": "ok", "m-last": "ok"})
        with caplog.at_level("WARNING"):
            chatbot._call_gemini("sys", "q")
        assert "GenerateRequestsPerDayPerProjectPerModel-FreeTier=20" in caplog.text


# ─── Presets (sidebar buttons) and the shared answer cache ────────

@pytest.fixture
def preset_env(fresh_budget):
    chatbot._answer_cache.clear()
    chatbot.st.session_state = {}
    yield
    chatbot._answer_cache.clear()


class TestPresets:
    PRESET = {"intents": ["standings"], "team": "Buffalo Bills"}

    def _ask(self, stream_chunks):
        with mock.patch.object(chatbot, "_call_gemini") as mock_extract, \
             mock.patch.object(chatbot, "_stream_gemini",
                               return_value=iter(stream_chunks)) as mock_stream:
            resp = chatbot.nfl_chatbot_with_context("Bills standings?", preset=self.PRESET)
            text = "".join(resp.stream)
        return text, mock_extract, mock_stream

    def test_preset_skips_intent_extraction(self, preset_env):
        text, mock_extract, _ = self._ask(["Bills are 3-1."])
        mock_extract.assert_not_called()
        assert text == "Bills are 3-1."

    def test_second_identical_preset_is_served_from_cache(self, preset_env):
        self._ask(["Bills are 3-1."])
        text, _, mock_stream = self._ask(["should not be used"])
        mock_stream.assert_not_called()
        assert text == "Bills are 3-1."

    def test_cached_answer_expires(self, preset_env):
        self._ask(["Bills are 3-1."])
        for k, (at, t, c) in list(chatbot._answer_cache.items()):
            chatbot._answer_cache[k] = (at - chatbot._ANSWER_CACHE_TTL - 1, t, c)
        text, _, mock_stream = self._ask(["Bills are 4-1."])
        mock_stream.assert_called_once()
        assert text == "Bills are 4-1."

    def test_raw_data_fallback_is_not_cached(self, preset_env):
        self._ask([chatbot.QUOTA_ERROR])
        assert chatbot._answer_cache == {}

    def test_cut_off_answer_is_not_cached(self, preset_env):
        self._ask(["The Bills", chatbot.API_ERROR])
        assert chatbot._answer_cache == {}

    def test_typed_questions_are_never_cached(self, preset_env):
        with mock.patch.object(chatbot, "_call_gemini",
                               return_value='{"intents":["standings"],"team":"Buffalo Bills"}'), \
             mock.patch.object(chatbot, "_stream_gemini", return_value=iter(["ok"])):
            "".join(chatbot.nfl_chatbot_with_context("Bills standings?").stream)
        assert chatbot._answer_cache == {}

    def test_position_preset_is_not_remembered_as_player(self, preset_env):
        with mock.patch.object(chatbot, "_stream_gemini", return_value=iter(["ok"])):
            chatbot.nfl_chatbot_with_context(
                "WR waivers?", preset={"intents": ["waiver"], "player": "WR"})
        assert "last_player" not in chatbot.st.session_state


# ─── Schedules, leaders, player-team fallback, season context ─────

_api_mock.get_week_schedule.return_value = "Week 5 slate"
_api_mock.get_team_schedule.return_value = "Team schedule"
_api_mock.get_league_leaders.return_value = "Leaders table"
_api_mock.current_nfl_season_year.return_value = 2026
_api_mock.current_nfl_week.return_value = 5


class TestNewRouting:
    def _run(self, **parsed):
        base = {"intents": [], "team": None, "player": None, "player_b": None,
                "raw_query": "test"}
        results, _ = chatbot._dispatch({**base, **parsed})
        return results

    def test_schedule_without_team_is_league_slate(self):
        assert self._run(intents=["schedule"])["schedule"] == "Week 5 slate"

    def test_schedule_with_team_and_opponent(self):
        _api_mock.get_team_schedule.reset_mock()
        self._run(intents=["schedule"], team="Dallas Cowboys", opponent="Philadelphia Eagles")
        _api_mock.get_team_schedule.assert_called_once_with(
            "Dallas Cowboys", opponent="Philadelphia Eagles")

    def test_leaders_passes_stat_and_position(self):
        _api_mock.get_league_leaders.reset_mock()
        self._run(intents=["leaders"], player="QB", stat="pts_ppr")
        _api_mock.get_league_leaders.assert_called_once_with("pts_ppr", position="QB")

    def test_leaders_defaults_to_fantasy_points(self):
        _api_mock.get_league_leaders.reset_mock()
        self._run(intents=["leaders"])
        _api_mock.get_league_leaders.assert_called_once_with("pts_ppr", position=None)

    def test_player_team_fills_team_level_intents(self):
        _api_mock.get_player_team.reset_mock()
        _api_mock.get_player_team.return_value = "KC"
        _api_mock.get_team_schedule.reset_mock()
        self._run(intents=["injury", "schedule"], player="Patrick Mahomes")
        _api_mock.get_player_team.assert_called_once_with("Patrick Mahomes")
        _api_mock.get_team_schedule.assert_called_once_with("KC", opponent=None)

    def test_position_is_not_looked_up_as_player(self):
        _api_mock.get_player_team.reset_mock()
        self._run(intents=["schedule"], player="WR")
        _api_mock.get_player_team.assert_not_called()


class TestSeasonContext:
    def test_extraction_prompt_includes_date_and_season(self):
        with mock.patch.object(chatbot, "_call_gemini", return_value='{"intents":["general"]}') as call:
            chatbot._extract_intent("Saquon's stats this season", {})
        prompt = call.call_args[0][1]
        assert "Current NFL season: 2026 (Week 5)" in prompt
        assert str(chatbot.datetime.date.today().year) in prompt

    def test_format_prompt_includes_season(self):
        prompt = chatbot._build_format_prompt("q", {"scores": "x"}, [], {})
        assert "Current NFL season: 2026" in prompt


class TestCharts:
    def test_two_players_merge_into_labeled_series(self):
        charts = {
            "CeeDee Lamb": {"name": "CeeDee Lamb", "weeks": ["Wk 1", "Wk 2", "Wk 10"], "pts": [20, 30, 40]},
            "Ja'Marr Chase": {"name": "Ja'Marr Chase", "weeks": ["Wk 2", "Wk 3"], "pts": [26.5, 24.8]},
        }
        with mock.patch.object(chatbot, "get_player_chart_data",
                               side_effect=lambda name, team=None: charts.get(name)):
            chart = chatbot._build_chart([("CeeDee Lamb", None), ("Ja'Marr Chase", None)])
        assert chart["weeks"] == ["Wk 1", "Wk 2", "Wk 3", "Wk 10"]  # numeric order
        assert chart["series"]["CeeDee Lamb"] == [20, 30, None, 40]
        assert chart["series"]["Ja'Marr Chase"] == [None, 26.5, 24.8, None]

    def test_comparison_prefers_two_player_chart(self):
        two = {"weeks": ["Wk 1"], "series": {"A": [1], "B": [2]}}
        one = {"weeks": ["Wk 1"], "series": {"A": [1]}}
        with mock.patch.dict(chatbot._INTENT_DISPATCH, {
                "fantasy": lambda *_: {"_text": "f", "chart_data": one},
                "comparison": lambda *_: {"_text": "c", "chart_data": two}}):
            _, chart = chatbot._dispatch({"intents": ["fantasy", "comparison"],
                                          "raw_query": "x"})
        assert chart is two
