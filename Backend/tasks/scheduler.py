"""
tasks/scheduler.py — Dynamic live-task scheduler.
====================================================
Runs frequently from cron (or in a loop via worker.py). Decides whether to
invoke the live polling tasks based on actual fixture state in the DB.

Four states:
  1. Live / imminent  : a fixture is in play or kicks off within IMMINENT_MINUTES.
                        → refresh_live  then recalc_scores
  2. Provisional      : all fixtures finished_provisional but GW not officially done.
                        → run_provisional_pass  then recalc_scores(provisional=True)
  3. Finalize         : all fixtures officially finished but gw_scores rows are
                        still flagged live/provisional → finalize_gw (final player
                        pull, chip penalties, clear flags). Fires once per GW.
  4. Idle             : nothing happening → exit cheaply.

The scheduler also refreshes fixture states from the FPL API at the start of each
run so the DB doesn't go stale during an evening live window (the daily
refresh_reference only runs at 03:00 and 14:00 UTC).

API calls per run: 1 fixture-state call always; 1-2 live-data calls when live.
"""

import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import DEFAULT_SEASON, get_current_gw, get_fixtures, has_unfinalized_scores

log = logging.getLogger(__name__)

SEASON           = DEFAULT_SEASON
IMMINENT_MINUTES = 15   # fire live tasks if a fixture starts within this window


def _sync_fixture_states(season: str, current: int) -> list[dict]:
    """
    Fetch fresh fixture states from FPL API and update the DB.
    Called once per scheduler run so the DB never drifts stale during a live window.
    Falls back to DB fixtures if the API call fails.
    """
    from fpl_api import fetch_fixtures as fetch_fixtures_api
    from db import upsert
    fresh = fetch_fixtures_api(gw=current)
    if not fresh:
        log.warning("Fixture-state fetch failed — using DB fixtures.")
        return get_fixtures(season, gw=current)

    rows = [
        {
            "season":               season,
            "fixture_id":           f["id"],
            "gw":                   current,
            "team_h":               f["team_h"],
            "team_a":               f["team_a"],
            "kickoff_time":         f.get("kickoff_time"),
            "started":              f.get("started") or False,
            "finished":             f.get("finished") or False,
            "finished_provisional": f.get("finished_provisional") or False,
            "team_h_score":         f.get("team_h_score"),
            "team_a_score":         f.get("team_a_score"),
        }
        for f in fresh
    ]
    upsert("fixtures", rows, on_conflict="season,fixture_id")
    return rows


def _next_kickoff_minutes(fixtures: list[dict]) -> Optional[float]:
    """Minutes until the next future kickoff in this GW, or None if there isn't one."""
    now = datetime.now(timezone.utc)
    upcoming = []
    for f in fixtures:
        if f.get("started") or f.get("finished") or f.get("finished_provisional"):
            continue
        ko = f.get("kickoff_time")
        if not ko:
            continue
        try:
            ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
        except ValueError:
            continue
        if ko_dt > now:
            upcoming.append(ko_dt)
    if not upcoming:
        return None
    return (min(upcoming) - now).total_seconds() / 60


