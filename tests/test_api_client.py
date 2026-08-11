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


# ─── Shared ESPN-shaped payloads ──────────────────────────────────

def _make_event(away_name, home_name, away_score, home_score,
                state="post", detail="Final", date="2025-09-07T20:00:00Z",
                venue="Highmark Stadium", odds=None):
    """Builds a minimal ESPN scoreboard event dict."""
    comp = {
        "competitors": [
            {"homeAway": "away", "team": {"displayName": away_name}, "score": away_score},
            {"homeAway": "home", "team": {"displayName": home_name}, "score": home_score},
        ],
        "status": {"type": {"state": state, "shortDetail": detail}},
        "venue": {"fullName": venue},
    }
    if odds:
        comp["odds"] = odds
    return {"date": date, "competitions": [comp]}


FAKE_SCOREBOARD = {
    "events": [
        _make_event("Buffalo Bills", "New England Patriots", "24", "17",
                    state="post", detail="Final"),
        _make_event("Kansas City Chiefs", "Los Angeles Chargers", "28", "21",
                    state="in", detail="3rd Qtr"),
        _make_event("Dallas Cowboys", "Philadelphia Eagles", "0", "0",
                    state="pre", detail="8:20 PM ET",
                    date="2099-09-14T00:20:00Z"),   # far future for "pre"
    ]
}

FAKE_STANDINGS = {
    "children": [
        {
            "name": "AFC",
            "standings": {
                "entries": [
                    {
                        "team": {"displayName": "Buffalo Bills"},
                        "stats": [
                            {"name": "wins",   "displayValue": "13"},
                            {"name": "losses", "displayValue": "4"},
                            {"name": "ties",   "displayValue": "0"},
                        ],
                    },
                    {
                        "team": {"displayName": "Kansas City Chiefs"},
                        "stats": [
                            {"name": "wins",   "displayValue": "14"},
                            {"name": "losses", "displayValue": "3"},
                            {"name": "ties",   "displayValue": "0"},
                        ],
                    },
                ]
            },
        }
    ]
}

FAKE_SCHEDULE = {
    "events": [
        # Past game
        {
            "date": "2025-09-07T20:00:00Z",
            "competitions": [{
                "competitors": [
                    {"homeAway": "away", "team": {"displayName": "Buffalo Bills"},
                     "score": {"displayValue": "24"}},
                    {"homeAway": "home", "team": {"displayName": "New England Patriots"},
                     "score": {"displayValue": "17"}},
                ],
                "status": {"type": {"state": "post", "shortDetail": "Final"}},
            }],
        },
        # Future game
        {
            "date": "2099-09-14T20:00:00Z",
            "competitions": [{
                "competitors": [
                    {"homeAway": "away", "team": {"displayName": "Buffalo Bills"},
                     "score": {"displayValue": "0"}},
                    {"homeAway": "home", "team": {"displayName": "Kansas City Chiefs"},
                     "score": {"displayValue": "0"}},
                ],
                "status": {"type": {"state": "pre", "shortDetail": "8:20 PM ET"}},
            }],
        },
    ]
}

FAKE_TEAM_META = {
    "id": "2",
    "displayName": "Buffalo Bills",
    "abbr": "buf",
    "slug": "buffalo-bills",
    "schedule_url": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/2/schedule",
}


# ─── get_live_scores ──────────────────────────────────────────────

