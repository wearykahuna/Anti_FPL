"""
tasks/recalc_gw.py — Force re-score one or more finished GWs.
==============================================================
Reads existing picks + player scores from the DB and re-runs the scoring
engine. Useful after a scoring rule change or a failed live run.

Processes GWs in ascending order so cumulative totals chain correctly.
GWs after gw_to are NOT automatically updated — if anti_totals changed,
run recalc_gw again with a wider range to fix downstream cumulative totals.

API calls: 0
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import (
    DEFAULT_SEASON,
    get_gw_scores,
    get_player_scores,
    get_players_ref,
    get_selections,
    get_teams,
    upsert,
)
from scoring import score_one_gw_for_team
from tasks.recalc_scores import to_gw_scores_row, update_cumulative_standings, update_ranks_for_gw

log = logging.getLogger(__name__)

SEASON       = DEFAULT_SEASON
_POS_TO_TYPE = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}


def run(gw_from: int, gw_to: int) -> int:
    log.info("Recalc GW %d–%d for season %s", gw_from, gw_to, SEASON)

    players_ref  = get_players_ref(SEASON)
    player_type  = {p["player_id"]: _POS_TO_TYPE.get(p.get("position"), 3) for p in players_ref}

    teams     = get_teams(SEASON)
    team_meta = {t["team_id"]: t for t in teams}
    team_ids  = list(team_meta.keys())

    for gw in range(gw_from, gw_to + 1):
        log.info("── GW %d ──", gw)

        selections = get_selections(SEASON, gw=gw)
        if not selections:
            log.warning("No selections for GW %d — skipping", gw)
            continue
        sel_by_team = {s["team_id"]: s for s in selections}

        player_scores = get_player_scores(SEASON, gw=gw)
        mins_map = {p["player_id"]: p.get("minutes",  0) for p in player_scores}
        pts_map  = {p["player_id"]: p.get("base_pts", 0) for p in player_scores}

        existing_rows    = get_gw_scores(SEASON, gw=gw)
        existing_by_team = {r["team_id"]: r for r in existing_rows}

        prev_gw_rows = get_gw_scores(SEASON, gw=gw - 1) if gw > 1 else []
        prev_totals  = {r["team_id"]: r.get("anti_total", 0) for r in prev_gw_rows}

        scored_rows: list[dict] = []
        for tid in team_ids:
            sel = sel_by_team.get(tid)
            if not sel:
                continue

            chips_history = team_meta.get(tid, {}).get("chips_history") or []
            existing      = existing_by_team.get(tid, {})

            squad       = sel.get("squad") or []
            captain_id  = sel.get("captain_id")
            vice_id     = sel.get("vice_captain_id")
            active_chip = sel.get("active_chip") or ""
            cap_mult    = 3 if active_chip == "3xc" else 2

            picks_data = {
                "active_chip": active_chip,
                "picks": [
                    {
                        "element":         pid,
                        "position":        i + 1,
                        "is_captain":      pid == captain_id,
                        "is_vice_captain": pid == vice_id,
                        "multiplier":      cap_mult if pid == captain_id else 1,
                    }
                    for i, pid in enumerate(squad)
                ],
            }

            hist_gw_row = {
                "event":                gw,
                "points":               existing.get("fpl_raw_pts", 0) or 0,
                "event_transfers_cost": existing.get("fpl_xfer_cost", 0) or 0,
                "bank":                 existing.get("bank", 0) or 0,
                "rank":                 existing.get("fpl_gw_rank"),
                "total_points":         existing.get("fpl_total"),
            }

            result = score_one_gw_for_team(
                team_id              = tid,
                gw                   = gw,
                hist_gw              = hist_gw_row,
                picks_data           = picks_data,
                mins                 = mins_map,
                pts                  = pts_map,
                player_type          = player_type,
                chips                = chips_history,
                previous_anti_total  = prev_totals.get(tid, 0),
                gw_finished          = True,
            )
            if result:
                scored_rows.append(to_gw_scores_row(tid, gw, SEASON, result, is_live=False))

        if scored_rows:
            upsert("gw_scores", scored_rows, on_conflict="season,team_id,gw")
            update_ranks_for_gw(SEASON, gw)
            update_cumulative_standings(SEASON, gw)
            log.info("GW %d: upserted %d rows", gw, len(scored_rows))
        else:
            log.warning("GW %d: nothing to upsert", gw)

    if gw_to < 38:
        log.warning(
            "Cumulative totals for GWs > %d may be stale — "
            "re-run recalc_gw with gw_from=%d gw_to=<last_gw> if scores changed.",
            gw_to, gw_to + 1,
        )

    log.info("recalc_gw complete.")
    return 0


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
    p = argparse.ArgumentParser()
    p.add_argument("--gw-from", type=int, required=True)
    p.add_argument("--gw-to",   type=int)
    a = p.parse_args()
    sys.exit(run(a.gw_from, a.gw_to or a.gw_from))
