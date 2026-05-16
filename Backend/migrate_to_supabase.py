"""
Migrate Anti-FPL scored data into Supabase.
============================================
Connects to Supabase, runs the scoring engine on the founding 10 teams,
and inserts everything into the database tables in a single pass —
live data and picks are fetched once and reused across all tables.

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
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from postgrest import SyncPostgrestClient

from anti_fpl_scoring import (
    fetch_bootstrap,
    fetch_team_info,
    fetch_live,
    fetch_picks,
    fetch_team_history,
    get_all_team_ids_from_league,
    current_gw,
    score_team_season,
    split_chips_by_half,
)

# ── Config ────────────────────────────────────────────────────────────────────
SEASON     = "2025/26"
BATCH_SIZE = 500

FOUNDING_TEAMS = [
    5388975, 6703903, 6595399, 3640882, 5399604,
    6654853, 7667159, 1610262, 3155889, 911549,
]

INACTIVE_PEN = 9   # mirror from scoring engine for player anti_pts

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Supabase client ───────────────────────────────────────────────────────────

def get_supabase() -> SyncPostgrestClient:
    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_KEY in .env file.")
        sys.exit(1)
    log.info("Connecting to Supabase: %s", url)
    return SyncPostgrestClient(
        f"{url}/rest/v1",
        headers={
            "apikey":        key,
            "Authorization": f"Bearer {key}",
        },
    )


# ── Wipe season data ──────────────────────────────────────────────────────────

def reset_season(sb: SyncPostgrestClient, season: str) -> None:
    log.warning("Wiping all data for season %s ...", season)
    for tbl in (
        "gw_scores", "cup_fixtures", "mini_league_members",
        "player_gw_scores", "team_gw_selections", "teams",
        "players", "gameweeks",
    ):
        sb.from_(tbl).delete().eq("season", season).execute()
        log.info("  Cleared %s", tbl)
    sb.from_("mini_leagues").delete().eq("season", season).execute()
    log.info("  Cleared mini_leagues")


# ── Upsert helper ─────────────────────────────────────────────────────────────

def upsert_in_batches(
    sb: SyncPostgrestClient, table: str, rows: list[dict], on_conflict: str
) -> None:
    if not rows:
        log.info("  %s: nothing to upsert", table)
        return
    total = len(rows)
    for i in range(0, total, BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        sb.from_(table).upsert(batch, on_conflict=on_conflict).execute()
        log.info("  %s: %d / %d rows upserted", table, min(i + BATCH_SIZE, total), total)


# ── Row builders ──────────────────────────────────────────────────────────────

def build_player_rows(bootstrap: dict, season: str) -> list[dict]:
    """Build reference rows for every FPL player from bootstrap data."""
    teams_by_id = {t["id"]: t for t in bootstrap.get("teams", [])}
    pos_map = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
    rows = []
    for el in bootstrap.get("elements", []):
        team = teams_by_id.get(el.get("team"), {})
        rows.append({
            "season":     season,
            "player_id":  el["id"],
            "web_name":   el.get("web_name", str(el["id"])),
            "first_name": el.get("first_name"),
            "last_name":  el.get("second_name"),
            "position":   pos_map.get(el.get("element_type"), "?"),
            "team_short": team.get("short_name"),
            "team_name":  team.get("name"),
            "now_cost":   el.get("now_cost"),
        })
    return rows


def build_gameweek_rows(bootstrap: dict, season: str) -> list[dict]:
    rows = []
    for ev in bootstrap.get("events", []):
        rows.append({
            "season":       season,
            "gw":           ev["id"],
            "deadline":     ev.get("deadline_time"),
            "is_current":   ev.get("is_current", False),
            "is_finished":  ev.get("finished", False),
            "finalized_at": datetime.now(timezone.utc).isoformat() if ev.get("finished") else None,
        })
    return rows


def build_team_row(team_id: int, scored: list[dict], season: str) -> dict:
    info       = fetch_team_info(team_id) or {}
    fpl_joined = info.get("joined_time")
    eligible   = any(g["gw"] == 1 and g.get("anti_gw_pts") is not None for g in scored)
    return {
        "team_id":        team_id,
        "season":         season,
        "manager":        f"{info.get('player_first_name','')} {info.get('player_last_name','')}".strip(),
        "team_name":      info.get("name", f"Team {team_id}"),
        "fpl_joined_at":  fpl_joined,
        "anti_joined_at": datetime.now(timezone.utc).isoformat(),
        "eligible":       eligible,
    }


def build_gw_score_rows(team_id: int, scored: list[dict], season: str) -> list[dict]:
    rows = []
    for g in scored:
        rows.append({
            "season":              season,
            "team_id":             team_id,
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


def build_player_gw_rows(
    gw: int, season: str, live_data: dict, is_live: bool
) -> list[dict]:
    rows = []
    for el in live_data.get("elements", []):
        stats    = el.get("stats", {})
        base_pts = stats.get("total_points", 0) or 0
        minutes  = stats.get("minutes", 0) or 0
        anti_pts = base_pts + INACTIVE_PEN if minutes == 0 else base_pts
        rows.append({
            "season":    season,
            "player_id": el["id"],
            "gw":        gw,
            "base_pts":  base_pts,
            "minutes":   minutes,
            "anti_pts":  anti_pts,
            "is_live":   is_live,
        })
    return rows


def build_selection_row(
    team_id: int, gw: int, season: str,
    picks_data: dict, is_live: bool,
) -> dict | None:
    picks   = picks_data.get("picks", [])
    captain = next((p["element"] for p in picks if p.get("is_captain")), None)
    vice    = next((p["element"] for p in picks if p.get("is_vice_captain")), None)
    squad   = [p["element"] for p in sorted(picks, key=lambda p: p["position"])]
    if not squad or captain is None or vice is None:
        return None
    return {
        "season":          season,
        "team_id":         team_id,
        "gw":              gw,
        "squad":           squad,
        "captain_id":      captain,
        "vice_captain_id": vice,
        "active_chip":     (picks_data.get("active_chip") or "").lower(),
        "is_live":         is_live,
    }


def compute_gw_ranks(all_score_rows: list[dict]) -> None:
    """Assign gw_rank per GW across all teams (lowest anti_gw_pts = rank 1)."""
    by_gw: dict[int, list[dict]] = {}
    for r in all_score_rows:
        by_gw.setdefault(r["gw"], []).append(r)
    for rows in by_gw.values():
        rows.sort(key=lambda r: r["anti_gw_pts"] if r["anti_gw_pts"] is not None else 99999)
        for rank, r in enumerate(rows, 1):
            r["gw_rank"] = rank


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", type=int,            help="FPL mini-league ID")
    parser.add_argument("--team",   type=int,            help="Single team ID only")
    parser.add_argument("--reset",  action="store_true", help="Wipe season data first")
    args = parser.parse_args()

    t0 = time.time()
    log.info("=" * 60)
    log.info("Anti-FPL → Supabase  —  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("Season: %s", SEASON)
    log.info("=" * 60)

    sb = get_supabase()

    if args.reset:
        reset_season(sb, SEASON)

    # ── Bootstrap ─────────────────────────────────────────────────────────────
    log.info("Fetching bootstrap...")
    bootstrap = fetch_bootstrap()
    if not bootstrap:
        log.error("Bootstrap fetch failed.")
        sys.exit(1)

    last_gw  = current_gw(bootstrap)
    finished = {e["id"] for e in bootstrap.get("events", []) if e.get("finished")}
    log.info("Last finished GW: %d", last_gw)

    # Upsert gameweeks
    upsert_in_batches(sb, "gameweeks", build_gameweek_rows(bootstrap, SEASON), "season,gw")

    # Upsert players (reference table)
    log.info("Upserting players reference table...")
    upsert_in_batches(sb, "players", build_player_rows(bootstrap, SEASON), "season,player_id")

    # ── Team IDs ──────────────────────────────────────────────────────────────
    if args.team:
        team_ids = [args.team]
    elif args.league:
        team_ids = get_all_team_ids_from_league(args.league)
    else:
        team_ids = FOUNDING_TEAMS
    log.info("Teams to process: %d", len(team_ids))

    # ── Fetch live data ONCE for all GWs ─────────────────────────────────────
    # live_cache  : gw → {player_id: minutes}
    # pts_cache   : gw → {player_id: total_points}
    # live_raw    : gw → full live response (reused for player_gw_scores)
    log.info("Fetching live data for GW1–%d (one pass)...", last_gw)
    live_cache: dict[int, dict[int, int]] = {}
    pts_cache:  dict[int, dict[int, int]] = {}
    live_raw:   dict[int, dict]           = {}

    for gw in range(1, last_gw + 1):
        raw = fetch_live(gw)
        if raw:
            live_raw[gw]   = raw
            live_cache[gw] = {e["id"]: e["stats"].get("minutes", 0)      for e in raw.get("elements", [])}
            pts_cache[gw]  = {e["id"]: e["stats"].get("total_points", 0) for e in raw.get("elements", [])}
            log.info("  GW%d: %d players indexed", gw, len(live_cache[gw]))
        time.sleep(0.3)

    # ── Player GW scores (all 700+ players, built from live_raw) ─────────────
    log.info("Building player_gw_scores from cached live data...")
    for gw, raw in live_raw.items():
        is_live   = gw not in finished
        p_rows    = build_player_gw_rows(gw, SEASON, raw, is_live)
        upsert_in_batches(sb, "player_gw_scores", p_rows, "season,player_id,gw")
    log.info("player_gw_scores complete.")

    # ── Score and persist each team ───────────────────────────────────────────
    all_score_rows: list[dict] = []
    team_rows:      list[dict] = []
    selection_rows: list[dict] = []

    for i, tid in enumerate(team_ids, 1):
        log.info("[%d/%d] Processing team %d...", i, len(team_ids), tid)

        history = fetch_team_history(tid)
        if not history:
            log.warning("  Skipping team %d — history fetch failed", tid)
            continue

        chips = history.get("chips", [])
        first_half, second_half = split_chips_by_half(chips)
        gw_rows_hist = {g["event"]: g for g in history.get("current", [])}

        # Fetch picks ONCE per team per GW — reuse for both scoring and selections
        picks_cache: dict[int, dict] = {}
        for gw in sorted(gw_rows_hist):
            picks = fetch_picks(tid, gw)
            if picks:
                picks_cache[gw] = picks
            time.sleep(0.3)

        # Score using already-fetched caches
        scored = score_team_season(
            team_id          = tid,
            history          = history,
            live_cache       = live_cache,
            pts_cache        = pts_cache,
            picks_cache      = picks_cache,
            last_gw          = last_gw,
            live_gw          = None if last_gw in finished else last_gw,
        )

        # Team info row
        team_rows.append(build_team_row(tid, scored, SEASON))

        # GW score rows
        score_rows = build_gw_score_rows(tid, scored, SEASON)
        all_score_rows.extend(score_rows)

        # Selection rows — built from same picks_cache, no extra API calls
        for gw, picks_data in picks_cache.items():
            is_live = gw not in finished
            sel     = build_selection_row(tid, gw, SEASON, picks_data, is_live)
            if sel:
                selection_rows.append(sel)

        time.sleep(0.4)

    # ── Compute per-GW ranks then persist ─────────────────────────────────────
    log.info("Computing per-GW ranks...")
    compute_gw_ranks(all_score_rows)

    # GW1 eligibility filter
    eligible_ids = {
        r["team_id"] for r in all_score_rows
        if r["gw"] == 1 and r.get("anti_gw_pts") is not None
    }
    team_rows      = [t for t in team_rows      if t["team_id"] in eligible_ids]
    all_score_rows = [r for r in all_score_rows if r["team_id"] in eligible_ids]
    selection_rows = [r for r in selection_rows if r["team_id"] in eligible_ids]
    log.info("Eligible teams after GW1 filter: %d", len(team_rows))

    log.info("Upserting teams...")
    upsert_in_batches(sb, "teams", team_rows, "team_id,season")

    log.info("Upserting gw_scores...")
    upsert_in_batches(sb, "gw_scores", all_score_rows, "season,team_id,gw")

    log.info("Upserting team_gw_selections...")
    upsert_in_batches(sb, "team_gw_selections", selection_rows, "season,team_id,gw")

    elapsed = time.time() - t0
    log.info("=" * 60)
    log.info("Migration complete in %.1f seconds.", elapsed)
    log.info("  Season:            %s", SEASON)
    log.info("  Teams:             %d", len(team_rows))
    log.info("  GW score rows:     %d", len(all_score_rows))
    log.info("  Selection rows:    %d", len(selection_rows))
    log.info("  GWs processed:     1 – %d", last_gw)
    log.info("=" * 60)


if __name__ == "__main__":
    main()