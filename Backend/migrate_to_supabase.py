"""
Migrate Anti-FPL scored data into Supabase.
============================================
Connects to Supabase, runs the scoring engine on the founding 10 teams,
and inserts everything into the database tables.

Usage:
    python migrate_to_supabase.py                  # founding 10 teams
    python migrate_to_supabase.py --league 248502  # full test league
    python migrate_to_supabase.py --reset          # wipe season data first

Reads SUPABASE_URL and SUPABASE_KEY from .env file in the same folder.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client, Client

from anti_fpl_scoring import (
    fetch_bootstrap,
    fetch_team_info,
    get_all_team_ids_from_league,
    score_league,
    current_gw,
)

# ── Config ────────────────────────────────────────────────────────────────────
SEASON = "2025/26"

FOUNDING_TEAMS = [
    5388975, 6703903, 6595399, 3640882, 5399604,
    6654853, 7667159, 1610262, 3155889, 911549,
]

# Batch size for inserts (Supabase handles large batches fine)
BATCH_SIZE = 500

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Supabase client ───────────────────────────────────────────────────────────

def get_supabase() -> Client:
    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_KEY in .env file.")
        sys.exit(1)
    log.info("Connecting to Supabase: %s", url)
    return create_client(url, key)


# ── Wipe season data (use with --reset) ───────────────────────────────────────

def reset_season(sb: Client, season: str) -> None:
    log.warning("Wiping all data for season %s ...", season)
    for tbl in ("gw_scores", "cup_fixtures", "mini_league_members", "teams", "gameweeks"):
        sb.table(tbl).delete().eq("season", season).execute()
        log.info("  Cleared %s", tbl)
    # mini_leagues last (FK from members)
    sb.table("mini_leagues").delete().eq("season", season).execute()
    log.info("  Cleared mini_leagues")


# ── Upsert helpers ────────────────────────────────────────────────────────────

def upsert_in_batches(sb: Client, table: str, rows: list[dict], on_conflict: str) -> None:
    """Insert/update rows in batches to avoid hitting payload limits."""
    if not rows:
        log.info("  %s: nothing to upsert", table)
        return
    total = len(rows)
    for i in range(0, total, BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        sb.table(table).upsert(batch, on_conflict=on_conflict).execute()
        log.info("  %s: upserted %d / %d", table, min(i + BATCH_SIZE, total), total)


# ── Build database rows ───────────────────────────────────────────────────────

def build_gameweek_rows(bootstrap: dict, season: str) -> list[dict]:
    rows = []
    for ev in bootstrap.get("events", []):
        rows.append({
            "season":       season,
            "gw":           ev["id"],
            "deadline":     ev.get("deadline_time"),
            "is_current":   ev.get("is_current", False),
            "is_finished":  ev.get("finished", False),
            "finalized_at": (datetime.now(timezone.utc).isoformat() if ev.get("finished") else None),
        })
    return rows


def build_team_row(team: dict, season: str) -> dict:
    # team here is one of the dicts returned by score_league()
    # Pull FPL "joined" timestamp separately (cheap, one call per team)
    fpl_info = fetch_team_info(team["team_id"]) or {}
    fpl_joined = fpl_info.get("joined_time")  # FPL API field

    eligible = any(g["gw"] == 1 and g.get("anti_gw_pts") is not None for g in team["gws"])

    return {
        "team_id":        team["team_id"],
        "season":         season,
        "manager":        team["manager"],
        "team_name":      team["team_name"],
        "fpl_joined_at":  fpl_joined,
        "anti_joined_at": datetime.now(timezone.utc).isoformat(),
        "eligible":       eligible,
    }


def build_gw_score_rows(team: dict, season: str) -> list[dict]:
    rows = []
    for g in team.get("gws", []):
        rows.append({
            "season":              season,
            "team_id":             team["team_id"],
            "gw":                  g["gw"],
            "fpl_raw_pts":         g.get("fpl_raw_pts"),
            "fpl_xfer_cost":       g.get("fpl_xfer_cost"),
            "fpl_gw_rank":         g.get("fpl_gw_rank"),
            "fpl_total":           g.get("fpl_total"),
            "active_chip":         g.get("active_chip") or "",
            "hit_pts":             g.get("hit_pts", 0) or 0,
            "inactive_count":      g.get("inactive_count"),
            "inactive_pen_pts":    g.get("inactive_pen_pts", 0) or 0,
            "bank":                g.get("bank"),
            "bank_pen":            g.get("bank_pen", False) or False,
            "bank_pen_pts":        g.get("bank_pen_pts", 0) or 0,
            "cvc_pen_pts":         g.get("cvc_pen_pts", 0) or 0,
            "chip_pen_pts":        g.get("chip_pen_pts", 0) or 0,
            "unused_chips":        g.get("unused_chips", []) or [],
            "total_pens_gw":       g.get("total_pens_gw", 0) or 0,
            "anti_gw_pts":         g.get("anti_gw_pts", 0) or 0,
            "anti_total":          g.get("anti_total", 0) or 0,
            "captain_element":     g.get("captain_element"),
            "captain_mult":        g.get("captain_mult", 1) or 1,
            "captain_pts":         g.get("captain_pts"),
            "vice_element":        g.get("vice_element"),
            "vice_pts":            g.get("vice_pts"),
            "cumulative_standing": g.get("standing"),
            "is_live":             g.get("live", False) or False,
        })
    return rows


def compute_gw_ranks(all_score_rows: list[dict]) -> None:
    """For each GW, assign gw_rank based on anti_gw_pts (lowest = #1)."""
    by_gw: dict[int, list[dict]] = {}
    for r in all_score_rows:
        by_gw.setdefault(r["gw"], []).append(r)
    for gw, rows in by_gw.items():
        rows.sort(key=lambda r: r["anti_gw_pts"] if r["anti_gw_pts"] is not None else 99999)
        for rank, r in enumerate(rows, 1):
            r["gw_rank"] = rank


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", type=int,            help="FPL mini-league ID")
    parser.add_argument("--reset",  action="store_true", help="Wipe season data first")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("Anti-FPL → Supabase migration  -  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("Season: %s", SEASON)
    log.info("=" * 60)

    sb = get_supabase()

    if args.reset:
        reset_season(sb, SEASON)

    # ── 1. Bootstrap and gameweeks ────────────────────────────────────────────
    log.info("Fetching FPL bootstrap...")
    bootstrap = fetch_bootstrap()
    if not bootstrap:
        log.error("Bootstrap fetch failed.")
        sys.exit(1)
    last_gw = current_gw(bootstrap)
    log.info("Current (last finished) GW: %d", last_gw)

    log.info("Upserting gameweeks...")
    gw_rows = build_gameweek_rows(bootstrap, SEASON)
    upsert_in_batches(sb, "gameweeks", gw_rows, on_conflict="season,gw")

    # ── 2. Team IDs ───────────────────────────────────────────────────────────
    if args.league:
        log.info("Pulling team IDs from mini-league %d...", args.league)
        team_ids = get_all_team_ids_from_league(args.league)
        log.info("Found %d teams.", len(team_ids))
    else:
        team_ids = FOUNDING_TEAMS
        log.info("Using %d founding teams.", len(team_ids))

    # ── 3. Score the season ──────────────────────────────────────────────────
    log.info("Scoring %d teams for GW1–%d...", len(team_ids), last_gw)
    teams = score_league(team_ids, last_gw)

    # ── 4. Build database rows ───────────────────────────────────────────────
    log.info("Building team rows...")
    team_rows = [build_team_row(t, SEASON) for t in teams]

    log.info("Building gw_scores rows...")
    all_score_rows: list[dict] = []
    for t in teams:
        all_score_rows.extend(build_gw_score_rows(t, SEASON))

    # Compute per-GW rank across the whole league
    log.info("Computing per-GW ranks across %d score rows...", len(all_score_rows))
    compute_gw_ranks(all_score_rows)

    # ── 5. Upsert into Supabase ──────────────────────────────────────────────
    log.info("Upserting %d teams...", len(team_rows))
    upsert_in_batches(sb, "teams", team_rows, on_conflict="team_id,season")

    log.info("Upserting %d gw_scores rows...", len(all_score_rows))
    upsert_in_batches(sb, "gw_scores", all_score_rows, on_conflict="season,team_id,gw")

    log.info("=" * 60)
    log.info("Migration complete.")
    log.info("  Season:       %s", SEASON)
    log.info("  Teams:        %d", len(team_rows))
    log.info("  GW score rows %d", len(all_score_rows))
    log.info("  GWs:          1 – %d", last_gw)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
