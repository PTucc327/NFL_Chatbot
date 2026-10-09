"""
Tests for the eval checks themselves: each must pass a good answer and fail
the bad answer it exists to catch (examples are real failures from eval runs).
"""
from unittest.mock import patch

import evals.cases as cases
from evals.checks import numbers_grounded, mentions_first_ranked, names_from_list


def test_grounding_accepts_scores_split_in_data():
    assert numbers_grounded("The Bucs won 24-16", "Tampa Bay **24** @ Dallas **16**", {})[0]


def test_grounding_rejects_invented_score():
    assert not numbers_grounded("The Bucs won 31-7", "Tampa Bay **24** @ Dallas **16**", {})[0]


def test_grounding_accepts_totals_of_listed_stats():
    data = "Wk 4: Rec: 4 / 31 yds\nWk 3: Rec: 7 / 65 yds\nWk 2: Rec: 6 / 61 yds\nWk 1: Rec: 5 / 30 yds"
    assert numbers_grounded("Gibbs has 22 receptions", data, {})[0]


def test_grounding_skipped_without_data():
    assert numbers_grounded("Playoff overtime periods are 15 minutes", "", {})[0]


def test_ranked_list_checks_read_every_line():
    data = "header\n1. **Josh Allen** (QB, BUF) — 115.5\n2. **Brock Purdy** (QB, SF) — 101.5"
    assert names_from_list(r"^\d+\. \*\*(.+?)\*\*", 2)("Allen and Purdy lead", data, {})[0]
    teams = "title\n- **1st:** Atlanta Falcons — 48.2\n- **2nd:** Jacksonville Jaguars — 74.2"
    assert mentions_first_ranked(r"^- \*\*(?:T-)?1st")("The Falcons lead", teams, {})[0]


_GUARD = ("ALREADY DRAFTED\n- Caleb Downs (DB, DAL)\n\n"
          "Known college prospects (curated; the only prospects to name):\n- Arch Manning (QB, Texas)")


def test_rookie_mentioned_as_rookie_is_fine():
    answer = "Watch **Arch Manning**. Don't confuse him with 2026 rookies like Caleb Downs."
    assert cases.no_drafted_prospects(answer, _GUARD, {})[0]


def test_nfl_players_listed_as_prospects_are_caught():
    # Real answer from an eval run: 2025 draftees listed as 2027 prospects.
    answer = ("* **Arch Manning** (QB, Texas)\n* **Abdul Carter** (EDGE, Penn State)\n"
              "* **Luther Burden III** (WR, Missouri)")
    with patch.object(cases, "_nfl_names", return_value={"abdul carter", "luther burden"}):
        ok, msg = cases.no_nfl_players_as_prospects(answer, _GUARD, {})
    assert not ok and "Abdul Carter" in msg and "Luther Burden III" in msg


def test_playoff_verdict_must_match_seed():
    data = "Philadelphia Eagles are the **#11 seed** in the NFC (outside the playoff spots)."
    check = cases.playoff_verdict_matches_seed("Philadelphia Eagles")
    assert check("No, they'd miss the playoffs.", data, {})[0]
    assert not check("Yes! They'd be in.", data, {})[0]
