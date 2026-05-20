"""
tasks/backfill_player_scores.py — Refresh player GW scores from FPL API for any past GW.
==========================================================================================
Unlike refresh_live.py, this has no live-window gate and no active-club filter.
Use it to correct stale player_gw_scores data after a GW has fully finished —
particularly when minutes or points were captured mid-game and never updated.

After running this, re-run recalc_gw to propagate corrected scores:
  python backend/tasks/recalc_gw.py --gw-from <GW> --gw-to <GW> --recalc-fpl-raw

API calls: 1 per GW.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_live
from db import DEFAULT_SEASON, upsert
from tasks.refresh_live import build_player_gw_rows

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON


def run(gw_from: int, gw_to: int) -> int:
    log.info("Backfill player scores GW %d–%d for season %s", gw_from, gw_to, SEASON)

    for gw in range(gw_from, gw_to + 1):
        log.info("── GW %d ──", gw)

        live_data = fetch_live(gw)
        if not live_data:
            log.error("Failed to fetch live data for GW %d — skipping.", gw)
            continue

        player_count = len(live_data.get("elements", []))
        log.info("Fetched %d player entries from FPL API", player_count)

        rows = build_player_gw_rows(
            gw=gw,
            season=SEASON,
            live_data=live_data,
            is_live=False,
            relevant_player_ids=None,  # all players
        )

        log.info("Upserting %d player_gw_scores rows for GW %d...", len(rows), gw)
        upsert("player_gw_scores", rows, on_conflict="season,player_id,gw")
        log.info("GW %d complete.", gw)

    log.info("backfill_player_scores complete.")
    return 0


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Re-fetch player GW scores from FPL API for past GWs.")
    p.add_argument("--gw", type=int, required=True, help="GW to backfill (or start of range)")
    p.add_argument("--gw-to", type=int, help="End of GW range (inclusive). Defaults to --gw.")
    a = p.parse_args()
    sys.exit(run(a.gw, a.gw_to or a.gw))
