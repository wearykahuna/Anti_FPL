"""
worker.py — Portable live-scoring loop around the scheduler.
==============================================================
Runs scheduler ticks in a loop with a cadence that adapts to match state:
fast while matches are live, slow while idle, and (optionally) exits early
when there is nothing coming up — so a cron-based host doesn't pay for
sleeping runners.

The same file serves both hosting targets:

  GitHub Actions (hourly cron, 55-min window):
      python worker.py --max-minutes 55 --exit-when-idle
  Render background worker (runs forever):
      python worker.py

--exit-when-idle stops the loop when the state is idle AND no fixture kicks
off within IDLE_EXIT_HORIZON minutes — the next hourly cron run will be
back well before anything happens. Without the flag the loop just sleeps
through idle periods (correct for an always-on worker).
"""

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

LIVE_INTERVAL_S    = 60    # between ticks while there is anything to score
IDLE_INTERVAL_S    = 300   # between ticks while waiting for a kickoff
IDLE_EXIT_HORIZON  = 70    # minutes: exit-when-idle only if no kickoff within this
ERROR_BACKOFF_S    = 30    # after a failed tick — retry soon, don't lose 5 min

# Every state in which scheduler.tick() actually does scoring work deserves the
# fast cadence. "settling" was the expensive omission: tick() runs the FULL live
# path for it (scheduler.py), but it used to fall through to the 300s idle
# interval — and settling is exactly the window after a block of simultaneous
# kickoffs ends, when bonus points land and everyone is refreshing.
FAST_STATES = ("live", "settling", "provisional", "finalize")


def main() -> int:
    p = argparse.ArgumentParser(description="Anti-FPL live-scoring worker loop")
    p.add_argument("--max-minutes",    type=float, default=0,
                   help="Stop after this many minutes (0 = run forever)")
    p.add_argument("--live-interval",  type=int, default=LIVE_INTERVAL_S,
                   help="Seconds between ticks while live (default %(default)s)")
    p.add_argument("--idle-interval",  type=int, default=IDLE_INTERVAL_S,
                   help="Seconds between ticks while idle (default %(default)s)")
    p.add_argument("--exit-when-idle", action="store_true",
                   help="Exit if idle and no kickoff within the next "
                        f"{IDLE_EXIT_HORIZON} minutes (for cron-based hosts)")
    args = p.parse_args()

    from tasks.scheduler import tick

    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes else None
    worst_rc = 0

    while True:
        tick_started = time.monotonic()
        try:
            rc, state, next_ko = tick()
        except Exception:
            # One tick failing (a transient DB race, a flaky API call, ...)
            # shouldn't take down the rest of this hourly window — log it,
            # back off, and let the next tick try again.
            # Back off briefly, not for a full idle interval: a busy weekend
            # means more transient FPL/Supabase errors, and each one used to
            # cost 5 minutes of live coverage.
            log.exception("Scheduler tick raised — backing off %ds and retrying.", ERROR_BACKOFF_S)
            worst_rc = max(worst_rc, 1)
            if deadline is not None and time.monotonic() + ERROR_BACKOFF_S >= deadline:
                log.info("Max runtime reached — exiting.")
                return worst_rc
            time.sleep(ERROR_BACKOFF_S)
            continue

        worst_rc = max(worst_rc, rc)

        if state == "idle" and args.exit_when_idle:
            if next_ko is None or next_ko > IDLE_EXIT_HORIZON:
                log.info("Idle, next kickoff %s — exiting (next cron covers it).",
                         f"in {next_ko:.0f} min" if next_ko is not None else "none this GW")
                return worst_rc

        interval = args.live_interval if state in FAST_STATES else args.idle_interval

        # Sleep for the remainder of the interval, not the whole thing on top
        # of however long the tick took. At a 60s target a 30s tick would
        # otherwise yield a real cadence of 90s.
        elapsed  = time.monotonic() - tick_started
        sleep_for = max(0.0, interval - elapsed)
        if elapsed > interval:
            log.warning("Tick took %.1fs, over the %ds interval — cadence is "
                        "tick-bound, not sleep-bound.", elapsed, interval)

        if deadline is not None and time.monotonic() + sleep_for >= deadline:
            log.info("Max runtime reached — exiting.")
            return worst_rc

        log.info("State %s — tick took %.1fs, next tick in %.0fs", state, elapsed, sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    sys.exit(main())
