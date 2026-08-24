"""
tasks/finalize_gw.py — Final scoring pass once a GW is officially confirmed.
==============================================================================
The live and provisional passes always score with gw_finished=False, which
(by design) never applies unused-chip penalties and leaves rows flagged
is_live / is_provisional. Nothing else runs after FPL confirms the GW, so
without this task those flags stay set forever and the GW19/GW38 chip
penalties are never applied automatically.

This task runs exactly once per GW, when:
  - every fixture in the GW is officially finished (not just provisional), and
  - gw_scores rows for the GW are still flagged is_live or is_provisional.

It then:
  1. Pulls final player scores from /event/{gw}/live/ (all players,
     is_live=False, is_provisional=False) — catches late bonus changes.
  2. Re-scores every team with gw_finished=True via recalc_gw
     (applies chip penalties, final auto-subs, VC promotion) and
     recalc_fpl_raw=True so fpl_raw comes from the final player scores.
  3. Clears is_live on the GW's team_gw_selections rows.

API calls per run: 2 (fixtures + live).

Note: FPL occasionally adjusts points days later (dubious-goals panel).
Those still need a manual backfill_player_scores + recalc_gw --recalc-fpl-raw.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_live, fetch_team_history
from db      import DEFAULT_SEASON, get_client, get_team_ids, has_unfinalized_scores, upsert

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON


def run(gw: int) -> int:
    log.info("Finalize GW%d — %s", gw, datetime.now().strftime("%Y-%m-%d %H:%M"))

    from tasks.refresh_live import build_player_gw_rows, sync_fixture_states

    # Confirm every fixture is officially finished before finalizing
    fixtures = sync_fixture_states(SEASON, gw)
    if not fixtures or not all(f.get("finished") for f in fixtures):
        log.info("GW%d fixtures not all officially finished — nothing to finalize.", gw)
        return 0

    if not has_unfinalized_scores(SEASON, gw):
        log.info("GW%d already finalized — nothing to do.", gw)
        return 0

    # 1. Final player scores (all players, flags cleared)
    live_data = fetch_live(gw)
    if not live_data:
        log.error("Final live-data fetch failed for GW%d.", gw)
        return 1
    rows = build_player_gw_rows(gw, SEASON, live_data, is_live=False)
    log.info("Upserting %d final player_gw_score rows...", len(rows))
    upsert("player_gw_scores", rows, on_conflict="season,player_id,gw")

    # 2. Final team scoring — gw_finished=True applies chip penalties and
    #    writes rows with is_live=False, is_provisional=False
    from tasks.recalc_gw import run as run_recalc_gw
    rc = run_recalc_gw(gw, gw, recalc_fpl_raw=True)
    if rc != 0:
        log.error("recalc_gw failed during finalize (exit %d).", rc)
        return rc

    # 3. Selections are no longer live
    sb = get_client()
    sb.from_("team_gw_selections").update({"is_live": False}) \
      .eq("season", SEASON).eq("gw", gw).execute()

    # 4. Capture each team's official squad value for this GW — an end-of-GW
    #    snapshot only (not part of the live/intra-GW loop). fetch_team_history
    #    already returns the whole season's history in one call, so this just
    #    reads one more field out of a payload other tasks already fetch.
    value_rows = []
    for tid in get_team_ids(SEASON):
        history = fetch_team_history(tid)
        hist_gw = next((g for g in (history or {}).get("current", []) if g.get("event") == gw), None)
        if hist_gw is None:
            continue
        value_rows.append({"season": SEASON, "team_id": tid, "gw": gw, "team_value": hist_gw.get("value")})
    if value_rows:
        log.info("Upserting %d team value rows for GW%d...", len(value_rows), gw)
        upsert("gw_scores", value_rows, on_conflict="season,team_id,gw")

    log.info("GW%d finalized.", gw)
    return 0


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Final scoring pass for an officially finished GW.")
    p.add_argument("--gw", type=int, required=True)
    a = p.parse_args()
    sys.exit(run(a.gw))
