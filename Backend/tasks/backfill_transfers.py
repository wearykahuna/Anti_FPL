"""
tasks/backfill_transfers.py — One-off backfill of gw_scores.transfers_gw for past GWs.
========================================================================================
refresh_picks.py captures each team's raw transfer count (event_transfers)
going forward, one GW at a time, as part of its deadline seed. This task
populates history for GWs that were already finalized before that capture
existed — run it once after adding the `transfers_gw` column to gw_scores.

fetch_team_history() returns a team's ENTIRE season history in one call, so
this is one API call per team regardless of how many past GWs need filling.

Writes via update_rows() (a real UPDATE per row, not an upsert) so it can
never INSERT a placeholder gw_scores row for a team+GW that hasn't been
scored yet — that would trip NOT NULL constraints on unrelated columns.

API calls: 1 per eligible team.
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_team_history
from db      import DEFAULT_SEASON, get_team_ids, select_all, update_rows

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON


def run() -> int:
    log.info("Backfill transfers for season %s", SEASON)

    team_ids = get_team_ids(SEASON)
    log.info("Fetching history for %d eligible teams...", len(team_ids))

    # Only update rows recalc_scores/finalize_gw already created for this team+GW
    # (e.g. a team added mid-season has no rows for GWs before it joined).
    # update_rows() below is a real UPDATE, not an upsert, so it can never
    # INSERT a placeholder row even if this check races against a concurrent
    # write and goes stale.
    existing = select_all("gw_scores", {"season": SEASON}, select="team_id,gw")
    existing_keys = {(r["team_id"], r["gw"]) for r in existing}

    transfer_rows = []
    skipped = 0
    for tid in team_ids:
        history = fetch_team_history(tid)
        if not history:
            log.warning("No history returned for team %d — skipping.", tid)
            continue
        for hist_gw in history.get("current", []):
            gw = hist_gw.get("event")
            transfers = hist_gw.get("event_transfers")
            if gw is None or transfers is None:
                continue
            if (tid, gw) not in existing_keys:
                skipped += 1
                continue
            transfer_rows.append({
                "season": SEASON, "team_id": tid, "gw": gw,
                "transfers_gw": transfers,
            })

    log.info("Updating %d gw_scores.transfers_gw rows (%d skipped — no existing gw_scores row)...",
              len(transfer_rows), skipped)
    update_rows("gw_scores", transfer_rows, key_cols=["season", "team_id", "gw"])

    log.info("backfill_transfers complete.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())