class TestGetLiveScores:
    def test_returns_scoreboard_header(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_SCOREBOARD):
            result = _client_mod.get_live_scores()
        assert "NFL Scoreboard" in result

    def test_shows_final_game(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_SCOREBOARD):
            result = _client_mod.get_live_scores()
        assert "Bills" in result or "Buffalo" in result

    def test_shows_live_game(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_SCOREBOARD):
            result = _client_mod.get_live_scores()
        assert "Live Right Now" in result or "3rd Qtr" in result

    def test_team_filter_returns_only_that_team(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_SCOREBOARD):
            result = _client_mod.get_live_scores("Buffalo Bills")
        # Bills appear
        assert "Bills" in result or "Buffalo" in result
        # Chiefs-Chargers game should be excluded
        assert "Kansas City" not in result

    def test_api_error_returns_friendly_message(self):
        with patch.object(_client_mod, "fetch_json", return_value={"__error": "timeout"}):
            result = _client_mod.get_live_scores()
        assert "trouble" in result.lower() or "error" in result.lower()

    def test_empty_events_returns_no_games_message(self):
        with patch.object(_client_mod, "fetch_json", return_value={"events": []}):
            result = _client_mod.get_live_scores()
        assert "right now" in result.lower() or "schedule" in result.lower()


# ─── get_standings ────────────────────────────────────────────────

class TestGetStandings:
    def test_returns_standings_header(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_STANDINGS), \
             patch.object(_client_mod, "find_team", return_value=None):
            result = _client_mod.get_standings()
        assert "Standings" in result

    def test_contains_team_records(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_STANDINGS), \
             patch.object(_client_mod, "find_team", return_value=None):
            result = _client_mod.get_standings()
        assert "13-4" in result or "14-3" in result

    def test_team_specific_query(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_STANDINGS), \
             patch.object(_client_mod, "find_team", return_value=FAKE_TEAM_META):
            result = _client_mod.get_standings("Buffalo Bills")
        assert "Buffalo Bills" in result
        assert "13-4" in result

    def test_unknown_team_returns_not_found(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_STANDINGS), \
             patch.object(_client_mod, "find_team", return_value={"displayName": "Fake Team FC"}):
            result = _client_mod.get_standings("Fake Team FC")
        assert "couldn't find" in result.lower()

    def test_api_error_returns_friendly_message(self):
        with patch.object(_client_mod, "fetch_json", return_value={"__error": "timeout"}):
            result = _client_mod.get_standings()
        assert "trouble" in result.lower() or "⚠️" in result


# ─── get_next_game & get_last_game ────────────────────────────────

class TestGetNextGame:
    def test_returns_opponent_name(self):
        with patch.object(_client_mod, "find_team", return_value=FAKE_TEAM_META), \
             patch.object(_client_mod, "fetch_json", return_value=FAKE_SCHEDULE):
            result = _client_mod.get_next_game("Buffalo Bills")
        assert "Kansas City" in result or "Chiefs" in result

    def test_returns_date_string(self):
        with patch.object(_client_mod, "find_team", return_value=FAKE_TEAM_META), \
             patch.object(_client_mod, "fetch_json", return_value=FAKE_SCHEDULE):
            result = _client_mod.get_next_game("Buffalo Bills")
        # Should contain a year or day-of-week or time
        assert any(x in result for x in ["ET", "2099", "Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"])

    def test_unknown_team_returns_not_found(self):
        with patch.object(_client_mod, "find_team", return_value=None):
            result = _client_mod.get_next_game("Fake Team")
        assert "couldn't" in result.lower() or "find" in result.lower()

    def test_no_future_games_returns_message(self):
        schedule_no_future = {"events": [FAKE_SCHEDULE["events"][0]]}  # only past game
        with patch.object(_client_mod, "find_team", return_value=FAKE_TEAM_META), \
             patch.object(_client_mod, "fetch_json", return_value=schedule_no_future):
            result = _client_mod.get_next_game("Buffalo Bills")
        assert "don't have" in result.lower() or "lined up" in result.lower()


