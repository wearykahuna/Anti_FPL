"""
tasks/refresh_live.py — Update player live stats during matches.
==================================================================
Self-gating: only runs if the live match window is open. Outside the window
this is a no-op so the cron can fire every 2 mins all weekend cheaply.

When live:
  - 1 API call to /event/{gw}/live/
  - Filters updates to players whose club has a match in play or recently finished
  - Upserts affected rows into player_gw_scores

API calls per run: 1 when live, 0 when idle.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_live
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


# ── Row builder ───────────────────────────────────────────────────────────────

def build_player_gw_rows(
    gw: int,
    season: str,
    live_data: dict,
    is_live: bool,
    relevant_player_ids: set[int] | None = None,
) -> list[dict]:
    """
    Build rows for player_gw_scores. If relevant_player_ids is provided,
    only those players are included (saves DB writes during live polling).
    """
    rows = []
    for el in live_data.get("elements", []):
        if relevant_player_ids is not None and el["id"] not in relevant_player_ids:
            continue
        stats    = el.get("stats", {})
        base_pts = stats.get("total_points", 0) or 0
        minutes  = stats.get("minutes", 0) or 0
        anti_pts = base_pts + INACTIVE_PEN if minutes == 0 else base_pts
        rows.append({
            "season":    season,
            "player_id": el["id"],
            "gw":        gw,
            "base_pts":  base_pts,
            "minutes":   minutes,
            "anti_pts":  anti_pts,
            "is_live":   is_live,
        })
    return rows


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_active_player_ids(season: str, gw: int) -> set[int]:
    """
    Return player IDs whose club has a fixture this GW that is currently
    in play OR has finished. Excludes players whose fixture hasn't started yet
    (their stats are still all zeroes — no need to update).
    """
    fixtures = get_fixtures(season, gw)
    active_club_ids: set[int] = set()
    for f in fixtures:
        if f.get("started") or f.get("finished") or f.get("finished_provisional"):
            active_club_ids.add(f["team_h"])
            active_club_ids.add(f["team_a"])

    if not active_club_ids:
        return set()

    players = get_players_ref(season)
    return {p["player_id"] for p in players if p.get("team_id") in active_club_ids}


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> int:
    log.info("Refresh live — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))

    is_open, current_gw = is_live_window_open(SEASON)
    if not is_open:
        log.info("Live window closed — exiting (no-op).")
        return 0

    log.info("Live window OPEN for GW%d", current_gw)

    # Identify players who actually need an update
    relevant_ids = get_active_player_ids(SEASON, current_gw)
    if not relevant_ids:
        log.info("No active fixtures this GW yet — exiting.")
        return 0
    log.info("Active players (clubs with started/finished fixtures): %d", len(relevant_ids))

    # One API call for the live GW
    live_data = fetch_live(current_gw)
    if not live_data:
        log.error("Live data fetch failed.")
        return 1

    rows = build_player_gw_rows(
        gw                  = current_gw,
        season              = SEASON,
        live_data           = live_data,
        is_live             = True,
        relevant_player_ids = relevant_ids,
    )

    log.info("Upserting %d player_gw_score rows...", len(rows))
    upsert("player_gw_scores", rows, on_conflict="season,player_id,gw")

    log.info("Refresh live complete.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())
