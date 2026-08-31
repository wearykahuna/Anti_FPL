"""
tasks/refresh_picks.py — Fetch team picks + chips once per GW post-deadline.
==============================================================================
Self-gating: only runs for teams that are missing either their picks or
their GW seed data for the current GW. FPL locks picks at the deadline, so
we fetch each team's picks exactly once per GW — never again that GW.

Also refreshes each team's chips_history at the same time, since chips can
only be activated at the deadline. Single history fetch per team per GW
keeps the chips_history in teams.chips_history up to date for recalc_scores.

This also SEEDS the team's gw_scores row for the current GW with bank /
transfer-cost / transfer-count / FPL rank / FPL total. These are fixed at the
deadline, and recalc_scores reads them back off the existing gw_scores row —
without the seed, hit and bank penalties would be silently 0 all GW.

The seed comes from the PICKS payload's `entry_history` block first, falling
back to the full history only if that's unavailable. The picks call is one we
already make, so the value driving the bank penalty no longer depends on a
second call that can fail independently.

The seed never writes a fabricated bank: if the real value isn't available it
writes nothing and retries next run. Writing 0 would make teams_already_seeded()
treat the team as done and the true bank would never be fetched again that GW.

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
    select_all,
    upsert,
)

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON

# Once a GW is fully captured (every team has picks AND a non-null bank), the
# four gate SELECTs below can only ever return "nothing to do" — but they ran
# on every scheduler tick regardless. Memoise that completion for the life of
# the worker process, with a TTL so a team registered mid-GW is still picked
# up. Worst case on a stale memo: recalc_scores writes bank=None for the new
# team and the next expiry re-seeds it.
_COMPLETE_TTL_S = 15 * 60
_seeded_complete: tuple[str, int, float] | None = None


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


def _hist_row_for_gw(history: dict | None, gw: int) -> dict | None:
    """The `current[]` entry for one GW out of a full team-history payload."""
    return next((g for g in (history or {}).get("current", []) if g.get("event") == gw), None)


def build_gw_seed_row(team_id: int, gw: int, season: str, hist_gw: dict | None) -> dict | None:
    """
    Deadline-fixed FPL fields for this GW.

    `hist_gw` is either the picks payload's `entry_history` block or the
    matching row from a full history `current[]` — they carry identical field
    names, so one builder serves both sources.

    recalc_scores reads bank / xfer cost back off the gw_scores row, so this
    seed is what makes hit and bank penalties apply during live play.

    Returns None when there is nothing trustworthy to seed. Critically that
    includes a missing `bank`: seeding a fabricated 0 would make
    teams_already_seeded() consider the team done, and the real bank would
    never be fetched again for this GW — silently disabling the penalty.
    """
    if not hist_gw:
        return None
    bank = hist_gw.get("bank")
    if bank is None:
        return None
    return {
        "season":        season,
        "team_id":       team_id,
        "gw":            gw,
        "bank":          bank,
        # team_value/in_the_bank from main (6bc12e0): seed these at the
        # deadline too, not just at finalize — populates the frontend's
        # Squad Value detail item immediately instead of leaving it "—"
        # until the GW finishes. in_the_bank mirrors `bank` (already
        # validated non-None above) so the two can never diverge.
        "team_value":    hist_gw.get("value"),
        "in_the_bank":   bank,
        "fpl_xfer_cost": hist_gw.get("event_transfers_cost", 0) or 0,
        "transfers_gw":  hist_gw.get("event_transfers", 0) or 0,
        "fpl_gw_rank":   hist_gw.get("rank"),
        "fpl_total":     hist_gw.get("total_points"),
    }


# ── Self-gate helpers ─────────────────────────────────────────────────────────

def teams_already_have_picks(season: str, gw: int) -> set[int]:
    """Return set of team_ids that already have a selection row for this GW."""
    rows = select_all("team_gw_selections", {"season": season, "gw": gw}, select="team_id")
    return {r["team_id"] for r in rows}


def teams_already_seeded(season: str, gw: int) -> set[int]:
    """
    Team_ids whose gw_scores row for this GW already has bank data seeded.

    `bank is not None` is only a truthful "seeded" test because nothing writes
    a fabricated 0 any more (see build_gw_seed_row, recalc_scores, recalc_gw).
    Both queries paginate — a truncated read here would re-fetch teams that are
    already seeded, but a truncated read in the other direction would leave a
    team permanently unseeded.
    """
    rows = select_all("gw_scores", {"season": season, "gw": gw}, select="team_id,bank")
    return {r["team_id"] for r in rows if r.get("bank") is not None}


# ── Entry point ───────────────────────────────────────────────────────────────

def run(current_gw: int | None = None, team_ids: list[int] | None = None) -> int:
    """
    current_gw / team_ids: pass already-fetched values (the scheduler resolves
    both before calling) to skip redundant reads on every tick.
    """
    global _seeded_complete

    log.info("=" * 60)
    log.info("Refresh picks — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("Season: %s", SEASON)
    log.info("=" * 60)

    if current_gw is None:
        current_gw = get_current_gw(SEASON)
    if current_gw is None:
        log.info("No current GW — nothing to do.")
        return 0
    log.info("Current GW: %d", current_gw)

    if _seeded_complete is not None:
        memo_season, memo_gw, memo_at = _seeded_complete
        if (memo_season, memo_gw) == (SEASON, current_gw)                 and time.monotonic() - memo_at < _COMPLETE_TTL_S:
            log.info("GW%d already fully captured (memo, %.0fs old) — skipping gate queries.",
                     current_gw, time.monotonic() - memo_at)
            return 0

    all_team_ids = team_ids if team_ids is not None else get_team_ids(SEASON)
    have_picks   = teams_already_have_picks(SEASON, current_gw)
    have_seed    = teams_already_seeded(SEASON, current_gw)

    need_picks = [tid for tid in all_team_ids if tid not in have_picks]
    need_seed  = [tid for tid in all_team_ids if tid not in have_seed]
    todo_ids   = [tid for tid in all_team_ids if tid in set(need_picks) | set(need_seed)]

    if not todo_ids:
        log.info("All %d teams already have picks + seed for GW%d — nothing to do.",
                 len(all_team_ids), current_gw)
        _seeded_complete = (SEASON, current_gw, time.monotonic())
        return 0

    # Anything left to fetch invalidates a previously-recorded completion.
    _seeded_complete = None

    log.info("GW%d: %d teams need picks, %d need seed data (%d total to process)...",
             current_gw, len(need_picks), len(need_seed), len(todo_ids))

    selection_rows: list[dict] = []
    chips_updates:  list[dict] = []
    seed_rows:      list[dict] = []

    need_picks_set = set(need_picks)
    need_seed_set  = set(need_seed)

    for i, tid in enumerate(todo_ids, 1):
        if i % 20 == 0:
            log.info("  Progress: %d / %d", i, len(todo_ids))

        # ── Picks ─────────────────────────────────────────────────────────
        picks_data = None
        if tid in need_picks_set:
            picks_data = fetch_picks(tid, current_gw)
            if picks_data:
                sel = build_selection_row(tid, current_gw, SEASON, picks_data)
                if sel:
                    selection_rows.append(sel)
            else:
                log.warning("  No picks for team %d (deadline not yet passed?)", tid)
            time.sleep(0.3)

        # ── Seed, primary source: the picks payload's entry_history ───────
        # It carries bank / event_transfers / event_transfers_cost / rank /
        # total_points — everything the seed needs — so the value that drives
        # the bank penalty no longer depends on a second, separately-failing
        # API call. On a seed-only retry this costs 1 call instead of 2.
        seed = None
        if tid in need_seed_set:
            if picks_data is None:
                picks_data = fetch_picks(tid, current_gw)
                time.sleep(0.3)
            seed = build_gw_seed_row(tid, current_gw, SEASON,
                                     (picks_data or {}).get("entry_history"))

        # ── History: seed fallback, and the only source of chips ──────────
        history = None
        if seed is None and tid in need_seed_set:
            history = fetch_team_history(tid)
            time.sleep(0.3)
            seed = build_gw_seed_row(tid, current_gw, SEASON,
                                     _hist_row_for_gw(history, current_gw))
            if seed is None:
                log.warning("  No usable bank for team %d GW%d yet — seed retries next run.",
                            tid, current_gw)

        if seed:
            seed_rows.append(seed)

        # Chips only change at a deadline, so refresh them alongside picks.
        if tid in need_picks_set:
            if history is None:
                history = fetch_team_history(tid)
                time.sleep(0.3)
            if history:
                chips_updates.append(build_chips_update(tid, SEASON, history))

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