class TestGetLastGame:
    def test_returns_score(self):
        with patch.object(_client_mod, "find_team", return_value=FAKE_TEAM_META), \
             patch.object(_client_mod, "fetch_json", return_value=FAKE_SCHEDULE):
            result = _client_mod.get_last_game("Buffalo Bills")
        assert "24" in result or "17" in result

    def test_returns_both_team_names(self):
        with patch.object(_client_mod, "find_team", return_value=FAKE_TEAM_META), \
             patch.object(_client_mod, "fetch_json", return_value=FAKE_SCHEDULE):
            result = _client_mod.get_last_game("Buffalo Bills")
        assert "Buffalo Bills" in result or "Bills" in result

    def test_unknown_team_returns_not_found(self):
        with patch.object(_client_mod, "find_team", return_value=None):
            result = _client_mod.get_last_game("Fake Team")
        assert "not finding" in result.lower() or "find" in result.lower()

    def test_no_past_games_returns_message(self):
        schedule_no_past = {"events": [FAKE_SCHEDULE["events"][1]]}  # only future game
        with patch.object(_client_mod, "find_team", return_value=FAKE_TEAM_META), \
             patch.object(_client_mod, "fetch_json", return_value=schedule_no_past):
            result = _client_mod.get_last_game("Buffalo Bills")
        assert "can't" in result.lower() or "find" in result.lower()


# ─── get_team_news & get_league_headlines ─────────────────────────

FAKE_ARTICLES = [
    {"title": "Bills sign top free agent wide receiver", "link": "https://example.com/1", "desc": "Buffalo Bills news"},
    {"title": "NFL Week 1 preview", "link": "https://example.com/2", "desc": "Around the NFL"},
]


class TestGetTeamNews:
    def test_no_team_name_returns_prompt(self):
        result = _client_mod.get_team_news("")
        assert "which team" in result.lower() or "love" in result.lower()

    def test_returns_headline_when_articles_found(self):
        with patch.object(_client_mod, "_fetch_rss_thread", return_value=FAKE_ARTICLES):
            result = _client_mod.get_team_news("Buffalo Bills")
        assert "Bills" in result or "buffalo" in result.lower() or "wide receiver" in result.lower()

    def test_quiet_message_when_no_articles_match(self):
        with patch.object(_client_mod, "_fetch_rss_thread", return_value=[]):
            result = _client_mod.get_team_news("Buffalo Bills")
        assert "quiet" in result.lower() or "couldn't" in result.lower() or "moment" in result.lower()

    def test_output_contains_markdown_link(self):
        with patch.object(_client_mod, "_fetch_rss_thread", return_value=FAKE_ARTICLES):
            result = _client_mod.get_team_news("Buffalo Bills")
        # Result should have at least one markdown link [text](url)
        assert "http" in result or "example.com" in result or "📰" in result


class TestGetLeagueHeadlines:
    def test_returns_header(self):
        with patch.object(_client_mod, "_fetch_rss_thread", return_value=FAKE_ARTICLES):
            result = _client_mod.get_league_headlines()
        assert "NFL" in result or "Around" in result or "league" in result.lower()

    def test_no_articles_returns_fallback(self):
        with patch.object(_client_mod, "_fetch_rss_thread", return_value=[]):
            result = _client_mod.get_league_headlines()
        assert "couldn't" in result.lower() or "try again" in result.lower()

    def test_deduplication_removes_duplicates(self):
        # If both RSS sources return the same headline, it should appear once
        dupe_articles = FAKE_ARTICLES + FAKE_ARTICLES  # exact duplicates
        with patch.object(_client_mod, "_fetch_rss_thread", return_value=dupe_articles):
            result = _client_mod.get_league_headlines(limit=10)
        # Should only appear once even though the source returned it twice
        assert result.count("Bills sign top free agent") == 1


# ─── get_player_weekly_stats ──────────────────────────────────────

FAKE_WEEKLY_DATA = {
    "4984": {
        "pts_ppr": 28.5,
        "pass_yd": 312, "pass_td": 3, "pass_int": 0, "rush_yd": 42,
    }
}

FAKE_WEEKLY_DATA_RB = {
    "rb_001": {
        "pts_ppr": 22.1,
        "rush_yd": 110, "rush_td": 1, "rec": 4, "rec_yd": 35,
    }
}


