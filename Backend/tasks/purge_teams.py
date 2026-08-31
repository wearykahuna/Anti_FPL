"""
tasks/purge_teams.py — Remove teams that aren't genuinely playing Anti FPL.
============================================================================
Ad-hoc, run by hand after reviewing `audit_teams` output. Never scheduled.

WHY THIS DELETES ROWS RATHER THAN JUST TOMBSTONING
--------------------------------------------------
Setting `teams.eligible = false` is not enough. Four pages read data WITHOUT
an eligibility filter, so a purged team would stay visible and keep skewing
the numbers:

  weekly_stats.html             gw_scores for the whole season, no team filter
  mini_league_weekly_stats.html gw_scores filtered by member ids only
  mini_league_dashboard.html    teams filtered by member ids only
  players.html                  team_gw_selections unfiltered, while the
                                ownership denominator IS filtered — so
                                ownership percentages exceed 100%

update_rankings_for_gw() has no eligibility filter either, so leaving the
gw_scores rows in place would also let purged teams keep occupying ranking
positions. Hence: tombstone the identity row, delete the derived rows.

WHAT IT TOUCHES (in FK-safe order — children before parents, matching the
order db.wipe_season() uses)
  1. gw_scores            DELETE
  2. team_gw_selections   DELETE
  3. mini_league_members  DELETE  (create_mini_league is additive-only and
                                   never prunes, so stale membership is
                                   exactly what keeps a purged team on the
                                   mini-league pages)
  4. teams                eligible=false  (soft, default) or DELETE (--mode hard)
  5. rankings             update_rankings_for_gw for GW1..last finished

Soft mode is the default and is recommended: it keeps the manager/team name so
the purge stays auditable, and it is fully reversible via
`python run_task.py score_new_team --team <id>`, which rebuilds gw_scores and
team_gw_selections from the FPL API. Hard mode does not protect against
resurrection any better — Backend/excluded_teams.txt is what does that.

DRY RUN IS THE DEFAULT. Pass --apply to actually write.

    python run_task.py purge_teams                    # dry run, reads excluded_teams.txt
    python run_task.py purge_teams --apply
    python run_task.py purge_teams --team-ids 123,456 --apply
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import (
    DEFAULT_SEASON,
    delete_where,
    get_client,
    get_gameweeks,
    load_excluded_team_ids,
    select_all,
)

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON

# player_gw_scores is deliberately absent: it is keyed by player_id, not
# team_id, and is shared across every team.
_DERIVED_TABLES = ["gw_scores", "team_gw_selections"]


def _last_finished_gw(season: str) -> int:
    finished = [g["gw"] for g in get_gameweeks(season) if g.get("is_finished")]
    return max(finished) if finished else 0


def run(team_ids: list[int] | None = None,
        from_file: bool = True,
        mode: str = "soft",
        apply: bool = False,
        skip_rerank: bool = False) -> int:

    if team_ids:
        targets = set(team_ids)
        source = "--team-ids"
    elif from_file:
        targets = load_excluded_team_ids()
        source = "Backend/excluded_teams.txt"
    else:
        targets = set()
        source = "(none)"

    if not targets:
        log.info("No teams to purge (source: %s). Nothing to do.", source)
        return 0

    if mode not in ("soft", "hard"):
        log.error("Unknown mode %r — expected 'soft' or 'hard'.", mode)
        return 1

    log.info("Purge teams — season %s, mode=%s, source=%s, %s",
             SEASON, mode, source, "APPLY" if apply else "DRY RUN")

    known = {t["team_id"]: t for t in select_all("teams", {"season": SEASON})}
    unknown = sorted(targets - set(known))
    if unknown:
        log.warning("%d target(s) have no `teams` row this season and will be "
                    "skipped for the tombstone step: %s", len(unknown), unknown)

    # ── Count what would go ────────────────────────────────────────────────
    print()
    print("%10s  %-28s  %10s  %12s  %10s"
          % ("team_id", "team_name", "gw_scores", "selections", "memberships"))
    print("-" * 80)

    plan: dict[int, dict[str, int]] = {}
    for tid in sorted(targets):
        counts = {
            "gw_scores": len(select_all("gw_scores", {"season": SEASON, "team_id": tid},
                                        select="team_id")),
            "team_gw_selections": len(select_all("team_gw_selections",
                                                 {"season": SEASON, "team_id": tid},
                                                 select="team_id")),
            "mini_league_members": len(select_all("mini_league_members",
                                                  {"season": SEASON, "team_id": tid},
                                                  select="team_id")),
        }
        plan[tid] = counts
        name = (known.get(tid) or {}).get("team_name", "(not in teams)")
        print("%10d  %-28.28s  %10d  %12d  %10d"
              % (tid, name, counts["gw_scores"],
                 counts["team_gw_selections"], counts["mini_league_members"]))
    print("-" * 80)

    total = {k: sum(c[k] for c in plan.values())
             for k in ("gw_scores", "team_gw_selections", "mini_league_members")}
    eligible_before = sum(1 for t in known.values() if t.get("eligible"))
    eligible_after = eligible_before - sum(
        1 for tid in targets if (known.get(tid) or {}).get("eligible"))

    log.info("Would remove %d gw_scores, %d selections, %d memberships across %d teams.",
             total["gw_scores"], total["team_gw_selections"],
             total["mini_league_members"], len(targets))
    log.info("Eligible teams: %d -> %d", eligible_before, eligible_after)
    log.info("teams row: %s", "DELETE (hard)" if mode == "hard"
             else "eligible=false (soft, reversible)")

    if not apply:
        log.info("DRY RUN — nothing written. Re-run with --apply to commit.")
        return 0

    # ── Apply, children first ──────────────────────────────────────────────
    sb = get_client()
    for tid in sorted(targets):
        for table in _DERIVED_TABLES:
            delete_where(table, {"season": SEASON, "team_id": tid})
        delete_where("mini_league_members", {"season": SEASON, "team_id": tid})

        if tid not in known:
            continue
        if mode == "hard":
            delete_where("teams", {"season": SEASON, "team_id": tid})
        else:
            sb.from_("teams").update({"eligible": False}) \
              .eq("season", SEASON).eq("team_id", tid).execute()

    log.info("Purged %d teams.", len(targets))

    # ── Rankings ───────────────────────────────────────────────────────────
    # anti_total is per-team cumulative, so surviving teams' SCORES are
    # untouched and need no re-scoring. But gw_rank / cumulative_standing rank
    # across teams, so they must be rebuilt now that rows have gone.
    if skip_rerank:
        log.warning("--skip-rerank set: gw_rank / cumulative_standing are now STALE. "
                    "Run repair_bank (which re-ranks) or recalc_gw before trusting "
                    "the standings.")
        return 0

    last_finished = _last_finished_gw(SEASON)
    if last_finished == 0:
        log.info("No finished GWs — nothing to re-rank.")
        return 0

    log.info("Re-ranking GW1-%d...", last_finished)
    from tasks.recalc_scores import update_rankings_for_gw
    for gw in range(1, last_finished + 1):
        update_rankings_for_gw(SEASON, gw)

    log.info("purge_teams complete.")
    return 0


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Purge non-Anti-FPL teams. Dry run by default.")
    p.add_argument("--team-ids", type=str, dest="team_ids",
                   help="Comma-separated team ids (overrides the file)")
    p.add_argument("--mode", choices=["soft", "hard"], default="soft",
                   help="soft: teams.eligible=false (default). hard: delete the teams row.")
    p.add_argument("--apply", action="store_true", help="Commit (default is a dry run)")
    p.add_argument("--skip-rerank", action="store_true", dest="skip_rerank",
                   help="Skip the ranking rebuild (use when repair_bank will re-rank anyway)")
    a = p.parse_args()
    ids = [int(x) for x in a.team_ids.split(",") if x.strip()] if a.team_ids else None
    sys.exit(run(ids, from_file=ids is None, mode=a.mode,
                 apply=a.apply, skip_rerank=a.skip_rerank))
