"""
tasks/refresh_picks.py — Fetch team picks + chips once per GW post-deadline.
==============================================================================
Self-gating: only runs for teams that are missing either their picks or
their GW seed data for the current GW. FPL locks picks at the deadline, so
we fetch each team's picks exactly once per GW — never again that GW.

Also refreshes each team's chips_history at the same time, since chips can
only be activated at the deadline. Single history fetch per team per GW
keeps the chips_history in teams.chips_history up to date for recalc_scores.

The history fetch also SEEDS the team's gw_scores row for the current GW
with bank / transfer-cost / transfer-count / FPL rank / FPL total. These are
fixed at the deadline, and recalc_scores reads them from the existing
gw_scores row — without the seed, hit and bank penalties would be silently
0 all GW.
(The seed retries on later runs until FPL publishes the history row.)

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


def build_gw_seed_row(team_id: int, gw: int, season: str, history: dict) -> dict | None:
    """
    Deadline-fixed FPL fields for this GW, from the team's history.
    recalc_scores reads bank / xfer cost from the existing gw_scores row,
    so this seed is what makes hit and bank penalties apply during live play.
    Returns None if FPL hasn't published the current-GW history row yet.
    """
    hist_gw = next((g for g in history.get("current", []) if g.get("event") == gw), None)
    if hist_gw is None:
        return None
    return {
        "season":        season,
        "team_id":       team_id,
        "gw":            gw,
        "bank":          hist_gw.get("bank", 0) or 0,
        "fpl_xfer_cost": hist_gw.get("event_transfers_cost", 0) or 0,
        "transfers_gw":  hist_gw.get("event_transfers", 0) or 0,
        "fpl_gw_rank":   hist_gw.get("rank"),
        "fpl_total":     hist_gw.get("total_points"),
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


def teams_already_seeded(season: str, gw: int) -> set[int]:
    """Team_ids whose gw_scores row for this GW already has bank data seeded."""
    sb   = get_client()
    rows = (sb.from_("gw_scores")
              .select("team_id,bank")
              .eq("season", season)
              .eq("gw", gw)
              .execute().data or [])
    return {r["team_id"] for r in rows if r.get("bank") is not None}


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

    all_team_ids = get_team_ids(SEASON)
    have_picks   = teams_already_have_picks(SEASON, current_gw)
    have_seed    = teams_already_seeded(SEASON, current_gw)

    need_picks = [tid for tid in all_team_ids if tid not in have_picks]
    need_seed  = [tid for tid in all_team_ids if tid not in have_seed]
    todo_ids   = [tid for tid in all_team_ids if tid in set(need_picks) | set(need_seed)]

    if not todo_ids:
        log.info("All %d teams already have picks + seed for GW%d — nothing to do.",
                 len(all_team_ids), current_gw)
        return 0

    log.info("GW%d: %d teams need picks, %d need seed data (%d total to process)...",
             current_gw, len(need_picks), len(need_seed), len(todo_ids))

    selection_rows: list[dict] = []
    chips_updates:  list[dict] = []
    seed_rows:      list[dict] = []

    for i, tid in enumerate(todo_ids, 1):
        if i % 20 == 0:
            log.info("  Progress: %d / %d", i, len(todo_ids))

        # Picks
        if tid in need_picks:
            picks_data = fetch_picks(tid, current_gw)
            if picks_data:
                sel = build_selection_row(tid, current_gw, SEASON, picks_data)
                if sel:
                    selection_rows.append(sel)
            else:
                log.warning("  No picks for team %d (deadline not yet passed?)", tid)
            time.sleep(0.3)

        # History → chips refresh + gw_scores seed (bank / xfer / rank / total)
        if tid in need_seed or tid in need_picks:
            history = fetch_team_history(tid)
            if history:
                chips_updates.append(build_chips_update(tid, SEASON, history))
                seed = build_gw_seed_row(tid, current_gw, SEASON, history)
                if seed:
                    seed_rows.append(seed)
                else:
                    log.warning("  No GW%d history row for team %d yet — seed retries next run.",
                                current_gw, tid)
            time.sleep(0.3)

    log.info("Upserting %d selection rows...", len(selection_rows))
    upsert("team_gw_selections", selection_rows, on_conflict="season,team_id,gw")

    log.info("Seeding %d gw_scores rows (bank / xfer cost / transfers)...", len(seed_rows))
    upsert("gw_scores", seed_rows, on_conflict="season,team_id,gw")

    log.info("Updating chips_history for %d teams...", len(chips_updates))
    sb = get_client()
    for row in chips_updates:
        sb.from_("teams").update({"chips_history": row["chips_history"]}) \
          .eq("team_id", row["team_id"]).eq("season", row["season"]).execute()

    log.info("Refresh picks complete.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())

