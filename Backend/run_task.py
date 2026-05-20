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
    python run_task.py recalc_gw --gw-from 5 --gw-to 10
    python run_task.py score_new_team --team 5388975
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
    from tasks.score_new_team import run
    if not args.team:
        log.error("score_new_team requires --team <team_id>")
        return 1
    return run(args.team)

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
    return run(args.gw_from, gw_to)


TASKS = {
    "recalc_gw":         _task_recalc_gw,
    "recalc_scores":     _task_recalc_scores,
    "refresh_live":      _task_refresh_live,
    "refresh_picks":     _task_refresh_picks,
    "refresh_reference": _task_refresh_reference,
    "scheduler":         _task_scheduler,
    "score_new_team":    _task_score_new_team,
    "snapshot":          _task_snapshot,
}


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Anti-FPL task dispatcher")
    parser.add_argument("task", choices=sorted(TASKS.keys()), help="Task to run")
    parser.add_argument("--team",    type=int, help="Team ID (for score_new_team)")
    parser.add_argument("--gw-from", type=int, dest="gw_from", help="Start GW (for recalc_gw)")
    parser.add_argument("--gw-to",   type=int, dest="gw_to",   help="End GW (for recalc_gw, defaults to --gw-from)")
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
