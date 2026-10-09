"""
Eval cases: real fan questions with expectations.

`expect` checks how the question was understood (intents ⊇ expected; team,
player, opponent, week, stat match). `checks` run on the answer with the
fetched data, on top of the global checks in checks.py.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

from evals.checks import (Check, line_with, mentions_all, mentions_any, mentions_none,
                          mentions_team, mentions_teams_from, mentions_first_ranked,
                          names_from_list, teams_in)


@dataclass
class Case:
    id: str
    category: str
    question: str
    expect: dict = field(default_factory=dict)
    checks: tuple = ()
    context: Optional[dict] = None  # session memory, e.g. {"last_team": ...}


# ─── Data-derived checks ──────────────────────────────────────────

def names_bye_teams(answer, data, parsed):
    teams = teams_in(line_with(data, r"On bye"))
    if not teams:
        return ("no team" in answer.lower() or "none" in answer.lower(), "no bye teams in data")
    missing = [t for t in teams if not mentions_team(answer, t)]
    return (not missing, f"bye teams not named: {missing}")


def box_score_final(answer, data, parsed):
    m = re.search(r"Box Score[^:]*: (.+?) (\d+) @ (.+?) (\d+)\*\*", data)
    if not m:
        return False, "no box score header in data"
    ok = m.group(2) in answer and m.group(4) in answer
    return ok, "" if ok else f"final score {m.group(2)}-{m.group(4)} not stated"


def playoff_verdict_matches_seed(team: str) -> Check:
    def check(answer, data, parsed):
        m = re.search(rf"{team} are the \*\*#(\d+) seed", data)
        if not m:
            return False, f"no seed for {team} in data"
        seed, low = int(m.group(1)), answer.lower()
        if seed <= 7:
            ok = any(w in low for w in ("yes", "would make", "in the playoffs", "in playoff position", "would be in"))
        else:
            ok = any(w in low for w in ("no", "miss", "outside", "out of", "would not"))
        return ok, "" if ok else f"verdict doesn't match seed #{seed}"
    return check


def super_bowl_winner(answer, data, parsed):
    m = re.search(r"Super Bowl[^:]*: \*\*(.+?) \d+\*\*", data)
    if not m:
        return False, "no Super Bowl result in data"
    return mentions_team(answer, m.group(1)), f"winner {m.group(1)} not named"


def injury_status_stated(answer, data, parsed):
    m = re.search(r"\*\*Status:\*\* (\w[\w ]*)", data)
    if not m:
        return False, "no injury status in data"
    status = m.group(1).strip().lower()
    synonyms = {"healthy": ("healthy", "no injury", "full go", "good to go", "expected to play")}
    ok = any(s in answer.lower() for s in synonyms.get(status, (status,)))
    return ok, "" if ok else f"status {status!r} not stated"


_ALREADY_PRO = ("rookie", "drafted", "already", "now in the nfl", "confuse", "graduated")


def no_drafted_prospects(answer, data, parsed):
    """Drafted players may be mentioned as rookies, never presented as prospects."""
    drafted = re.findall(r"^- (.+?) \(", data.split("Known college prospects")[0], re.M)
    bad = [n for n in drafted for line in answer.splitlines()
           if n in line and not any(w in line.lower() for w in _ALREADY_PRO)]
    return (not bad, f"presented drafted players as prospects: {sorted(set(bad))}")


_SUFFIX = re.compile(r"\s+(?:jr\.?|sr\.?|ii|iii|iv|v)$", re.I)


def _nfl_names() -> set:
    """Every rostered NFL player's name (suffix-free), from the live player list."""
    from src import api_client
    api_client._ensure_player_cache()
    return {_SUFFIX.sub("", (p.get("full_name") or "").lower()).strip()
            for p in api_client._PLAYER_CACHE.values() if p.get("active") and p.get("team")}


