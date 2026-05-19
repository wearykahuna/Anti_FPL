"""
tasks/refresh_picks.py — Fetch team picks + chips once per GW post-deadline.
==============================================================================
Self-gating: only runs if the current GW's picks have not yet been stored.
FPL locks picks at the deadline, so we fetch each team's picks exactly once
per GW — never again that GW.

Also refreshes each team's chips_history at the same time, since chips can
only be activated at the deadline. Single history fetch per team per GW
keeps the chips_history in teams.chips_history up to date for recalc_scores.

For 200 teams → ~400 API calls (picks + history), but only on first run
of each GW. Subsequent runs in the same GW exit immediately.
"""

import logging
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_picks, fetch_team_history
from db       import (
    DEFAULT_SEASON,
    get_client,
    get_current_gw,
    get_team_ids,
    upsert,
)

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON


# ── Row builders ──────────────────────────────────────────────────────────────

def build_selection_row(team_id: int, gw: int, season: str, picks_data: dict) -> dict | None:
    picks   = picks_data.get("picks", [])
    captain = next((p["element"] for p in picks if p.get("is_captain")),      None)
    vice    = next((p["element"] for p in picks if p.get("is_vice_captain")), None)
    squad   = [p["element"] for p in sorted(picks, key=lambda p: p["position"])]
    if not squad or captain is None or vice is None:
        return None
    return {
        "season":          season,
        "team_id":         team_id,
        "gw":              gw,
        "squad":           squad,
        "captain_id":      captain,
        "vice_captain_id": vice,
        "active_chip":     (picks_data.get("active_chip") or "").lower(),
        "is_live":         True,    # set to false by recalc when GW finishes
    }


def build_chips_update(team_id: int, season: str, history: dict) -> dict:
    """Partial row update for the teams table — only touches chips_history."""
    return {
        "team_id":       team_id,
        "season":        season,
        "chips_history": history.get("chips", []),
    }


# ── Self-gate helpers ─────────────────────────────────────────────────────────

def teams_already_have_picks(season: str, gw: int) -> set[int]:
    """Return set of team_ids that already have a selection row for this GW."""
    sb   = get_client()
    rows = (sb.from_("team_gw_selections")
              .select("team_id")
              .eq("season", season)
              .eq("gw", gw)
              .execute().data or [])
    return {r["team_id"] for r in rows}


# ── Entry point ───────────────────────────────────────────────────────────────

def run() -> int:
    log.info("=" * 60)
    log.info("Refresh picks — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("Season: %s", SEASON)
    log.info("=" * 60)

    current_gw = get_current_gw(SEASON)
    if current_gw is None:
        log.info("No current GW — nothing to do.")
        return 0
    log.info("Current GW: %d", current_gw)

    all_team_ids   = get_team_ids(SEASON)
    already_stored = teams_already_have_picks(SEASON, current_gw)
    todo_ids       = [tid for tid in all_team_ids if tid not in already_stored]

    if not todo_ids:
        log.info("All %d teams already have picks for GW%d — nothing to do.",
                 len(all_team_ids), current_gw)
        return 0

    log.info("Fetching picks + chips for %d teams (%d already stored)...",
             len(todo_ids), len(already_stored))

    selection_rows: list[dict] = []
    chips_updates:  list[dict] = []

    for i, tid in enumerate(todo_ids, 1):
        if i % 20 == 0:
            log.info("  Progress: %d / %d", i, len(todo_ids))

        # Picks
        picks_data = fetch_picks(tid, current_gw)
        if not picks_data:
            log.warning("  No picks for team %d (deadline not yet passed?)", tid)
            continue
        sel = build_selection_row(tid, current_gw, SEASON, picks_data)
        if sel:
            selection_rows.append(sel)
        time.sleep(0.3)

        # Chips history — refresh once per GW per team
        history = fetch_team_history(tid)
        if history:
            chips_updates.append(build_chips_update(tid, SEASON, history))
        time.sleep(0.3)

    log.info("Upserting %d selection rows...", len(selection_rows))
    upsert("team_gw_selections", selection_rows, on_conflict="season,team_id,gw")

    log.info("Updating chips_history for %d teams...", len(chips_updates))
    upsert("teams", chips_updates, on_conflict="team_id,season")

    log.info("Refresh picks complete.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())

