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
  2. Fetches each team's official end-of-GW history and patches team_value /
     in_the_bank onto their gw_scores rows — BEFORE the re-score. `bank` is
     only FILLED IN when the deadline seed never landed; it is never
     overwritten, because bank and transfers are both known and locked at the
     deadline and the penalty is reconciled upfront.
  3. Re-scores every team with gw_finished=True via recalc_gw
     (applies chip penalties, final auto-subs, VC promotion) and
     recalc_fpl_raw=True so fpl_raw comes from the final player scores.
  4. Patches + re-scores any row recalc_gw created that step 2 couldn't reach.
  5. Clears is_live on the GW's team_gw_selections rows.

API calls per run: 2 (fixtures + live) + 1 per team (history).

Note: FPL occasionally adjusts points days later (dubious-goals panel).
Those still need a manual backfill_player_scores + recalc_gw --recalc-fpl-raw.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_live, fetch_team_history
from db      import (
    DEFAULT_SEASON,
    get_client,
    get_team_ids,
    has_unfinalized_scores,
    select_all,
    update_rows,
    upsert,
)

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

    # 2. End-of-GW snapshot, fetched BEFORE re-scoring so recalc_gw can see it.
    #
    #    IMPORTANT — `bank` is DEADLINE-LOCKED, and this only FILLS it when it
    #    is missing; it never overwrites a value the deadline seed already
    #    captured. Bank and transfers are both fully known the moment the
    #    deadline passes, so the penalty is reconciled upfront and never moves
    #    afterwards. Overwriting here would risk replacing the correct deadline
    #    value with a later one if FPL's history row drifts once a manager
    #    starts making transfers for the NEXT gameweek mid-GW.
    #
    #    team_value / in_the_bank are display-only, so those are always
    #    refreshed to the genuine end-of-GW figures.
    scored_this_gw = {r["team_id"] for r in
                       select_all("gw_scores", {"season": SEASON, "gw": gw}, select="team_id")}
    bank_known = {r["team_id"] for r in
                  select_all("gw_scores", {"season": SEASON, "gw": gw}, select="team_id,bank")
                  if r.get("bank") is not None}
    hist_by_team: dict[int, dict] = {}
    for tid in get_team_ids(SEASON):
        history = fetch_team_history(tid)
        hist_gw = next((g for g in (history or {}).get("current", []) if g.get("event") == gw), None)
        if hist_gw is not None:
            hist_by_team[tid] = hist_gw

    def _value_row(tid: int, hist_gw: dict) -> dict:
        row = {
            "season": SEASON, "team_id": tid, "gw": gw,
            "team_value":  hist_gw.get("value"),
            "in_the_bank": hist_gw.get("bank"),
        }
        # Backfill only. A team already seeded at the deadline keeps that value.
        if tid not in bank_known:
            row["bank"] = hist_gw.get("bank")
            log.info("  Filling missing bank for team %d GW%d from history (seed "
                     "never landed).", tid, gw)
        return row

    # update_rows is a real UPDATE, never an INSERT, so it can only patch rows
    # that already exist — hence the scored_this_gw filter (and the second
    # pass at step 4 for rows recalc_gw is about to create).
    value_rows = [_value_row(tid, h) for tid, h in hist_by_team.items() if tid in scored_this_gw]
    if value_rows:
        log.info("Patching bank/value onto %d gw_scores rows for GW%d...", len(value_rows), gw)
        update_rows("gw_scores", value_rows, key_cols=["season", "team_id", "gw"])

    # 3. Final team scoring — gw_finished=True applies chip penalties and
    #    writes rows with is_live=False, is_provisional=False. Reads the real
    #    bank patched in at step 2.
    from tasks.recalc_gw import run as run_recalc_gw
    rc = run_recalc_gw(gw, gw, recalc_fpl_raw=True)
    if rc != 0:
        log.error("recalc_gw failed during finalize (exit %d).", rc)
        return rc

    # 4. Catch teams whose gw_scores row recalc_gw just created — step 2 could
    #    not patch those, because they had no row to UPDATE yet. Uses the
    #    already-fetched history, so this costs no extra API calls, and is a
    #    no-op on the normal path where a live pass ran earlier in the GW.
    now_scored = {r["team_id"] for r in
                  select_all("gw_scores", {"season": SEASON, "gw": gw}, select="team_id")}
    late_rows = [_value_row(tid, h) for tid, h in hist_by_team.items()
                 if tid in now_scored and tid not in scored_this_gw]
    if late_rows:
        log.info("Patching %d newly-created gw_scores rows, then re-scoring...", len(late_rows))
        update_rows("gw_scores", late_rows, key_cols=["season", "team_id", "gw"])
        rc = run_recalc_gw(gw, gw, recalc_fpl_raw=True)
        if rc != 0:
            log.error("recalc_gw failed on the late-row pass (exit %d).", rc)
            return rc

    # 5. Selections are no longer live
    sb = get_client()
    sb.from_("team_gw_selections").update({"is_live": False}) \
      .eq("season", SEASON).eq("gw", gw).execute()

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
