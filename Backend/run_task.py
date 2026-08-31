"""
run_task.py — Task dispatcher.
================================
Single entry point for the GitHub Actions workflow. Routes to the right
task module based on the first CLI argument.

Usage:
    python run_task.py refresh_reference
    python run_task.py refresh_picks
    python run_task.py refresh_live
    python run_task.py recalc_scores
    python run_task.py recalc_gw --gw-from 5
    python run_task.py recalc_gw --gw-from 5 --gw-to 10 --recalc-fpl-raw
    python run_task.py finalize_gw                    # current GW
    python run_task.py finalize_gw --gw-from 38       # explicit GW
    python run_task.py backfill_player_scores --gw-from 30 --gw-to 38
    python run_task.py backfill_team_value                            # one-off, after adding gw_scores.team_value
    python run_task.py backfill_transfers                              # one-off, after adding gw_scores.transfers_gw
    python run_task.py score_new_team --team 5388975
    python run_task.py score_new_team --league 400754        # bulk-backfill a whole FPL league
    python run_task.py create_mini_league --league 795730 --invite-code KINGANTI2627
    python run_task.py snapshot

Exit code propagates from the task itself (0 success, 1 failure).
"""

import argparse
import logging
import sys
from pathlib import Path

# Make the backend folder importable (tasks/, snapshot.py, db.py, etc.)
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Task registry ─────────────────────────────────────────────────────────────

def _task_refresh_reference(args):
    from tasks.refresh_reference import run
    return run()

def _task_refresh_picks(args):
    from tasks.refresh_picks import run
    return run()

def _task_refresh_live(args):
    from tasks.refresh_live import run
    return run()

def _task_recalc_scores(args):
    from tasks.recalc_scores import run
    return run()

def _task_score_new_team(args):
    from tasks.score_new_team import run, run_league
    if args.league:
        return run_league(args.league)
    if not args.team:
        log.error("score_new_team requires --team <team_id> or --league <league_id>")
        return 1
    return run(args.team)

def _task_create_mini_league(args):
    from tasks.create_mini_league import run
    from db import DEFAULT_SEASON
    if not args.league or not args.invite_code:
        log.error("create_mini_league requires --league <league_id> --invite-code <code>")
        return 1
    return run(args.league, args.invite_code, args.season or DEFAULT_SEASON,
               args.name, args.admin_code, args.admin_team_id)

def _task_snapshot(args):
    # snapshot.py::main() calls parse_args() internally; clear argv so it
    # doesn't see the "snapshot" positional arg we used to route here.
    saved, sys.argv[1:] = sys.argv[1:], []
    try:
        from snapshot import main as snapshot_main
        snapshot_main()
    finally:
        sys.argv[1:] = saved
    return 0


def _task_scheduler(args):
    from tasks.scheduler import run
    return run()

def _task_recalc_gw(args):
    from tasks.recalc_gw import run
    if not args.gw_from:
        log.error("recalc_gw requires --gw-from <gw>")
        return 1
    gw_to = args.gw_to or args.gw_from
    return run(args.gw_from, gw_to, recalc_fpl_raw=args.recalc_fpl_raw)

def _task_finalize_gw(args):
    from tasks.finalize_gw import run
    from db import DEFAULT_SEASON, get_current_gw
    gw = args.gw_from or get_current_gw(DEFAULT_SEASON)
    if gw is None:
        log.error("finalize_gw: no current GW and no --gw-from given")
        return 1
    return run(gw)

def _task_backfill_player_scores(args):
    from tasks.backfill_player_scores import run
    if not args.gw_from:
        log.error("backfill_player_scores requires --gw-from <gw>")
        return 1
    return run(args.gw_from, args.gw_to or args.gw_from)

def _task_backfill_team_value(args):
    from tasks.backfill_team_value import run
    return run()

def _task_backfill_transfers(args):
    from tasks.backfill_transfers import run
    return run()

def _task_audit_teams(args):
    from tasks.audit_teams import run
    leagues = [args.league] if args.league else None
    codes   = [args.invite_code] if args.invite_code else None
    return run(leagues, codes, args.all_db, args.no_history)