def _classify(fixtures: list[dict]) -> tuple[str, str]:
    """
    Classify the current state from a fresh fixture list.
    Returns (state, reason) where state is one of:
      "live"      — a fixture is in play or kicks off imminently
      "provisional" — every fixture in the GW is finished/finished_provisional
      "settling"  — some fixtures are finished_provisional (may still get late
                    stat corrections, e.g. bonus points) but the GW isn't all
                    done yet, and nothing else is live/imminent (e.g. a later
                    kickoff the same GW). Without this, those players' scores
                    would sit stale until the next fixture goes live and
                    incidentally drags them along — could be hours away.
      "idle"      — nothing live, nothing settling, nothing imminent
    """
    now     = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=IMMINENT_MINUTES)

    for f in fixtures:
        if f.get("finished") or f.get("finished_provisional"):
            continue  # skip done fixtures

        ko = f.get("kickoff_time")
        ko_dt = None
        if ko:
            try:
                ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
            except ValueError:
                pass

        # Fixture is in play (DB flag OR kickoff_time has passed — guards against stale DB)
        if f.get("started") or (ko_dt and ko_dt <= now):
            return "live", "match in play"

        # Fixture kicks off within the imminent window
        if ko_dt and now <= ko_dt <= horizon:
            return "live", f"kickoff at {ko_dt.strftime('%H:%M UTC')}"

    # No live / imminent fixture found.
    all_done = all(f.get("finished") or f.get("finished_provisional") for f in fixtures)
    any_prov = any(f.get("finished_provisional") and not f.get("finished") for f in fixtures)
    if all_done and any_prov:
        return "provisional", "all fixtures finished_provisional"
    if any_prov:
        return "settling", "some fixtures finished_provisional, awaiting a later kickoff this GW"

    return "idle", "no live/imminent matches"


def tick() -> tuple[int, str, Optional[float]]:
    """
    One scheduler pass.
    Returns (exit_code, state, minutes_until_next_kickoff).
    state is one of "live", "settling", "provisional", "finalize", "idle".
    """
    log.info("Scheduler — %s", datetime.now().strftime("%Y-%m-%d %H:%M"))

    current = get_current_gw(SEASON)
    if current is None:
        log.info("No current GW — exiting.")
        return 0, "idle", None

    # Refresh fixture states from FPL API before classifying
    fixtures = _sync_fixture_states(SEASON, current)
    if not fixtures:
        log.info("No fixtures for current GW — exiting.")
        return 0, "idle", None

    state, reason = _classify(fixtures)
    next_ko = _next_kickoff_minutes(fixtures)

    # Idle + everything officially finished + rows still flagged → finalize once
    if (state == "idle"
            and all(f.get("finished") for f in fixtures)
            and has_unfinalized_scores(SEASON, current)):
        state, reason = "finalize", "GW officially finished, scores not yet finalized"

    log.info("State: %s (%s)", state, reason)

    if state == "idle":
        return 0, state, next_ko

    if state in ("live", "settling"):
        # "settling" reuses the exact same non-provisional calls — they're
        # already scoped to only clubs with an active/finished_provisional
        # fixture (get_active_player_ids / teams_with_active_players), and
        # calc_fpl_raw already applies auto-subs progressively from the real
        # finished_players set, so this correctly settles a team the moment
        # every one of ITS fixtures is finished_provisional — it doesn't
        # need to wait for an unrelated later kickoff elsewhere in the GW.
        from tasks.refresh_live  import run as run_refresh_live
        from tasks.recalc_scores import run as run_recalc_scores

        log.info("▶ refresh_live")
        rc1 = run_refresh_live(fixtures=fixtures)  # already synced above this tick
        log.info("◀ refresh_live exit %d", rc1)

        log.info("▶ recalc_scores")
        rc2 = run_recalc_scores()
        log.info("◀ recalc_scores exit %d", rc2)

        return max(rc1, rc2), state, next_ko

    if state == "finalize":
        from tasks.finalize_gw import run as run_finalize
        log.info("▶ finalize_gw (GW%d)", current)
        rc = run_finalize(current)
        log.info("◀ finalize_gw exit %d", rc)
        return rc, state, next_ko

    # state == "provisional"
    from tasks.refresh_live  import run_provisional_pass
    from tasks.recalc_scores import run as run_recalc_scores

    log.info("▶ provisional pass (GW%d)", current)
    rc1 = run_provisional_pass(current, fixtures=fixtures)  # already synced above this tick
    log.info("◀ provisional pass exit %d", rc1)

    log.info("▶ recalc_scores [provisional]")
    rc2 = run_recalc_scores(provisional=True)
    log.info("◀ recalc_scores exit %d", rc2)

    return max(rc1, rc2), state, next_ko


def run() -> int:
    """Single-shot entry point (run_task.py). See worker.py for the loop."""
    rc, _state, _next_ko = tick()
    return rc


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())
