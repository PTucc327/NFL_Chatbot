"""
Tests for src/api_client.py

Loads the real module via importlib so it is never affected by mocks in other
test files. All HTTP calls are patched out with unittest.mock.
"""
import datetime
import importlib.util
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# ─── Load the real modules directly ──────────────────────────────

def _load_module(name, rel_path):
    path = os.path.join(os.path.dirname(__file__), "..", rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod

_utils_mod  = _load_module("_real_src_utils",  "src/utils.py")
_client_mod = _load_module("_real_src_api",    "src/api_client.py")

# Convenience references
get_player_profile_smart = _client_mod.get_player_profile_smart
get_player_injury        = _client_mod.get_player_injury
get_fantasy_player_stats = _client_mod.get_fantasy_player_stats
get_player_comparison    = _client_mod.get_player_comparison
get_trade_analysis       = _client_mod.get_trade_analysis
_build_lookup            = _client_mod._build_lookup


# ─── Shared fake data ─────────────────────────────────────────────

FAKE_PLAYERS = {
    "4984": {
        "player_id": "4984", "full_name": "Josh Allen", "first_name": "Josh",
        "last_name": "Allen", "position": "QB", "team": "BUF", "active": True,
        "years_exp": 8, "injury_status": None, "injury_body_part": None,
        "injury_notes": None, "practice_participation": None,
        "depth_chart_position": "QB", "depth_chart_order": 1,
    },
    "2212": {
        "player_id": "2212", "full_name": "Josh Allen", "first_name": "Josh",
        "last_name": "Allen", "position": "G", "team": None, "active": False,
        "years_exp": 3, "injury_status": None, "injury_body_part": None,
        "injury_notes": None, "practice_participation": None,
        "depth_chart_position": None, "depth_chart_order": None,
    },
    "6794": {
        "player_id": "6794", "full_name": "Patrick Mahomes", "first_name": "Patrick",
        "last_name": "Mahomes", "position": "QB", "team": "KC", "active": True,
        "years_exp": 9, "injury_status": "Questionable", "injury_body_part": "Ankle",
        "injury_notes": "Day-to-day", "practice_participation": "Limited",
        "depth_chart_position": "QB", "depth_chart_order": 1,
    },
}

FAKE_STATS = {
    "4984": {"pts_ppr": 312.5},
    "6794": {"pts_ppr": 298.0},
}


@pytest.fixture(autouse=True)
def inject_cache():
    """Put fake player data in the module's cache before each test."""
    _client_mod._PLAYER_CACHE      = FAKE_PLAYERS
    _client_mod._PLAYER_CACHE_LAST = datetime.datetime.now().timestamp() + 9999
    yield
    _client_mod._PLAYER_CACHE      = {}
    _client_mod._PLAYER_CACHE_LAST = 0


# ─── _current_nfl_season_year ─────────────────────────────────────

class TestCurrentNflSeasonYear:
    def test_offseason_returns_previous_year(self):
        # Patch datetime.datetime.now inside the real api_client module
        fake_now = datetime.datetime(2026, 7, 1)
        with patch.object(_client_mod.datetime, "datetime",
                          wraps=datetime.datetime) as mock_dt:
            mock_dt.now.return_value = fake_now
            result = _client_mod._current_nfl_season_year()
        assert result == 2025

    def test_in_season_returns_current_year(self):
        fake_now = datetime.datetime(2025, 10, 15)
        with patch.object(_client_mod.datetime, "datetime",
                          wraps=datetime.datetime) as mock_dt:
            mock_dt.now.return_value = fake_now
            result = _client_mod._current_nfl_season_year()
        assert result == 2025


# ─── get_player_profile_smart ─────────────────────────────────────

class TestGetPlayerProfileSmart:
    def test_active_josh_allen_returned(self):
        with patch.object(_client_mod, "get_fantasy_player_stats", return_value="312 PPR pts"):
            result = get_player_profile_smart("josh allen")
        assert isinstance(result, str)
        assert "Josh Allen" in result
        assert "BUF" in result

    def test_profile_contains_position(self):
        with patch.object(_client_mod, "get_fantasy_player_stats", return_value="312 PPR pts"):
            result = get_player_profile_smart("josh allen")
        assert "QB" in result

    def test_profile_shows_depth_chart(self):
        with patch.object(_client_mod, "get_fantasy_player_stats", return_value="312 PPR pts"):
            result = get_player_profile_smart("josh allen")
        assert "Starter" in result

    def test_injured_player_shows_status(self):
        with patch.object(_client_mod, "get_fantasy_player_stats", return_value="298 PPR pts"):
            result = get_player_profile_smart("patrick mahomes")
        assert "Questionable" in result

    def test_unknown_player_not_found(self):
        result = get_player_profile_smart("zxcvbnm qwerty")
        assert isinstance(result, str)
        assert "couldn't find" in result.lower()

    def test_legend_returns_legend_card(self):
        _client_mod._LEGENDS = _build_lookup([{
            "name": "Tom Brady", "status": "Retired (HOF 2028)",
            "teams": "Patriots, Buccaneers", "stats": "89,214 Yds", "awards": "7x SB Champ",
        }])
        result = get_player_profile_smart("tom brady")
        assert "Legend" in result
        assert "HOF" in result
        _client_mod._LEGENDS = {}  # clean up


# ─── get_player_injury ────────────────────────────────────────────

class TestGetPlayerInjury:
    def test_healthy_player_shows_healthy(self):
        result = get_player_injury("josh allen")
        assert "Josh Allen" in result
        assert "Healthy" in result

    def test_injured_player_shows_status(self):
        result = get_player_injury("patrick mahomes")
        assert "Questionable" in result

    def test_injured_player_shows_body_part(self):
        result = get_player_injury("patrick mahomes")
        assert "Ankle" in result

    def test_injured_player_shows_practice(self):
        result = get_player_injury("patrick mahomes")
        assert "Limited" in result

    def test_shows_depth_chart(self):
        result = get_player_injury("patrick mahomes")
        assert "Starter" in result or "Depth Chart" in result

    def test_unknown_player(self):
        result = get_player_injury("nobody special here")
        assert "couldn't find" in result.lower()


# ─── get_fantasy_player_stats ─────────────────────────────────────

class TestGetFantasyPlayerStats:
    def test_known_player_returns_points(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_STATS):
            result = get_fantasy_player_stats("josh allen")
        assert "312" in result
        assert "PPR" in result

    def test_unknown_player_returns_not_found(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_STATS):
            result = get_fantasy_player_stats("zxcvbnm nobody")
        assert "not seeing" in result.lower() or "no" in result.lower()


# ─── get_player_comparison ────────────────────────────────────────

class TestGetPlayerComparison:
    def test_both_players_in_output(self):
        with patch.object(_client_mod, "get_player_weekly_stats", return_value="Wk 17: 30 pts"), \
             patch.object(_client_mod, "get_fantasy_player_stats", return_value="pts"):
            result = get_player_comparison("josh allen", "patrick mahomes")
        assert "Josh Allen" in result
        assert "Patrick Mahomes" in result

    def test_output_has_player_sections(self):
        with patch.object(_client_mod, "get_player_weekly_stats", return_value="stats"), \
             patch.object(_client_mod, "get_fantasy_player_stats", return_value="pts"):
            result = get_player_comparison("josh allen", "patrick mahomes")
        assert "PLAYER 1" in result
        assert "PLAYER 2" in result


# ─── get_trade_analysis ───────────────────────────────────────────

class TestGetTradeAnalysis:
    def test_both_players_in_output(self):
        with patch.object(_client_mod, "get_player_weekly_stats", return_value="stats"), \
             patch.object(_client_mod, "get_fantasy_player_stats", return_value="pts"), \
             patch.object(_client_mod, "get_next_game", return_value="Bills vs Chiefs"):
            result = get_trade_analysis("josh allen", "patrick mahomes")
        assert "Josh Allen" in result
        assert "Patrick Mahomes" in result

    def test_output_has_give_receive_sections(self):
        with patch.object(_client_mod, "get_player_weekly_stats", return_value="stats"), \
             patch.object(_client_mod, "get_fantasy_player_stats", return_value="pts"), \
             patch.object(_client_mod, "get_next_game", return_value="game"):
            result = get_trade_analysis("josh allen", "patrick mahomes")
        assert "GIVING AWAY" in result
        assert "RECEIVING" in result


# ─── get_waiver_recommendations ──────────────────────────────────

# Fake free-agent players (no team assigned)
FAKE_FREE_AGENTS = {
    "fa_001": {
        "player_id": "fa_001", "full_name": "DeAndre Hopkins",
        "position": "WR", "team": None, "active": True,
        "injury_status": None, "injury_body_part": None,
    },
    "fa_002": {
        "player_id": "fa_002", "full_name": "Odell Beckham Jr",
        "position": "WR", "team": None, "active": True,
        "injury_status": "Questionable", "injury_body_part": "Hamstring",
    },
    "fa_003": {
        "player_id": "fa_003", "full_name": "Cam Akers",
        "position": "RB", "team": None, "active": True,
        "injury_status": None, "injury_body_part": None,
    },
}

FAKE_WEEK_STATS = {
    "fa_001": {"pts_ppr": 18.4},
    "fa_002": {"pts_ppr": 12.1},
    "fa_003": {"pts_ppr": 9.7},
}


class TestGetWaiverRecommendations:
    @pytest.fixture(autouse=True)
    def inject_fa_cache(self):
        _client_mod._PLAYER_CACHE      = FAKE_FREE_AGENTS
        _client_mod._PLAYER_CACHE_LAST = datetime.datetime.now().timestamp() + 9999
        yield
        _client_mod._PLAYER_CACHE      = {}
        _client_mod._PLAYER_CACHE_LAST = 0

    def test_returns_players_with_recent_points(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_WEEK_STATS):
            result = _client_mod.get_waiver_recommendations()
        assert "DeAndre Hopkins" in result

    def test_position_filter_wr(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_WEEK_STATS):
            result = _client_mod.get_waiver_recommendations(position="WR")
        assert "WR" in result
        # RB should not appear in a WR-only query
        assert "Cam Akers" not in result

    def test_injured_player_flagged(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_WEEK_STATS):
            result = _client_mod.get_waiver_recommendations()
        # Questionable player should show injury warning
        if "Odell Beckham Jr" in result:
            assert "Questionable" in result or "⚠️" in result

    def test_invalid_position_returns_message(self):
        result = _client_mod.get_waiver_recommendations(position="QB1")
        assert "recognised" in result.lower() or "not" in result.lower()

    def test_output_has_ranking_header(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_WEEK_STATS):
            result = _client_mod.get_waiver_recommendations()
        assert "Waiver Wire" in result or "waiver" in result.lower()