class TestGetPlayerWeeklyStats:
    def test_qb_stats_line_format(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_WEEKLY_DATA):
            result = _client_mod.get_player_weekly_stats("josh allen", num_weeks=1)
        assert "Pass" in result
        assert "312" in result

    def test_unknown_player_returns_not_found(self):
        result = _client_mod.get_player_weekly_stats("zxcvbnm nobody", num_weeks=1)
        assert "no weekly stats" in result.lower() or "not found" in result.lower()

    def test_no_stats_in_cache_returns_no_data_message(self):
        # fetch_json returns empty dict — no stats for the player this week
        with patch.object(_client_mod, "fetch_json", return_value={}):
            result = _client_mod.get_player_weekly_stats("josh allen", num_weeks=1)
        assert "no weekly stats" in result.lower() or "available" in result.lower()

    def test_rb_stats_line_format(self):
        rb_player = {
            "rb_001": {
                "player_id": "rb_001", "full_name": "Derrick Henry",
                "position": "RB", "team": "BAL", "active": True,
                "injury_status": None, "injury_body_part": None,
                "depth_chart_position": "RB", "depth_chart_order": 1,
            }
        }
        _client_mod._PLAYER_CACHE = rb_player
        _client_mod._PLAYER_CACHE_LAST = datetime.datetime.now().timestamp() + 9999
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_WEEKLY_DATA_RB):
            result = _client_mod.get_player_weekly_stats("derrick henry", num_weeks=1)
        assert "Rush" in result or "Rec" in result
        # Restore
        _client_mod._PLAYER_CACHE = FAKE_PLAYERS
        _client_mod._PLAYER_CACHE_LAST = datetime.datetime.now().timestamp() + 9999


# ─── get_fantasy_sit_start ────────────────────────────────────────

class TestGetFantasySitStart:
    def test_returns_player_name_and_position(self):
        with patch.object(_client_mod, "get_player_weekly_stats", return_value="Wk 5: 28 pts"), \
             patch.object(_client_mod, "get_player_injury", return_value="Healthy"), \
             patch.object(_client_mod, "get_next_game", return_value="vs Chiefs Sunday"):
            result = _client_mod.get_fantasy_sit_start("josh allen")
        assert "Josh Allen" in result
        assert "QB" in result

    def test_returns_injury_block(self):
        with patch.object(_client_mod, "get_player_weekly_stats", return_value="Wk 5: 28 pts"), \
             patch.object(_client_mod, "get_player_injury", return_value="🏥 Josh Allen — Healthy") as mock_inj, \
             patch.object(_client_mod, "get_next_game", return_value="vs Chiefs"):
            result = _client_mod.get_fantasy_sit_start("josh allen")
        mock_inj.assert_called()
        assert "Healthy" in result or "🏥" in result

    def test_returns_matchup_block(self):
        with patch.object(_client_mod, "get_player_weekly_stats", return_value="Wk 5: 28 pts"), \
             patch.object(_client_mod, "get_player_injury", return_value="Healthy"), \
             patch.object(_client_mod, "get_next_game", return_value="vs Kansas City Chiefs Sunday") as mock_next:
            result = _client_mod.get_fantasy_sit_start("josh allen")
        mock_next.assert_called()
        assert "Upcoming Matchup" in result

    def test_opponent_context_included_when_provided(self):
        with patch.object(_client_mod, "get_player_weekly_stats", return_value="stats"), \
             patch.object(_client_mod, "get_player_injury", return_value="Healthy"), \
             patch.object(_client_mod, "get_next_game", return_value="next game"):
            result = _client_mod.get_fantasy_sit_start("josh allen", opponent_team="Kansas City Chiefs")
        assert "Kansas City Chiefs" in result

    def test_unknown_player_returns_not_found(self):
        result = _client_mod.get_fantasy_sit_start("zxcvbnm nobody")
        assert "couldn't find" in result.lower()


# ─── get_game_odds ────────────────────────────────────────────────

FAKE_SCOREBOARD_WITH_ODDS = {
    "events": [
        _make_event(
            "Buffalo Bills", "Kansas City Chiefs", "0", "0",
            state="pre", detail="8:20 PM ET",
            date="2099-09-14T00:20:00Z",
            odds=[{"details": "Bills -6.5", "overUnder": 47.5}],
        )
    ]
}


