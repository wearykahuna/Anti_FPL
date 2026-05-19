"""
tasks/scheduler.py — Dynamic live-task scheduler.
====================================================
Runs frequently from cron. Decides whether to invoke the live polling tasks
based on actual fixture state in the DB.

A match is "imminent or live" when:
  - Any fixture has started=true and finished=false (currently in play), OR
  - Any fixture has started=false and kickoff_time is within IMMINENT_MINUTES

If true → invokes refresh_live then recalc_scores.
If false → exits cheaply.

API calls per run: 0 (DB only, unless live tasks fire).
"""

import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import DEFAULT_SEASON, get_current_gw, get_fixtures

log = logging.getLogger(__name__)

SEASON           = DEFAULT_SEASON
IMMINENT_MINUTES = 15   # fire live tasks if a fixture starts within this window


def is_live_or_imminent(season: str) -> tuple[bool, int | None, str]:
    """
    Returns (should_fire, current_gw, reason).
    """
    current = get_current_gw(season)
    if current is None:
        return False, None, "no current GW"

    fixtures = get_fixtures(season, gw=current)
    if not fixtures:
        return False, current, "no fixtures for current GW"

    now      = datetime.now(timezone.utc)
    horizon  = now + timedelta(minutes=IMMINENT_MINUTES)

    for f in fixtures:
        if f.get("finished") or f.get("finished_provisional"):
            continue

        # In play right now
        if f.get("started"):
            return True, current, "match in play"

        # Imminent kickoff
        ko = f.get("kickoff_time")
        if not ko:
            continue
        try:
            ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
        except ValueError:
            continue
        if now <= ko_dt <= horizon:
            return True, current, f"kickoff at {ko_dt.strftime('%H:%M UTC')}"

    return False, current, "no live/imminent matches"


def run() -> int:
    log.info("Scheduler — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))

    fire, current_gw, reason = is_live_or_imminent(SEASON)
    log.info("Decision: fire=%s (GW=%s, reason=%s)", fire, current_gw, reason)

    if not fire:
        return 0

    # Run the live tasks in sequence
    from tasks.refresh_live  import run as run_refresh_live
    from tasks.recalc_scores import run as run_recalc_scores

    log.info("▶ refresh_live")
    rc1 = run_refresh_live()
    log.info("◀ refresh_live exit %d", rc1)

    log.info("▶ recalc_scores")
    rc2 = run_recalc_scores()
    log.info("◀ recalc_scores exit %d", rc2)

    return max(rc1, rc2)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())