def no_nfl_players_as_prospects(answer, data, parsed):
    """
    Stronger than the guard list: no bolded or listed 'prospect' may be a player
    already on an NFL roster (any draft year). The model's memory predates the
    last drafts, so it tends to list e.g. 2025 draftees as future prospects.
    """
    nfl = _nfl_names()
    candidates = re.findall(r"\*\*([A-Z][\w.'\- ]+?)\*\*", answer)
    candidates += re.findall(r"^\s*(?:[-*\u2022]|\d+\.)\s+([A-Z][\w.'\-]+(?: [A-Z][\w.'\-]+)+)", answer, re.M)
    lines = {c: next((l for l in answer.splitlines() if c in l), "") for c in candidates}
    bad = sorted({c for c in candidates
                  if _SUFFIX.sub("", c.lower()).strip() in nfl
                  and not any(w in lines[c].lower() for w in _ALREADY_PRO)})
    return (not bad, f"NFL players presented as prospects: {bad}")


def honest_about_coverage(answer, data, parsed):
    ok = re.search(r"big board|isn't available|not available|don't have a (?:full|live|current)", answer, re.I)
    return bool(ok), "" if ok else "doesn't say a full big board isn't available"


def backup_qb_named(answer, data, parsed):
    block = data.split("**QB**", 1)[-1]
    m = re.search(r"2\. (.+?) \(", block)
    if not m:
        return False, "no QB2 in data"
    return m.group(1).split()[-1] in answer, f"backup {m.group(1)} not named"


def cites_league_rank(answer, data, parsed):
    ok = re.search(r"\b\d{1,2}(st|nd|rd|th)\b", answer) is not None
    return ok, "" if ok else "no league rank (e.g. 28th) cited"


def verdict_word(answer, data, parsed):
    ok = re.search(r"\b(accept|decline|counter)\b", answer, re.I) is not None
    return ok, "" if ok else "no Accept/Decline/Counter verdict"


def picks_one(*names: str) -> Check:
    def check(answer, data, parsed):
        ok = re.search(r"\bstart\b", answer, re.I) and any(n.split()[-1] in answer for n in names)
        return bool(ok), "" if ok else "no clear start recommendation"
    return check


# ─── Cases ────────────────────────────────────────────────────────

