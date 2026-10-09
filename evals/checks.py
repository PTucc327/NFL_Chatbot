"""
Answer checks for the eval suite.

Live data changes every week, so checks compare an answer with the data the
pipeline fetched for that same question instead of hard-coded facts. Each
check returns (ok, message).
"""
import json
import re
from pathlib import Path
from typing import Callable, Iterable

_TEAMS = json.loads((Path(__file__).resolve().parents[1] / "data" / "teams.json").read_text(encoding="utf-8"))
TEAM_NAMES = [t["displayName"] for t in _TEAMS]
NICKNAME = {t["displayName"]: t["displayName"].split()[-1] for t in _TEAMS}  # "Chiefs"

Check = Callable[[str, str, dict], tuple]  # (answer, data, parsed) -> (ok, msg)


# ─── Helpers over the fetched data ────────────────────────────────

def teams_in(text: str) -> list:
    """Teams named in text (full name or nickname), in order of appearance."""
    hits = []
    for name in TEAM_NAMES:
        for token in (name, NICKNAME[name]):
            i = text.find(token)
            if i >= 0:
                hits.append((i, name))
                break
    return [name for _, name in sorted(hits)]


def mentions_team(answer: str, team: str) -> bool:
    return team in answer or NICKNAME.get(team, team) in answer


def line_with(data: str, pattern: str) -> str:
    m = re.search(rf"^.*{pattern}.*$", data, re.M | re.I)
    return m.group(0) if m else ""


# ─── Global checks (every case) ───────────────────────────────────

_ERROR_TEXT = ("__API_ERROR__", "__QUOTA_ERROR__", "__CONFIG_ERROR__",
               "trouble reaching", "AI assistant is busy", "response was cut off")
_NON_LATIN = re.compile("[Ѐ-ӿ؀-ۿऀ-ॿ぀-ヿ一-鿿]")
# Numbers worth grounding: 2+ digits, decimals, records/scores like 24-16.
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?(?:-\d+)?)(?![\w.])")


def no_errors(answer, data, parsed):
    bad = [t for t in _ERROR_TEXT if t in answer]
    return (not bad, f"error text in answer: {bad}" if bad else "")


def no_foreign_script(answer, data, parsed):
    m = _NON_LATIN.search(answer)
    return (m is None, f"non-Latin text: {m.group(0)!r}" if m else "")


def reasonable_length(answer, data, parsed, max_words: int = 450):
    n = len(answer.split())
    return (20 <= n <= max_words, f"{n} words" if not 20 <= n <= max_words else "")


def numbers_grounded(answer, data, parsed, threshold: float = 0.85):
    """
    Share of numbers in the answer that also appear in the fetched data or the
    question. Catches invented scores and stats. Small numbers (<10) are
    skipped — counts like '3 TDs' are often restated as words or derived.
    """
    if not data.strip():
        return True, ""  # general-knowledge answer: nothing to ground against
    source = data + " " + parsed.get("raw_query", "")
    # Totals of same-labelled stats ("Rec: 4", "Rec: 7"... -> 22) are fair.
    by_label = {}
    for label, num in re.findall(r"([A-Za-z]+): (\d+)", data):
        by_label.setdefault(label, []).append(int(num))
    source += " " + " ".join(str(sum(v)) for v in by_label.values() if len(v) > 1)
    # Normalize "1,268" -> "1268" and "247.25" stays; compare both forms.
    norm = lambda s: s.replace(",", "")
    source_n = norm(source)
    nums = [n for n in _NUMBER.findall(norm(answer))
            if "-" in n or "." in n or len(n) >= 2]
    nums = [n for n in nums if not (len(n) == 4 and n.startswith(("19", "20")))]  # years
    if not nums:
        return True, ""
    def found(n: str) -> bool:
        if n in source_n or ("." in n and n.rstrip("0").rstrip(".") in source_n):
            return True
        # "24-16" written as a score: data often has each side separately.
        return "-" in n and all(part in source_n for part in n.split("-"))
    missing = [n for n in nums if not found(n)]
    share = 1 - len(missing) / len(nums)
    return (share >= threshold, f"{share:.0%} grounded; not in data: {missing[:6]}")


GLOBAL_CHECKS = (no_errors, no_foreign_script, reasonable_length, numbers_grounded)


# ─── Building blocks for case-specific checks ─────────────────────

def mentions_all(*words: str) -> Check:
    def check(answer, data, parsed):
        missing = [w for w in words if w.lower() not in answer.lower()]
        return (not missing, f"missing: {missing}" if missing else "")
    return check


def mentions_any(*words: str) -> Check:
    def check(answer, data, parsed):
        ok = any(w.lower() in answer.lower() for w in words)
        return (ok, "" if ok else f"none of {list(words)}")
    return check


def mentions_none(*words: str) -> Check:
    def check(answer, data, parsed):
        bad = [w for w in words if w.lower() in answer.lower()]
        return (not bad, f"should not mention: {bad}" if bad else "")
    return check


def mentions_teams_from(pattern: str, minimum: int = 1) -> Check:
    """Answer names at least `minimum` teams from the data line matching pattern."""
    def check(answer, data, parsed):
        teams = teams_in(line_with(data, pattern))
        if not teams:
            return False, f"no data line matching {pattern!r}"
        named = [t for t in teams if mentions_team(answer, t)]
        return (len(named) >= minimum, f"named {len(named)}/{minimum} of {teams}")
    return check


def mentions_first_ranked(pattern: str = r"^(?:1\.|- \*\*1st)") -> Check:
    """Answer names the #1 entry of a ranked list in the data."""
    def check(answer, data, parsed):
        line = line_with(data, pattern)
        if not line:
            return False, "no ranked list in data"
        teams = teams_in(line)
        name = teams[0] if teams else re.sub(r"[*\d.\-:]", "", line).split("(")[0].strip()
        ok = mentions_team(answer, name) if teams else name.split()[-1] in answer
        return ok, "" if ok else f"top entry {name!r} not mentioned"
    return check


def names_from_list(pattern: str, minimum: int = 2) -> Check:
    """Answer names at least `minimum` people from bold **Name** entries in the data."""
    def check(answer, data, parsed):
        names = re.findall(pattern, data, re.M)
        named = [n for n in names if n.split()[-1] in answer]
        return (len(named) >= minimum, f"named {len(named)}/{minimum} of {names[:5]}")
    return check


def all_of(*checks: Check) -> Iterable[Check]:
    return checks