class TestGetGameOdds:
    def test_returns_spread_and_over_under(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_SCOREBOARD_WITH_ODDS):
            result = _client_mod.get_game_odds("Buffalo Bills")
        assert "-6.5" in result
        assert "47.5" in result

    def test_game_without_odds_returns_not_available_message(self):
        scoreboard_no_odds = {
            "events": [_make_event("Buffalo Bills", "New England Patriots", "0", "0")]
        }
        with patch.object(_client_mod, "fetch_json", return_value=scoreboard_no_odds):
            result = _client_mod.get_game_odds("Buffalo Bills")
        assert "aren't out" in result.lower() or "not" in result.lower()

    def test_team_not_playing_returns_no_lines_message(self):
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_SCOREBOARD_WITH_ODDS):
            result = _client_mod.get_game_odds("Green Bay Packers")
        assert "couldn't find" in result.lower() or "active" in result.lower()

    def test_api_error_returns_friendly_message(self):
        # This is the bug that was fixed — previously crashed on __error key
        with patch.object(_client_mod, "fetch_json", return_value={"__error": "timeout"}):
            result = _client_mod.get_game_odds("Buffalo Bills")
        assert "trouble" in result.lower() or "try again" in result.lower()


# ─── ensure_team_cache & detect_team_from_query ───────────────────

FAKE_TEAMS_RESPONSE = {
    "sports": [{
        "leagues": [{
            "teams": [
                {"team": {"id": "2", "displayName": "Buffalo Bills",
                          "abbreviation": "BUF", "slug": "buffalo-bills"}},
                {"team": {"id": "6", "displayName": "New York Giants",
                          "abbreviation": "NYG", "slug": "new-york-giants"}},
            ]
        }]
    }]
}


class TestEnsureTeamCache:
    def test_cache_populated_after_call(self):
        _client_mod._TEAM_CACHE      = {}
        _client_mod._TEAM_CACHE_LAST = 0
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_TEAMS_RESPONSE):
            _client_mod.ensure_team_cache()
        assert "buffalo bills" in _client_mod._TEAM_CACHE
        assert "buf" in _client_mod._TEAM_CACHE

    def test_stale_cache_is_not_refetched(self):
        # Prime cache with a valid TTL
        _client_mod._TEAM_CACHE      = {"buffalo bills": FAKE_TEAM_META}
        _client_mod._TEAM_CACHE_LAST = datetime.datetime.now().timestamp() + 9999
        with patch.object(_client_mod, "fetch_json") as mock_fetch:
            _client_mod.ensure_team_cache()
            mock_fetch.assert_not_called()

    def test_api_error_leaves_cache_empty(self):
        _client_mod._TEAM_CACHE      = {}
        _client_mod._TEAM_CACHE_LAST = 0
        with patch.object(_client_mod, "fetch_json", return_value={"__error": "network error"}):
            _client_mod.ensure_team_cache()
        assert _client_mod._TEAM_CACHE == {}


class TestDetectTeamFromQuery:
    def test_detects_full_name(self):
        _client_mod._TEAM_CACHE      = {}
        _client_mod._TEAM_CACHE_LAST = 0
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_TEAMS_RESPONSE):
            result = _client_mod.detect_team_from_query("How did the Buffalo Bills do?")
        assert result == "Buffalo Bills"

    def test_detects_nickname(self):
        # "pats" → "patriots" via NICKNAMES dict
        result = _client_mod.detect_team_from_query("How are the pats doing?")
        assert result == "patriots"

    def test_returns_none_for_unknown_team(self):
        _client_mod._TEAM_CACHE      = {}
        _client_mod._TEAM_CACHE_LAST = 0
        with patch.object(_client_mod, "fetch_json", return_value=FAKE_TEAMS_RESPONSE):
            result = _client_mod.detect_team_from_query("Who won the basketball game?")
        assert result is None
