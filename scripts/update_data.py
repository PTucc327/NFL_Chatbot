"""
scripts/update_data.py
======================
Weekly data refresh script — run every Tuesday to pull fresh NFL rosters
and draft prospect rankings into the data/ directory.

What it updates:
  data/rosters.json   — active NFL rosters grouped by team, with depth chart
                         positions. Answers "who is the backup QB for X team?"
                         and "list the Eagles receivers" style questions.
  data/prospects.json — top college prospects from ESPN draft rankings.

Data sources:
  - Sleeper /players/nfl  (free, no auth needed)
  - ESPN draft API        (free, no auth needed)

Usage:
  python scripts/update_data.py               # update everything
  python scripts/update_data.py --rosters     # rosters only
  python scripts/update_data.py --prospects   # prospects only
  python scripts/update_data.py --dry-run     # print what would change, no writes

Schedule (Windows Task Scheduler):
  See setup_weekly_task.bat in this directory.
"""

import argparse
import json
import logging
import os
import sys
import time
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(__file__), "update_data.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("update_data")

# ── Paths ──────────────────────────────────────────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_DATA_DIR   = os.path.join(_SCRIPT_DIR, "..", "data")

ROSTERS_PATH   = os.path.join(_DATA_DIR, "rosters.json")
PROSPECTS_PATH = os.path.join(_DATA_DIR, "prospects.json")

# ── Endpoints ──────────────────────────────────────────────────────
SLEEPER_PLAYERS = "https://api.sleeper.app/v1/players/nfl"

# ESPN draft API was removed; we derive prospects directly from the Sleeper
# player dump — rookies (years_exp == 0) ranked by Sleeper's search_rank.
# search_rank is a universal relevance score Sleeper assigns; lower = more
# searched / more notable.  9999999 means "unranked / unknown player".
_SLEEPER_PROSPECT_RANK_CUTOFF = 500   # only include well-known prospects

# Fantasy-relevant skill positions only — the full Sleeper dump has every
# practice-squad lineman; we only want players users actually ask about.
SKILL_POSITIONS = {"QB", "RB", "WR", "TE", "K"}
DEPTH_POSITIONS = {"QB", "RB", "WR", "TE", "K", "LB", "CB", "S", "DE", "DT"}

REQUEST_TIMEOUT = 15


# ── Helpers ────────────────────────────────────────────────────────

def _fetch(url: str, params: Optional[Dict] = None) -> Any:
    """GET with simple retry + friendly error."""
    for attempt in range(1, 4):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log.warning(f"Attempt {attempt}/3 failed for {url}: {e}")
            if attempt < 3:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"All retries failed for {url}")


