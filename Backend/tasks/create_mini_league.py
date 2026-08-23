"""
tasks/create_mini_league.py — Create/refresh a mini-league from an FPL league ID.
====================================================================================
On-demand task. Run once per new mini-league (safe to re-run to resync
membership if people join/leave the FPL league later).

Given an FPL classic league ID and an invite_code (the label the frontend
pages use to look this league up in Supabase — see mini_league.html /
mini_league_dashboard.html), this:
  1. Pulls every team_id currently in that FPL league.
  2. Backfills any team not already in the `teams` table for the season
     (full score_new_team.run() per missing team — cheap no-op if the team
     was already seeded via a --league bulk run of a superset league).
  3. Upserts the mini_leagues row (season, invite_code) and every
     mini_league_members row (mini_league_id, team_id).

Usage:
    python tasks/create_mini_league.py --league 795730 --invite-code KINGANTI2627
"""

import argparse
import logging
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_all_league_team_ids, fetch_league_standings_page
from db      import DEFAULT_SEASON, select_all, upsert

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON


def run(league_id: int, invite_code: str, season: str = SEASON,
        name: str | None = None, admin_code: str | None = None,
        admin_team_id: int | None = None) -> int:
    log.info("=" * 60)
    log.info("Create mini-league %d -> invite_code=%s, season=%s", league_id, invite_code, season)
    log.info("=" * 60)

    team_ids = fetch_all_league_team_ids(league_id)
    if not team_ids:
        log.error("No teams found in FPL league %d — aborting.", league_id)
        return 1
    log.info("Found %d teams in FPL league %d", len(team_ids), league_id)

    page1 = fetch_league_standings_page(league_id, 1) or {}
    league_meta = page1.get("league", {})

    if not name:
        name = league_meta.get("name") or f"League {league_id}"
    log.info("League name: %s", name)

    if not admin_team_id:
        admin_team_id = league_meta.get("admin_entry")
    if not admin_team_id:
        log.error("No admin_team_id found (FPL league has no admin_entry) — pass --admin-team-id explicitly.")
        return 1
    log.info("Admin team id: %s", admin_team_id)

    if not admin_code:
        admin_code = secrets.token_hex(4)
    log.info("Admin code: %s (auto-generated unless you passed --admin-code)", admin_code)

    # ── Backfill any team not yet in `teams` for this season ────────────────
    existing = {t["team_id"] for t in select_all("teams", {"season": season})}
    missing  = [tid for tid in team_ids if tid not in existing]
    if missing:
        log.info("Backfilling %d team(s) not yet in `teams`...", len(missing))
        from tasks.score_new_team import run as score_new_team_run
        for i, tid in enumerate(missing, 1):
            log.info("  [%d/%d] team %d", i, len(missing), tid)
            score_new_team_run(tid)
            time.sleep(0.4)
    else:
        log.info("All %d teams already present in `teams`.", len(team_ids))

    # ── Upsert the mini_leagues row ──────────────────────────────────────────
    upsert("mini_leagues",
           [{"season": season, "invite_code": invite_code, "name": name,
             "admin_code": admin_code, "admin_team_id": admin_team_id}],
           on_conflict="season,invite_code")

    ml_rows = select_all("mini_leagues", {"season": season, "invite_code": invite_code})
    if not ml_rows:
        log.error("mini_leagues row not found after upsert — check the table's "
                   "unique constraint matches on_conflict='season,invite_code'.")
        return 1
    mini_league_id = ml_rows[0]["id"]
    log.info("mini_leagues id = %s", mini_league_id)

    # ── Upsert membership ─────────────────────────────────────────────────────
    member_rows = [{"mini_league_id": mini_league_id, "team_id": tid, "season": season} for tid in team_ids]
    upsert("mini_league_members", member_rows, on_conflict="mini_league_id,team_id")

    log.info("Mini-league ready: %d members linked to mini_league_id=%s",
             len(team_ids), mini_league_id)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--league", type=int, required=True, help="FPL classic league ID")
    p.add_argument("--invite-code", type=str, required=True, dest="invite_code",
                   help="Label the frontend uses to look up this mini-league in Supabase")
    p.add_argument("--season", type=str, default=SEASON)
    p.add_argument("--name", type=str, default=None,
                   help="Display name for the mini_leagues row (defaults to the FPL league's own name)")
    p.add_argument("--admin-code", type=str, default=None, dest="admin_code",
                   help="Admin code for the mini_leagues row (auto-generated if omitted)")
    p.add_argument("--admin-team-id", type=int, default=None, dest="admin_team_id",
                   help="Admin's FPL team ID (defaults to the FPL league's own admin_entry)")
    args = p.parse_args()
    return run(args.league, args.invite_code, args.season, args.name,
               args.admin_code, args.admin_team_id)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(main())