CASES = [
    # Game day & schedules
    Case("tnf", "game day", "Who's playing Thursday Night Football this week?",
         {"intents": {"schedule"}}, (mentions_teams_from(r"Thursday Night Football", 2),)),
    Case("byes", "game day", "Who's on bye this week?",
         {"intents": {"schedule"}}, (names_bye_teams,)),
    Case("matchup", "schedule", "When do the Cowboys play the Eagles this year?",
         {"intents": {"schedule"}, "team": "Dallas Cowboys", "opponent": "Philadelphia Eagles"},
         (mentions_any("Week", "Wk"),)),
    Case("remaining", "schedule", "What's left on the Chiefs schedule?",
         {"intents": {"schedule"}, "team": "Kansas City Chiefs"},
         (mentions_teams_from(r"Wk \d+: (?:vs|@)", 1),)),

    # Box scores
    Case("box_matchup", "box score", "How did the Giants beat the Cardinals?",
         {"intents": {"box_score"}, "team": "New York Giants", "opponent": "Arizona Cardinals"},
         (box_score_final,)),
    Case("box_week", "box score", "Give me the box score from the Chiefs week 3 game",
         {"intents": {"box_score"}, "team": "Kansas City Chiefs", "week": 3},
         (box_score_final,)),
    Case("box_bye", "box score", "Chiefs week 5 box score",
         {"intents": {"box_score"}, "team": "Kansas City Chiefs", "week": 5},
         (mentions_any("bye"),)),

    # Standings & playoffs
    Case("playoffs_afc", "standings", "What's the AFC playoff picture looking like right now?",
         {"intents": {"playoffs"}}, (mentions_first_ranked(r"^1\. "), mentions_any("wild card"))),
    Case("division", "standings", "NFC East standings",
         {"intents": {"standings"}},
         (mentions_all("Giants", "Eagles", "Cowboys", "Commanders"),)),
    Case("would_make_it", "standings", "Would the Eagles make the playoffs if the season ended today?",
         {"intents": {"playoffs"}, "team": "Philadelphia Eagles"},
         (playoff_verdict_matches_seed("Philadelphia Eagles"),)),

    # Team rankings
    Case("team_defense", "team stats", "How's the Bills defense looking this year?",
         {"intents": {"team_stats"}, "team": "Buffalo Bills"},
         (mentions_any("allowed", "allowing"), cites_league_rank)),
    Case("best_run_d", "team stats", "Who has the best run defense in the league?",
         {"intents": {"team_stats"}}, (mentions_first_ranked(r"^- \*\*(?:T-)?1st"),)),

    # Players & injuries
    Case("injury", "players", "Is Patrick Mahomes playing this week?",
         {"intents": {"injury"}, "player": "Patrick Mahomes"}, (injury_status_stated,)),
    Case("drafted_prospect", "players", "Tell me about Travis Hunter",
         {"intents": {"player"}, "player": "Travis Hunter"},
         (mentions_any("Jaguars", "Jacksonville", "JAX"),)),
    Case("this_season", "players", "Show me Saquon's stats this season",
         {"player": "Saquon Barkley"}, (mentions_none("2024 season", "2025 season"),)),
    Case("backup_qb", "players", "Who's the Ravens backup QB?",
         {"intents": {"roster"}, "team": "Baltimore Ravens"}, (backup_qb_named,)),
    Case("typo", "players", "jalen hurts injry update",
         {"intents": {"injury"}, "player": "Jalen Hurts"}, (injury_status_stated,)),

    # Fantasy
    Case("start_sit", "fantasy", "Should I start Bijan Robinson or Jahmyr Gibbs in my flex?",
         {"player": "Bijan Robinson", "player_b": "Jahmyr Gibbs"},
         (picks_one("Bijan Robinson", "Jahmyr Gibbs"),)),
    Case("trade", "fantasy", "Should I trade Travis Kelce for CeeDee Lamb?",
         {"intents": {"trade"}, "player": "Travis Kelce", "player_b": "CeeDee Lamb"},
         (verdict_word,)),
    Case("waivers", "fantasy", "Who are the best WR waiver wire pickups right now?",
         {"intents": {"waiver"}}, (names_from_list(r"\*\*\d+\. (.+?)\*\*", 2),)),
    Case("qb_leaders", "fantasy", "Top 5 QBs in fantasy right now",
         {"intents": {"leaders"}, "stat": "pts_ppr"},
         (names_from_list(r"^\d+\. \*\*(.+?)\*\*", 3),)),
    Case("rookies", "fantasy", "Who are the best rookies this season?",
         {"intents": {"leaders"}}, (names_from_list(r"^\d+\. \*\*(.+?)\*\*", 3),)),

    # History & general knowledge
    Case("super_bowl", "history", "Who won the Super Bowl last season?",
         {"intents": {"postseason"}}, (super_bowl_winner,)),
    Case("rules", "history", "How does overtime work in the NFL playoffs?",
         {}, (mentions_any("possession", "possess"),)),
    Case("draft", "history", "Who are the top prospects for the next NFL draft?",
         {"intents": {"draft"}},
         (no_drafted_prospects, no_nfl_players_as_prospects, honest_about_coverage)),

    # Robustness
    Case("off_topic", "robustness", "What's the best pizza place in New York City?",
         {}, (mentions_any("NFL", "football", "pigskin", "gridiron", "fantasy"),
              mentions_none("Joe's Pizza", "Lombardi's", "Di Fara", "Prince Street"))),
    Case("follow_up", "robustness", "How about their running backs?",
         {"intents": {"roster"}, "team": "Baltimore Ravens"},
         (mentions_any("Henry", "RB", "running back"),),
         context={"last_team": "Baltimore Ravens"}),
]