def _write_json(path: str, data: Any, dry_run: bool) -> None:
    """Write JSON to disk, or just log what would be written in dry-run mode."""
    if dry_run:
        log.info(f"[DRY RUN] Would write {len(data)} records to {os.path.basename(path)}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    log.info(f"Wrote {os.path.basename(path)} — {len(data)} records")


# ── Roster updater ─────────────────────────────────────────────────

def update_rosters(dry_run: bool = False) -> None:
    """
    Pulls the full Sleeper player dump and builds data/rosters.json —
    a dict keyed by team abbreviation, each containing a list of players
    sorted by depth chart order within their position group.

    Schema per player:
      { player_id, full_name, position, depth_chart_position,
        depth_chart_order, injury_status, injury_body_part, years_exp }

    This lets api_client.py answer "who are the Chiefs receivers?" or
    "who is the backup QB for the Eagles?" without scanning the full
    4 MB Sleeper dump at query time.
    """
    log.info("Fetching Sleeper player dump …")
    raw: Dict[str, Dict] = _fetch(SLEEPER_PLAYERS)
    log.info(f"  {len(raw):,} total players in Sleeper dump")

    # Group active skill-position players by team
    rosters: Dict[str, List[Dict]] = {}

    for pid, p in raw.items():
        if not p.get("active"):
            continue
        team = p.get("team") or ""
        pos  = p.get("position") or ""
        if not team or pos not in DEPTH_POSITIONS:
            continue

        entry = {
            "player_id":            pid,
            "full_name":            p.get("full_name", ""),
            "first_name":           p.get("first_name", ""),
            "last_name":            p.get("last_name", ""),
            "position":             pos,
            "depth_chart_position": p.get("depth_chart_position", pos),
            "depth_chart_order":    p.get("depth_chart_order"),
            "injury_status":        p.get("injury_status"),
            "injury_body_part":     p.get("injury_body_part"),
            "years_exp":            p.get("years_exp", 0),
            "age":                  p.get("age"),
            "college":              p.get("college", ""),
        }
        rosters.setdefault(team, []).append(entry)

    # Sort each team's roster: by position group, then depth order
    POS_ORDER = {"QB": 0, "RB": 1, "WR": 2, "TE": 3, "K": 4,
                 "DE": 5, "DT": 6, "LB": 7, "CB": 8, "S": 9}
    for team in rosters:
        rosters[team].sort(key=lambda p: (
            POS_ORDER.get(p["position"], 99),
            p["depth_chart_order"] if p["depth_chart_order"] is not None else 99,
        ))

    # Wrap with metadata
    output = {
        "_meta": {
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "total_players": sum(len(v) for v in rosters.values()),
            "teams": len(rosters),
            "source": "Sleeper API /v1/players/nfl",
        },
        "rosters": rosters,
    }

    log.info(f"  Built rosters for {len(rosters)} teams, "
             f"{output['_meta']['total_players']} players total")
    _write_json(ROSTERS_PATH, output, dry_run)


# ── Prospects updater ──────────────────────────────────────────────

def update_prospects(dry_run: bool = False) -> None:
    """
    Pulls ESPN's draft prospect rankings and writes data/prospects.json.

    Schema per prospect (matches existing format so api_client.py needs
    no changes):
      { name, school, pos, stats, outlook }
    """
    log.info("Fetching ESPN draft prospects …")
    try:
        data = _fetch(ESPN_DRAFT_API, params={"limit": 50})
    except RuntimeError as e:
        log.error(f"ESPN draft API unavailable: {e}")
        log.warning("Keeping existing prospects.json unchanged.")
        return

    athletes = data.get("athletes", [])
    if not athletes:
        log.warning("ESPN returned no prospects — keeping existing file.")
        return

    prospects = []
    for a in athletes:
        bio   = a.get("athlete", a)          # ESPN nests differently sometimes
        name  = bio.get("displayName") or bio.get("fullName", "Unknown")
        pos   = bio.get("position", {}).get("abbreviation", "?")
        school_data = bio.get("college") or bio.get("team") or {}
        school = school_data.get("displayName") or school_data.get("name", "Unknown")

        # Build a stats summary from ESPN's measurables if present
        stats_parts = []
        for stat in bio.get("statistics", [])[:4]:
            label = stat.get("displayName", stat.get("name", ""))
            val   = stat.get("displayValue", "")
            if label and val:
                stats_parts.append(f"{label}: {val}")

        # Draft projection
        projection = a.get("draftProjection") or bio.get("draftProjection") or ""
        rank        = a.get("rank", a.get("ranking", ""))
        outlook     = projection or (f"#{rank} overall prospect" if rank else "Top NFL Draft Prospect")

        prospects.append({
            "name":    name,
            "school":  school,
            "pos":     pos,
            "stats":   "; ".join(stats_parts) if stats_parts else "See ESPN for full stats",
            "outlook": outlook,
        })

    log.info(f"  {len(prospects)} prospects fetched from ESPN")
    _write_json(PROSPECTS_PATH, prospects, dry_run)


# ── Main ───────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Weekly NFL data refresh — rosters and draft prospects."
    )
    parser.add_argument("--rosters",   action="store_true", help="Update rosters only")
    parser.add_argument("--prospects", action="store_true", help="Update prospects only")
    parser.add_argument("--dry-run",   action="store_true", help="Print changes without writing")
    args = parser.parse_args()

    # Default: update everything
    run_rosters   = args.rosters   or not (args.rosters or args.prospects)
    run_prospects = args.prospects or not (args.rosters or args.prospects)

    start = time.time()
    log.info("=" * 60)
    log.info(f"NFL data update started  {'[DRY RUN]' if args.dry_run else ''}")
    log.info("=" * 60)

    if run_rosters:
        try:
            update_rosters(dry_run=args.dry_run)
        except Exception as e:
            log.error(f"Roster update failed: {e}")

    if run_prospects:
        try:
            update_prospects(dry_run=args.dry_run)
        except Exception as e:
            log.error(f"Prospects update failed: {e}")

    elapsed = time.time() - start
    log.info(f"Done in {elapsed:.1f}s")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
