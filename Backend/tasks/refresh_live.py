"""
tasks/refresh_live.py — Update player live stats during matches.
==================================================================
Self-gating: only runs if the live match window is open. Outside the window
this is a no-op so the cron can fire every 2 mins all weekend cheaply.

When live:
  - 1 API call to /event/{gw}/live/
  - 1 API call to /fixtures/?event={gw}  (updates fixture states in DB)
  - Filters updates to players whose club has a match in play or recently finished
  - Upserts affected rows into player_gw_scores
  - Sets is_provisional=True for players in finished_provisional (not yet finished) fixtures

API calls per run: 2 when live, 0 when idle.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_fixtures as fetch_fixtures_api, fetch_live
from db       import (
    DEFAULT_SEASON,
    get_fixtures,
    get_players_ref,
    is_live_window_open,
    upsert,
)
from scoring  import INACTIVE_PEN

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON


# ── Fixture state sync ────────────────────────────────────────────────────────

def sync_fixture_states(season: str, gw: int) -> list[dict]:
    """
    Fetch fresh fixture states from FPL API and upsert into the fixtures table.
    Returns the fresh fixture list (or falls back to DB if the API call fails).
    """
    fresh = fetch_fixtures_api(gw=gw)
    if not fresh:
        log.warning("Fixture state fetch failed — using DB fixtures.")
        return get_fixtures(season, gw)

    rows = [
        {
            "season":               season,
            "fixture_id":           f["id"],
            "gw":                   gw,
            "team_h":               f["team_h"],
            "team_a":               f["team_a"],
            "kickoff_time":         f.get("kickoff_time"),
            "started":              f.get("started") or False,
            "finished":             f.get("finished") or False,
            "finished_provisional": f.get("finished_provisional") or False,
            "team_h_score":         f.get("team_h_score"),
            "team_a_score":         f.get("team_a_score"),
        }
        for f in fresh
    ]
    upsert("fixtures", rows, on_conflict="season,fixture_id")
    log.info("Synced %d fixture states for GW%d", len(rows), gw)
    return rows


# ── Row builder ───────────────────────────────────────────────────────────────

def build_player_gw_rows(
    gw: int,
    season: str,
    live_data: dict,
    is_live: bool,
    relevant_player_ids:    set[int] | None = None,
    provisional_player_ids: set[int] | None = None,
) -> list[dict]:
    """
    Build rows for player_gw_scores. If relevant_player_ids is provided,
    only those players are included (saves DB writes during live polling).
    is_provisional=True is set for players in provisional_player_ids.
    """
    rows = []
    for el in live_data.get("elements", []):
        if relevant_player_ids is not None and el["id"] not in relevant_player_ids:
            continue
        stats    = el.get("stats", {})
        base_pts = stats.get("total_points", 0) or 0
        minutes  = stats.get("minutes", 0) or 0
        anti_pts = base_pts + INACTIVE_PEN if minutes == 0 else base_pts
        is_prov  = provisional_player_ids is not None and el["id"] in provisional_player_ids
        rows.append({
            "season":         season,
            "player_id":      el["id"],
            "gw":             gw,
            "base_pts":       base_pts,
            "minutes":        minutes,
            "anti_pts":       anti_pts,
            "is_live":        is_live,
            "is_provisional": is_prov,
        })
    return rows


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_active_player_ids(
    season: str, gw: int,
    fixtures: list[dict] | None = None,
    players:  list[dict] | None = None,
) -> set[int]:
    """
    Return player IDs whose club has a fixture this GW that is currently
    in play OR has finished. Excludes players whose fixture hasn't started yet
    (their stats are still all zeroes — no need to update).
    Accepts optional pre-fetched fixtures/players lists to avoid redundant DB queries.
    """
    if fixtures is None:
        fixtures = get_fixtures(season, gw)
    active_club_ids: set[int] = set()
    for f in fixtures:
        if f.get("started") or f.get("finished") or f.get("finished_provisional"):
            active_club_ids.add(f["team_h"])
            active_club_ids.add(f["team_a"])

    if not active_club_ids:
        return set()

    if players is None:
        players = get_players_ref(season)
    return {p["player_id"] for p in players if p.get("team_id") in active_club_ids}


def _provisional_player_ids(
    fixtures: list[dict], season: str,
    players: list[dict] | None = None,
) -> set[int]:
    """Player IDs whose fixture is finished_provisional but not yet officially finished."""
    prov_clubs: set[int] = set()
    for f in fixtures:
        if f.get("finished_provisional") and not f.get("finished"):
            prov_clubs.add(f["team_h"])
            prov_clubs.add(f["team_a"])
    if not prov_clubs:
        return set()
    if players is None:
        players = get_players_ref(season)
    return {p["player_id"] for p in players if p.get("team_id") in prov_clubs}


# ── Entry points ──────────────────────────────────────────────────────────────

def run(fixtures: list[dict] | None = None) -> int:
    """
    fixtures: pass an already-fresh fixture list (e.g. from the scheduler,
    which syncs fixture state once per tick before dispatching) to skip the
    redundant FPL API call + fixtures upsert this would otherwise do itself.
    Standalone callers (workflow_dispatch task=refresh_live) omit it and it
    syncs its own, unchanged.
    """
    log.info("Refresh live — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))

    is_open, current_gw = is_live_window_open(SEASON)
    if not is_open:
        log.info("Live window closed — exiting (no-op).")
        return 0

    log.info("Live window OPEN for GW%d", current_gw)

    # Sync fresh fixture states so scheduler can detect provisional window next cycle
    if fixtures is None:
        fixtures = sync_fixture_states(SEASON, current_gw)
    players      = get_players_ref(SEASON)
    relevant_ids = get_active_player_ids(SEASON, current_gw, fixtures=fixtures, players=players)
    if not relevant_ids:
        log.info("No active fixtures this GW yet — exiting.")
        return 0
    log.info("Active players (clubs with started/finished fixtures): %d", len(relevant_ids))

    prov_ids  = _provisional_player_ids(fixtures, SEASON, players=players)

    live_data = fetch_live(current_gw)
    if not live_data:
        log.error("Live data fetch failed.")
        return 1

    rows = build_player_gw_rows(
        gw                     = current_gw,
        season                 = SEASON,
        live_data              = live_data,
        is_live                = True,
        relevant_player_ids    = relevant_ids,
        provisional_player_ids = prov_ids or None,
    )

    log.info("Upserting %d player_gw_score rows...", len(rows))
    upsert("player_gw_scores", rows, on_conflict="season,player_id,gw")

    log.info("Refresh live complete.")
    return 0


def run_provisional_pass(gw: int, fixtures: list[dict] | None = None) -> int:
    """
    Fetch final player scores after all GW fixtures are finished_provisional.
    Called by the scheduler's provisional window path. No live-gate check.

    fixtures: see run() — pass a pre-synced list to skip the redundant fetch.
    """
    log.info("Provisional player score pass — GW%d %s", gw, datetime.now().strftime("%Y-%m-%d %H:%M"))

    if fixtures is None:
        fixtures = sync_fixture_states(SEASON, gw)
    players      = get_players_ref(SEASON)
    relevant_ids = get_active_player_ids(SEASON, gw, fixtures=fixtures, players=players)
    prov_ids     = _provisional_player_ids(fixtures, SEASON, players=players)

    live_data = fetch_live(gw)
    if not live_data:
        log.error("Live data fetch failed.")
        return 1

    rows = build_player_gw_rows(
        gw                     = gw,
        season                 = SEASON,
        live_data              = live_data,
        is_live                = True,
        relevant_player_ids    = relevant_ids or None,
        provisional_player_ids = prov_ids or None,
    )

    log.info("Upserting %d player_gw_score rows (provisional)...", len(rows))
    upsert("player_gw_scores", rows, on_conflict="season,player_id,gw")
    log.info("Provisional player scores updated.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())
