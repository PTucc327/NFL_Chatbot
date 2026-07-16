"""
Tests for src/utils.py — imports the real module directly via importlib
so it is never affected by mocks set up in other test files.
"""
import datetime
import importlib.util
import os
import sys

# Load the real src/utils.py directly, bypassing sys.modules
_UTILS_PATH = os.path.join(os.path.dirname(__file__), "..", "src", "utils.py")
_spec = importlib.util.spec_from_file_location("_real_utils", _UTILS_PATH)
_utils = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_utils)

is_fuzzy_match     = _utils.is_fuzzy_match
clean_query        = _utils.clean_query
parse_iso_datetime = _utils.parse_iso_datetime
to_et              = _utils.to_et


# ─── is_fuzzy_match ───────────────────────────────────────────────

class TestIsFuzzyMatch:
    def test_exact_match(self):
        assert is_fuzzy_match("Josh Allen", "Josh Allen") is True

    def test_case_insensitive(self):
        assert is_fuzzy_match("josh allen", "Josh Allen") is True

    def test_typo_match(self):
        assert is_fuzzy_match("jsh allen", "Josh Allen") is True

    def test_word_order(self):
        assert is_fuzzy_match("allen josh", "Josh Allen") is True

    def test_single_token_no_match(self):
        # Single first-name must NOT match every player with that name
        assert is_fuzzy_match("josh", "Josh Allen") is False

    def test_empty_target(self):
        assert is_fuzzy_match("", "Josh Allen") is False

    def test_empty_candidate(self):
        assert is_fuzzy_match("josh allen", "") is False

    def test_completely_different(self):
        assert is_fuzzy_match("patrick mahomes", "Josh Allen") is False

    def test_full_name_correct_player(self):
        assert is_fuzzy_match("patrick mahomes", "Patrick Mahomes") is True


# ─── clean_query ──────────────────────────────────────────────────

class TestCleanQuery:
    def test_lowercases(self):
        assert clean_query("Josh Allen") == "josh allen"

    def test_strips_punctuation(self):
        assert clean_query("Josh Allen!") == "josh allen"

    def test_collapses_whitespace(self):
        assert clean_query("  Tom   Brady  ") == "tom brady"

    def test_empty_string(self):
        assert clean_query("") == ""

    def test_question_marks(self):
        assert clean_query("Who is Patrick Mahomes?") == "who is patrick mahomes"

    def test_special_chars(self):
        assert clean_query("CeeDee Lamb #88") == "ceedee lamb 88"


# ─── parse_iso_datetime ───────────────────────────────────────────

class TestParseIsoDatetime:
    def test_z_suffix(self):
        result = parse_iso_datetime("2025-01-12T18:00:00Z")
        assert result is not None
        assert result.year == 2025
        assert result.tzinfo is not None

    def test_millisecond_z(self):
        result = parse_iso_datetime("2025-01-12T18:00:00.000Z")
        assert result is not None
        assert result.hour == 18

    def test_none_input(self):
        assert parse_iso_datetime(None) is None

    def test_bad_string(self):
        assert parse_iso_datetime("not-a-date") is None

    def test_empty_string(self):
        assert parse_iso_datetime("") is None

    def test_returns_utc_aware(self):
        result = parse_iso_datetime("2025-09-07T17:00:00Z")
        assert result.tzinfo is not None


# ─── to_et ────────────────────────────────────────────────────────

class TestToEt:
    def test_converts_utc_to_et(self):
        dt = datetime.datetime(2025, 9, 7, 17, 0, 0, tzinfo=datetime.timezone.utc)
        result = to_et(dt)
        assert "ET" in result
        assert ":" in result

    def test_none_returns_tbd(self):
        assert to_et(None) == "TBD"

    def test_naive_datetime_treated_as_utc(self):
        dt = datetime.datetime(2025, 9, 7, 17, 0, 0)
        result = to_et(dt)
        assert "ET" in result
