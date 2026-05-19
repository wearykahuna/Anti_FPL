"""
tasks/refresh_reference.py — Refresh reference data.
======================================================
Daily task. Updates the three reference tables that change rarely:
  - players    : FPL player metadata (name, position, club, price)
  - gameweeks  : GW deadlines, current/finished flags
  - fixtures   : match state (kicked off, finished, scores)

Self-gating: always safe to run, idempotent via upsert.
API calls per run: 2 (bootstrap + fixtures).
"""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_bootstrap, fetch_fixtures
from db       import upsert, DEFAULT_SEASON

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON


# ── Row builders ──────────────────────────────────────────────────────────────

def build_player_rows(bootstrap: dict, season: str) -> list[dict]:
    teams_by_id = {t["id"]: t for t in bootstrap.get("teams", [])}
    pos_map = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
    rows = []
    for el in bootstrap.get("elements", []):
        team = teams_by_id.get(el.get("team"), {})
        rows.append({
            "season":     season,
            "player_id":  el["id"],
            "web_name":   el.get("web_name", str(el["id"])),
            "first_name": el.get("first_name"),
            "last_name":  el.get("second_name"),
            "position":   pos_map.get(el.get("element_type"), "?"),
            "team_id":    el.get("team"),
            "team_short": team.get("short_name"),
            "team_name":  team.get("name"),
            "now_cost":   el.get("now_cost"),
        })
    return rows


def build_gameweek_rows(bootstrap: dict, season: str) -> list[dict]:
    rows = []
    now_iso = datetime.now(timezone.utc).isoformat()
    for ev in bootstrap.get("events", []):
        rows.append({
            "season":       season,
            "gw":           ev["id"],
            "deadline":     ev.get("deadline_time"),
            "is_current":   ev.get("is_current", False),
            "is_finished":  ev.get("finished", False),
            "finalized_at": now_iso if ev.get("finished") else None,
        })
    return rows


def build_fixture_rows(fixtures: list[dict], season: str) -> list[dict]:
    rows = []
    for f in fixtures:
        rows.append({
            "season":               season,
            "fixture_id":           f["id"],
            "gw":                   f.get("event") or 0,
            "team_h":               f["team_h"],
            "team_a":               f["team_a"],
            "kickoff_time":         f.get("kickoff_time"),
            "started":              f.get("started") or False,
            "finished":             f.get("finished") or False,
            "finished_provisional": f.get("finished_provisional") or False,
            "team_h_score":         f.get("team_h_score"),
            "team_a_score":         f.get("team_a_score"),
        })
    # Drop fixtures with no GW assigned (rare, defensive)
    return [r for r in rows if r["gw"] > 0]


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> int:
    """Returns exit code (0 success, 1 failure)."""
    log.info("=" * 60)
    log.info("Refresh reference data — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("Season: %s", SEASON)
    log.info("=" * 60)

    bootstrap = fetch_bootstrap()
    if not bootstrap:
        log.error("Bootstrap fetch failed.")
        return 1

    fixtures = fetch_fixtures()
    if fixtures is None:
        log.warning("Fixtures fetch failed — continuing with players + gameweeks only.")
        fixtures = []

    player_rows   = build_player_rows(bootstrap, SEASON)
    gameweek_rows = build_gameweek_rows(bootstrap, SEASON)
    fixture_rows  = build_fixture_rows(fixtures, SEASON)

    log.info("Upserting %d players, %d gameweeks, %d fixtures",
             len(player_rows), len(gameweek_rows), len(fixture_rows))

    upsert("players",   player_rows,   on_conflict="season,player_id")
    upsert("gameweeks", gameweek_rows, on_conflict="season,gw")
    upsert("fixtures",  fixture_rows,  on_conflict="season,fixture_id")

    log.info("Refresh reference complete.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())
