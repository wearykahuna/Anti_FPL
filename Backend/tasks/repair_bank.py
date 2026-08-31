"""
tasks/repair_bank.py — One-off repair of gw_scores.bank + re-score.
====================================================================
The bank penalty (+25 when bank > £3.0m) was silently missing for whole
gameweeks. Three write sites used to coerce a missing bank to 0, and
refresh_picks then treated 0 as "already seeded" and never re-fetched — so a
single missed deadline tick disabled the penalty for that team for the rest of
the GW *and* for the finalized record.

Those defects are fixed going forward. This task repairs the damage already in
the database: it re-fetches each team's authoritative history, writes the true
bank, and — critically — RE-SCORES the affected gameweeks so bank_pen,
bank_pen_pts, total_pens_gw, anti_gw_pts, anti_total and cumulative_standing
are all recomputed consistently. Patching the raw value alone (which is all
backfill_team_value did) can never produce a penalty.

Ordering matters. anti_total for GW N is anti_total(N-1) + anti_gw_pts(N), and
cumulative_standing ranks on anti_total — so changing GW N invalidates every
GW after it. The re-score therefore runs ASCENDING from the earliest changed
GW through the last finished GW, in a single recalc_gw call (its loop is
already ascending and re-ranks each GW as it goes).

Dry-run is the default: it prints a full diff and writes nothing. Pass
apply=True (--apply) to commit. Idempotent — a second run finds no differences
and skips the re-score entirely.

fetch_team_history() returns a team's ENTIRE season in one call, so this costs
one API call per team regardless of the GW range.

    python run_task.py repair_bank --gw-from 1                        # dry run
    python run_task.py repair_bank --gw-from 1 --apply                # commit
    python run_task.py repair_bank --gw-from 1 --include-live --apply # + live GW
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_team_history
from db      import (DEFAULT_SEASON, get_current_gw, get_gameweeks, get_team_ids,
                     select_all, update_rows)
from scoring import BANK_PEN, BANK_THRESHOLD

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON

_FPL_FIELDS = {
    "fpl_xfer_cost": "event_transfers_cost",
    "transfers_gw":  "event_transfers",
    "fpl_gw_rank":   "rank",
    "fpl_total":     "total_points",
}


def _last_finished_gw(season: str) -> int:
    """Highest GW flagged is_finished, or 0 if none are."""
    finished = [g["gw"] for g in get_gameweeks(season) if g.get("is_finished")]
    return max(finished) if finished else 0


def _would_pen(bank) -> bool:
    return bank is not None and bank > BANK_THRESHOLD


def run(gw_from: int = 1,
        gw_to: int | None = None,
        apply: bool = False,
        also_fpl_fields: bool = False,
        force_recalc: bool = False,
        include_live: bool = False) -> int:

    last_finished = _last_finished_gw(SEASON)
    current_gw    = get_current_gw(SEASON)

    # The live GW does NOT self-heal on its own. refresh_picks decides a team
    # is seeded on `bank is not None`, so rows already poisoned with a
    # fabricated 0 keep passing that test and are never re-fetched. Repairing
    # them needs --include-live.
    #
    # Live GWs are patched but NEVER re-scored here: recalc_gw scores with
    # gw_finished=True, which would prematurely finalize the GW and apply the
    # GW19/GW38 chip penalties. Patching `bank` alone is enough — the next
    # recalc_scores tick reads it back and recomputes the penalty live.
    max_gw = last_finished
    if include_live and current_gw:
        max_gw = max(max_gw, current_gw)
    if max_gw == 0:
        log.info("No finished GWs this season and no live GW to repair — nothing to do.")
        return 0

    requested_to = gw_to if gw_to is not None else max_gw
    gw_to = min(requested_to, max_gw)
    if requested_to > max_gw:
        log.warning("GW%d is beyond what can be repaired — clamping to GW%d%s.",
                    requested_to, max_gw,
                    "" if include_live else " (pass --include-live to also repair the live GW)")
    if gw_from > gw_to:
        log.info("Nothing to do: gw_from=%d is past GW%d.", gw_from, gw_to)
        return 0
    if include_live and current_gw and current_gw > last_finished:
        log.info("Including LIVE GW%d — bank will be patched, scoring left to "
                 "the next recalc_scores tick.", current_gw)

    log.info("Repair bank — season %s, GW%d-%d, mode=%s",
             SEASON, gw_from, gw_to, "APPLY" if apply else "DRY RUN")

    existing = {(r["team_id"], r["gw"]): r for r in select_all(
        "gw_scores", {"season": SEASON},
        select="team_id,gw,bank,bank_pen,bank_pen_pts,anti_gw_pts")}

    team_ids = get_team_ids(SEASON)
    log.info("Fetching history for %d eligible teams...", len(team_ids))

    value_rows: list[dict] = []
    diffs: list[tuple] = []
    no_history = 0
    skipped = 0

    for tid in team_ids:
        history = fetch_team_history(tid)
        if not history:
            log.warning("No history returned for team %d — skipping.", tid)
            no_history += 1
            continue

        for hist_gw in history.get("current", []):
            gw = hist_gw.get("event")
            if gw is None or not (gw_from <= gw <= gw_to):
                continue

            # Never INSERT: a team with no row for this GW simply didn't play it.
            row = existing.get((tid, gw))
            if row is None:
                skipped += 1
                continue

            true_bank = hist_gw.get("bank")
            if true_bank is None:
                continue

            stored_bank = row.get("bank")
            if stored_bank == true_bank:
                continue

            was = bool(row.get("bank_pen"))
            now = _would_pen(true_bank)
            diffs.append((tid, gw, stored_bank, true_bank, was, now,
                          (BANK_PEN if now else 0) - (BANK_PEN if was else 0)))

            new_row = {
                "season": SEASON, "team_id": tid, "gw": gw,
                "bank":        true_bank,
                "in_the_bank": true_bank,
            }
            if also_fpl_fields:
                for col, src in _FPL_FIELDS.items():
                    new_row[col] = hist_gw.get(src)
            value_rows.append(new_row)

    # Diff report — always printed, dry run or not.
    print()
    print("%8s  %3s  %7s  %7s  %7s  %7s  %6s"
          % ("team_id", "gw", "stored", "true", "was_pen", "now_pen", "delta"))
    print("-" * 60)
    for tid, gw, stored, true, was, now, delta in sorted(diffs, key=lambda d: (d[1], d[0])):
        s_str = "NULL" if stored is None else "%.1fm" % (stored / 10)
        print("%8d  %3d  %7s  %6.1fm  %7s  %7s  %+6d"
              % (tid, gw, s_str, true / 10, was, now, delta))
    print("-" * 60)

    gained = sum(1 for d in diffs if d[6] > 0)
    lost = sum(1 for d in diffs if d[6] < 0)
    earliest = min((d[1] for d in diffs), default=None)
    log.info("%d rows differ (%d penalties gained, %d lost). "
             "Earliest changed GW: %s. %d rows skipped (no gw_scores row), "
             "%d teams had no history.",
             len(diffs), gained, lost, earliest or "-", skipped, no_history)

    if not apply:
        log.info("DRY RUN — nothing written. Re-run with --apply to commit.")
        return 0

    if value_rows:
        log.info("Updating %d gw_scores rows...", len(value_rows))
        update_rows("gw_scores", value_rows, key_cols=["season", "team_id", "gw"])
    else:
        log.info("No bank values needed changing.")

    # Re-score ascending from the earliest change through the last FINISHED GW.
    # Changes confined to the live GW need no recalc_gw at all — recalc_scores
    # picks them up on its next tick.
    finished_changes = [d for d in diffs if d[1] <= last_finished]
    if finished_changes:
        recalc_start = min(min(d[1] for d in finished_changes), gw_from)
    elif force_recalc and gw_from <= last_finished:
        recalc_start = gw_from
    else:
        if diffs:
            log.info("Only live-GW rows changed — no re-score needed; the next "
                     "recalc_scores tick will apply the penalty.")
        else:
            log.info("No changes and --force-recalc not set — skipping re-score.")
        return 0

    log.info("Re-scoring GW%d-%d ascending (anti_total is cumulative, so every "
             "GW after a change must be recomputed)...", recalc_start, last_finished)
    from tasks.recalc_gw import run as run_recalc_gw
    rc = run_recalc_gw(recalc_start, last_finished, recalc_fpl_raw=False)
    if rc != 0:
        log.error("recalc_gw failed (exit %d) — bank values were written but "
                  "scores may be inconsistent. Re-run with --force-recalc.", rc)
        return rc

    log.info("repair_bank complete.")
    return 0


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Repair gw_scores.bank and re-score affected GWs.")
    p.add_argument("--gw-from", type=int, default=1, dest="gw_from")
    p.add_argument("--gw-to",   type=int, default=None, dest="gw_to")
    p.add_argument("--apply", action="store_true", help="Commit changes (default is a dry run)")
    p.add_argument("--also-fpl-fields", action="store_true", dest="also_fpl_fields",
                   help="Also repair xfer cost / transfers / rank / total (shifts anti_gw_pts)")
    p.add_argument("--force-recalc", action="store_true", dest="force_recalc",
                   help="Re-score even when no bank values changed")
    p.add_argument("--include-live", action="store_true", dest="include_live",
                   help="Also repair the in-progress GW (patch only, no re-score)")
    a = p.parse_args()
    sys.exit(run(a.gw_from, a.gw_to, a.apply, a.also_fpl_fields,
                 a.force_recalc, a.include_live))