def _task_purge_teams(args):
    from tasks.purge_teams import run
    ids = [int(x) for x in args.team_ids.split(",") if x.strip()] if args.team_ids else None
    return run(ids, from_file=ids is None, mode=args.mode,
               apply=args.apply, skip_rerank=args.skip_rerank)

def _task_repair_bank(args):
    from tasks.repair_bank import run
    return run(
        gw_from         = args.gw_from or 1,
        gw_to           = args.gw_to,
        apply           = args.apply,
        also_fpl_fields = args.also_fpl_fields,
        force_recalc    = args.force_recalc,
        include_live    = args.include_live,
    )


TASKS = {
    "audit_teams":       _task_audit_teams,
    "backfill_player_scores": _task_backfill_player_scores,
    "backfill_team_value": _task_backfill_team_value,
    "backfill_transfers": _task_backfill_transfers,
    "create_mini_league": _task_create_mini_league,
    "finalize_gw":       _task_finalize_gw,
    "recalc_gw":         _task_recalc_gw,
    "recalc_scores":     _task_recalc_scores,
    "refresh_live":      _task_refresh_live,
    "refresh_picks":     _task_refresh_picks,
    "purge_teams":       _task_purge_teams,
    "refresh_reference": _task_refresh_reference,
    "repair_bank":       _task_repair_bank,
    "scheduler":         _task_scheduler,
    "score_new_team":    _task_score_new_team,
    "snapshot":          _task_snapshot,
}


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Anti-FPL task dispatcher")
    parser.add_argument("task", choices=sorted(TASKS.keys()), help="Task to run")
    parser.add_argument("--team",    type=int, help="Team ID (for score_new_team)")
    parser.add_argument("--league",  type=int, help="FPL league ID (for score_new_team / create_mini_league)")
    parser.add_argument("--invite-code", type=str, dest="invite_code",
                        help="Supabase invite_code label (for create_mini_league)")
    parser.add_argument("--season",  type=str, help="Season override (for create_mini_league)")
    parser.add_argument("--name",    type=str, help="Display name override (for create_mini_league)")
    parser.add_argument("--admin-code", type=str, dest="admin_code",
                        help="Admin code override (for create_mini_league)")
    parser.add_argument("--admin-team-id", type=int, dest="admin_team_id",
                        help="Admin team ID override (for create_mini_league)")
    parser.add_argument("--gw-from", type=int, dest="gw_from", help="Start GW (for recalc_gw / finalize_gw / backfill)")
    parser.add_argument("--gw-to",   type=int, dest="gw_to",   help="End GW (defaults to --gw-from)")
    parser.add_argument("--recalc-fpl-raw", action="store_true", dest="recalc_fpl_raw",
                        help="recalc_gw: recompute fpl_raw from player_gw_scores")
    parser.add_argument("--apply", action="store_true",
                        help="repair_bank: commit changes (default is a dry run)")
    parser.add_argument("--also-fpl-fields", action="store_true", dest="also_fpl_fields",
                        help="repair_bank: also repair xfer cost / transfers / rank / total")
    parser.add_argument("--force-recalc", action="store_true", dest="force_recalc",
                        help="repair_bank: re-score even when no bank values changed")
    parser.add_argument("--include-live", action="store_true", dest="include_live",
                        help="repair_bank: also repair the in-progress GW (patch only, no re-score)")
    parser.add_argument("--all-db", action="store_true", dest="all_db",
                        help="audit_teams: also audit every team already in the teams table")
    parser.add_argument("--no-history", action="store_true", dest="no_history",
                        help="audit_teams: skip the per-team history call")
    parser.add_argument("--team-ids", type=str, dest="team_ids",
                        help="purge_teams: comma-separated team ids (overrides excluded_teams.txt)")
    parser.add_argument("--mode", choices=["soft", "hard"], default="soft",
                        help="purge_teams: soft = eligible=false (default), hard = delete the row")
    parser.add_argument("--skip-rerank", action="store_true", dest="skip_rerank",
                        help="purge_teams: skip the ranking rebuild")
    args = parser.parse_args()

    log.info("Running task: %s", args.task)
    try:
        code = TASKS[args.task](args)
    except Exception:
        log.exception("Task %s crashed", args.task)
        return 1

    log.info("Task %s exit code %d", args.task, code)
    return code


if __name__ == "__main__":
    sys.exit(main())
