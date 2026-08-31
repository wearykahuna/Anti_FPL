"""
tasks/audit_teams.py — READ-ONLY audit of who is in the league and why.
========================================================================
Writes nothing. Produces a reviewable table so you can eyeball team names and
decide who doesn't belong, then paste the ones you want gone into
Backend/excluded_teams.txt and run purge_teams.

Two independent signals:

  started_event != 1   Objective. The FPL entry was created after the season
                       began, so it cannot be a genuine Anti FPL entrant.
  the team name        Subjective, and yours to judge — that is the whole
                       reason this prints a table instead of purging directly.

Verdicts:
  MISSING_FROM_DB    in an audited FPL league, but has no `teams` row
                     → run: python run_task.py score_new_team --team <id>
  LATE_ENTRY         started_event != 1 — purge candidate
  ORPHAN             in `teams` but in none of the audited leagues
  MANUALLY_EXCLUDED  already listed in excluded_teams.txt
  INELIGIBLE_DB      already flagged eligible=false in Supabase
  OK                 nothing to answer for

Output is TSV on stdout, so it pipes:
    python run_task.py audit_teams --league 123456 > audit.tsv

API cost: 1-2 calls per team (entry summary, plus history unless --no-history).

    python run_task.py audit_teams --league 123456
    python run_task.py audit_teams --invite-code KINGANTI2627
    python run_task.py audit_teams --all-db --no-history
"""

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import fetch_all_league_team_ids, fetch_team_history, fetch_team_info
from db      import DEFAULT_SEASON, load_excluded_team_ids, select_all

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON


def _resolve_invite_codes(season: str, invite_codes: list[str]) -> dict[int, list[str]]:
    """team_id -> [invite_code, ...] for the named mini-leagues."""
    membership: dict[int, list[str]] = {}
    for code in invite_codes:
        ml_rows = select_all("mini_leagues", {"season": season, "invite_code": code})
        if not ml_rows:
            log.warning("No mini_leagues row for invite_code=%s — skipping.", code)
            continue
        ml_id = ml_rows[0]["id"]
        for m in select_all("mini_league_members", {"mini_league_id": ml_id, "season": season}):
            membership.setdefault(m["team_id"], []).append(code)
    return membership


def run(league_ids: list[int] | None = None,
        invite_codes: list[str] | None = None,
        all_db: bool = False,
        no_history: bool = False) -> int:

    league_ids   = league_ids or []
    invite_codes = invite_codes or []

    if not league_ids and not invite_codes and not all_db:
        log.error("Nothing to audit. Pass --league, --invite-code and/or --all-db.")
        return 1

    log.info("Audit teams — season %s", SEASON)

    # ── Who is in the audited leagues (the "should be here" set) ──────────
    in_leagues: set[int] = set()
    league_of: dict[int, list[str]] = {}
    for lid in league_ids:
        for tid in fetch_all_league_team_ids(lid):
            in_leagues.add(tid)
            league_of.setdefault(tid, []).append(str(lid))

    ml_membership = _resolve_invite_codes(SEASON, invite_codes)
    for tid, codes in ml_membership.items():
        in_leagues.add(tid)
        league_of.setdefault(tid, []).extend(codes)

    # ── Who is in the DB (eligible_only=False: we must see tombstones too) ─
    db_rows = {t["team_id"]: t for t in select_all("teams", {"season": SEASON})}

    audited = in_leagues | (set(db_rows) if all_db else set())
    if not audited:
        log.error("No teams resolved — check the league id / invite code.")
        return 1

    excluded = load_excluded_team_ids()
    log.info("Auditing %d teams (%d from leagues, %d in DB, %d manually excluded)...",
             len(audited), len(in_leagues), len(db_rows), len(excluded))

    rows = []
    for i, tid in enumerate(sorted(audited), 1):
        if i % 25 == 0:
            log.info("  Progress: %d / %d", i, len(audited))

        info = fetch_team_info(tid)
        time.sleep(0.3)
        if not info:
            log.warning("No entry payload for team %d — skipping.", tid)
            continue

        started = info.get("started_event")
        db_row  = db_rows.get(tid)

        gws_played, chips_used = "", ""
        if not no_history:
            history = fetch_team_history(tid)
            time.sleep(0.3)
            if history:
                gws_played = str(len(history.get("current", [])))
                chips_used = "|".join(sorted(
                    {c.get("name", "") for c in history.get("chips", [])})) or "-"

        if tid in excluded:
            verdict = "MANUALLY_EXCLUDED"
        elif db_row is None:
            verdict = "MISSING_FROM_DB"
        elif started != 1:
            verdict = "LATE_ENTRY"
        elif tid not in in_leagues:
            verdict = "ORPHAN"
        elif not db_row.get("eligible"):
            verdict = "INELIGIBLE_DB"
        else:
            verdict = "OK"

        manager = (f"{info.get('player_first_name','')} "
                   f"{info.get('player_last_name','')}").strip()

        rows.append({
            "team_id":   tid,
            "manager":   manager,
            "team_name": info.get("name", ""),
            "started":   started,
            "joined":    (db_row or {}).get("fpl_joined_at") or info.get("joined_time") or "",
            "eligible":  "" if db_row is None else str(bool(db_row.get("eligible"))),
            "leagues":   ",".join(league_of.get(tid, [])) or "-",
            "chips":     chips_used,
            "gws":       gws_played,
            "verdict":   verdict,
        })

    # ── TSV to stdout ─────────────────────────────────────────────────────
    cols = ["team_id", "manager", "team_name", "started", "joined",
            "eligible", "leagues", "chips", "gws", "verdict"]
    print("\t".join(cols))
    for r in sorted(rows, key=lambda r: (r["verdict"], r["team_id"])):
        print("\t".join(str(r[c]) for c in cols))

    # ── Summary + a paste-ready candidate block ───────────────────────────
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    print()
    print("── Summary " + "─" * 50)
    for verdict in sorted(counts):
        print(f"  {verdict:<20} {counts[verdict]:>4}")

    candidates = [r for r in rows if r["verdict"] in ("LATE_ENTRY", "ORPHAN")]
    if candidates:
        print()
        print("── Purge candidates — review, delete any you want to KEEP, then")
        print("   paste the rest into Backend/excluded_teams.txt " + "─" * 8)
        for r in candidates:
            print(f'{r["team_id"]}   # {r["verdict"]}: "{r["team_name"]}" '
                  f'({r["manager"]}, started_event={r["started"]})')

    missing = [r for r in rows if r["verdict"] == "MISSING_FROM_DB"]
    if missing:
        print()
        print("── In a league but not scored yet — run score_new_team " + "─" * 12)
        for r in missing:
            print(f'  python run_task.py score_new_team --team {r["team_id"]}'
                  f'   # "{r["team_name"]}"')

    print()
    log.info("Audit complete — nothing was written.")
    return 0


if __name__ == "__main__":
    import argparse
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(description="Read-only audit of league membership and eligibility.")
    p.add_argument("--league", type=int, action="append", dest="league_ids",
                   help="FPL classic league id (repeatable)")
    p.add_argument("--invite-code", type=str, action="append", dest="invite_codes",
                   help="Supabase mini-league invite_code (repeatable)")
    p.add_argument("--all-db", action="store_true", dest="all_db",
                   help="Also audit every team already in the teams table")
    p.add_argument("--no-history", action="store_true", dest="no_history",
                   help="Skip the history call (halves API cost, drops chips/gws columns)")
    a = p.parse_args()
    sys.exit(run(a.league_ids, a.invite_codes, a.all_db, a.no_history))
