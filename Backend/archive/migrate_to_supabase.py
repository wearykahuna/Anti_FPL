"""
Migrate Anti-FPL scored data into Supabase.
============================================
v20 — Smart-skip enabled. Only fetches and scores what has changed:
  - Live GW (always re-fetched and re-scored every run)
  - Finished GWs not yet stored in Supabase (catches first run / gap recovery)
  - Skips completed GWs that are already stored
  - Skips future GWs entirely

For 200 teams during a live GW: ~200-400 API calls (down from ~7,400+).
After the season finishes, becomes a no-op.

Usage:
    python migrate_to_supabase.py                  # default: score whoever's in teams table
    python migrate_to_supabase.py --league 248502  # legacy: pull league + score all
    python migrate_to_supabase.py --team 5388975   # single team
    python migrate_to_supabase.py --reset          # wipe season data first
    python migrate_to_supabase.py --force-full     # disable smart-skip, re-score everything

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
    score_team_season,
    build_player_type_map,
)

# ── Config ────────────────────────────────────────────────────────────────────
SEASON     = "2025/26"
BATCH_SIZE = 500

# Optional: seed the teams table from this FPL mini-league on first run.
SEED_LEAGUE_ID = 248502

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


# ── Wipe season data (preserves mini-leagues) ─────────────────────────────────

def reset_season(sb: SyncPostgrestClient, season: str) -> None:
    log.warning("Wiping scoring data for season %s (preserves mini-leagues)...", season)
    for tbl in (
        "gw_scores", "cup_fixtures",
        "player_gw_scores", "team_gw_selections", "teams",
        "players", "fixtures", "gameweeks",
    ):
        sb.from_(tbl).delete().eq("season", season).execute()
        log.info("  Cleared %s", tbl)
    log.info("  (Skipped mini_leagues and mini_league_members)")


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
            "team_id":    el.get("team"),
            "team_short": team.get("short_name"),
            "team_name":  team.get("name"),
            "now_cost":   el.get("now_cost"),
        })
    return rows


def fetch_all_fixtures() -> list[dict]:
    import requests
    try:
        r = requests.get(
            "https://fantasy.premierleague.com/api/fixtures/",
            timeout=20,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Fixtures fetch failed: %s", exc)
        return []


def build_fixture_rows(fixtures: list[dict], season: str) -> list[dict]:
    rows = []
    for f in fixtures:
        rows.append({
            "season":               season,
            "fixture_id":           f["id"],
            "gw":                   f.get("event") or 0,
            "team_h":               f["team_h"],
            "team_a":               f["team_a"],
            "kickoff_time":         f.get("kickoff_time"),
            "started":              f.get("started") or False,
            "finished":             f.get("finished") or False,
            "finished_provisional": f.get("finished_provisional") or False,
            "team_h_score":         f.get("team_h_score"),
            "team_a_score":         f.get("team_a_score"),
        })
    return [r for r in rows if r["gw"] > 0]


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


def build_team_row(team_id: int, scored: list[dict], season: str,
                   has_gw1: bool) -> dict:
    info       = fetch_team_info(team_id) or {}
    fpl_joined = info.get("joined_time")
    return {
        "team_id":        team_id,
        "season":         season,
        "manager":        f"{info.get('player_first_name','')} {info.get('player_last_name','')}".strip(),
        "team_name":      info.get("name", f"Team {team_id}"),
        "fpl_joined_at":  fpl_joined,
        "anti_joined_at": datetime.now(timezone.utc).isoformat(),
        "eligible":       has_gw1,
    }


def build_gw_score_rows(team_id: int, scored: list[dict], season: str,
                        live_gw: int | None = None) -> list[dict]:
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
            "is_live":             g["gw"] == live_gw,
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


# ── Per-GW rank (only for GWs we just scored) ─────────────────────────────────

def update_ranks_for_gw(sb: SyncPostgrestClient, season: str, gw: int) -> None:
    """
    Recompute gw_rank (per-GW rank by anti_gw_pts) for one specific GW.
    Pulls all team scores for that GW from Supabase, ranks lowest→highest,
    writes back gw_rank.
    """
    rows = (sb.from_("gw_scores")
              .select("id,team_id,anti_gw_pts")
              .eq("season", season)
              .eq("gw", gw)
              .execute().data or [])
    if not rows:
        return
    rows.sort(key=lambda r: r.get("anti_gw_pts") if r.get("anti_gw_pts") is not None else 99999)
    updates = []
    for rank, r in enumerate(rows, 1):
        updates.append({
            "id":          r["id"],
            "season":      season,
            "team_id":     r["team_id"],
            "gw":          gw,
            "gw_rank":     rank,
        })
    if updates:
        sb.from_("gw_scores").upsert(updates, on_conflict="season,team_id,gw").execute()
    log.info("  GW%d: ranked %d teams", gw, len(rows))


# ── Determine GWs that need work this run ─────────────────────────────────────

def determine_gws_to_process(
    sb: SyncPostgrestClient,
    bootstrap: dict,
    force_full: bool,
) -> tuple[set[int], int | None]:
    """
    Returns (gws_to_process, live_gw).

    A GW needs processing if:
      - It's the current/live GW (data changes every poll), OR
      - It's finished but not yet stored in gw_scores at the season level
    Future GWs and already-complete-and-stored GWs are skipped.
    """
    finished = {e["id"] for e in bootstrap.get("events", []) if e.get("finished")}
    current  = next((e["id"] for e in bootstrap.get("events", []) if e.get("is_current")), None)
    live_gw  = current if current and current not in finished else None

    if force_full:
        # Process everything from GW1 to last GW (live or last finished)
        last_gw = current if current else (max(finished) if finished else 1)
        gws = set(range(1, last_gw + 1))
        log.info("[force-full] Processing all GWs 1–%d", last_gw)
        return gws, live_gw

    # GWs we've already finalized (stored at least once)
    existing_rows = (sb.from_("gameweeks")
                       .select("gw,finalized_at")
                       .eq("season", SEASON)
                       .execute().data or [])
    already_finalized = {r["gw"] for r in existing_rows if r.get("finalized_at")}

    gws_to_process = set()

    # Always process the live GW
    if live_gw is not None:
        gws_to_process.add(live_gw)

    # Any finished GW we haven't yet finalized in Supabase
    for gw in finished:
        if gw not in already_finalized:
            gws_to_process.add(gw)

    return gws_to_process, live_gw


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league",     type=int,            help="FPL mini-league ID")
    parser.add_argument("--team",       type=int,            help="Single team ID only")
    parser.add_argument("--reset",      action="store_true", help="Wipe season data first")
    parser.add_argument("--force-full", action="store_true", help="Disable smart-skip (re-score everything)")
    args = parser.parse_args()

    t0 = time.time()
    log.info("=" * 60)
    log.info("Anti-FPL → Supabase v20  —  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
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

    finished      = {e["id"] for e in bootstrap.get("events", []) if e.get("finished")}
    current       = next((e["id"] for e in bootstrap.get("events", []) if e.get("is_current")), None)
    last_finished = max(finished) if finished else 0
    last_gw       = current if current else last_finished
    player_type   = build_player_type_map(bootstrap)
    log.info("Last finished GW: %d, Current GW: %s", last_finished, current)

    # Always upsert these reference tables — cheap and keeps state fresh
    upsert_in_batches(sb, "gameweeks", build_gameweek_rows(bootstrap, SEASON), "season,gw")
    upsert_in_batches(sb, "players", build_player_rows(bootstrap, SEASON), "season,player_id")
    log.info("Fetching and upserting fixtures...")
    upsert_in_batches(sb, "fixtures", build_fixture_rows(fetch_all_fixtures(), SEASON), "season,fixture_id")

    # ── Smart-skip: decide which GWs need work ────────────────────────────────
    gws_to_process, live_gw = determine_gws_to_process(sb, bootstrap, args.force_full)
    log.info("=" * 60)
    log.info("GWs to process this run: %s", sorted(gws_to_process) or "[none]")
    log.info("Live GW: %s", live_gw)
    log.info("=" * 60)

    if not gws_to_process:
        log.info("Nothing to do — all data current. Exiting.")
        elapsed = time.time() - t0
        log.info("Run completed in %.1f seconds.", elapsed)
        return

    # ── Team IDs ──────────────────────────────────────────────────────────────
    if args.team:
        team_ids = [args.team]
        log.info("Single-team mode: %d", args.team)
    elif args.league:
        log.info("Pulling team IDs from mini-league %d (legacy mode)...", args.league)
        team_ids = get_all_team_ids_from_league(args.league)
    else:
        existing = sb.from_("teams").select("team_id").eq("season", SEASON).execute().data or []
        team_ids = [r["team_id"] for r in existing]

        if not team_ids:
            log.info("Teams table empty — seeding from league %d...", SEED_LEAGUE_ID)
            team_ids = get_all_team_ids_from_league(SEED_LEAGUE_ID)
            log.info("Pulled %d team IDs", len(team_ids))
        else:
            log.info("Loaded %d existing teams from teams table", len(team_ids))

    log.info("Teams to process: %d", len(team_ids))

    # ── Fetch live data ONLY for GWs we're processing ─────────────────────────
    log.info("Fetching live data for %d GW(s)...", len(gws_to_process))
    live_cache: dict[int, dict[int, int]] = {}
    pts_cache:  dict[int, dict[int, int]] = {}

    for gw in sorted(gws_to_process):
        raw = fetch_live(gw)
        if not raw:
            log.warning("  GW%d: live fetch failed", gw)
            continue
        live_cache[gw] = {e["id"]: e["stats"].get("minutes", 0)      for e in raw.get("elements", [])}
        pts_cache[gw]  = {e["id"]: e["stats"].get("total_points", 0) for e in raw.get("elements", [])}
        log.info("  GW%d: %d players indexed", gw, len(live_cache[gw]))

        # Upsert player_gw_scores for this GW immediately
        is_live = gw == live_gw
        upsert_in_batches(sb, "player_gw_scores",
                          build_player_gw_rows(gw, SEASON, raw, is_live),
                          "season,player_id,gw")

        time.sleep(0.3)

    # ── Per-team scoring ──────────────────────────────────────────────────────
    all_score_rows:    list[dict] = []
    team_rows:         list[dict] = []
    selection_rows:    list[dict] = []
    new_team_ids:      set[int]   = set()  # teams we're seeing for the first time

    # Pre-fetch each team's existing scored GWs to decide what to fetch per team
    existing_scores = (sb.from_("gw_scores")
                          .select("team_id,gw")
                          .eq("season", SEASON)
                          .execute().data or [])
    existing_by_team: dict[int, set[int]] = {}
    for r in existing_scores:
        existing_by_team.setdefault(r["team_id"], set()).add(r["gw"])

    for i, tid in enumerate(team_ids, 1):
        # GWs this specific team still needs scored:
        # = the global gws_to_process MINUS this team's already-scored GWs
        # + always re-score live GW
        already_scored = existing_by_team.get(tid, set())
        team_gws_to_score = (gws_to_process - already_scored)
        if live_gw is not None:
            team_gws_to_score.add(live_gw)

        # If team has never been scored at all → first-time backfill (full season)
        if not already_scored and not args.force_full:
            log.info("[%d/%d] team %d → NEW team, full backfill GW1–%d",
                     i, len(team_ids), tid, last_gw)
            team_gws_to_score = set(range(1, last_gw + 1))
            new_team_ids.add(tid)

        if not team_gws_to_score:
            continue

        log.info("[%d/%d] team %d → scoring GWs %s",
                 i, len(team_ids), tid, sorted(team_gws_to_score))

        # Fetch full history (cheap, one call)
        history = fetch_team_history(tid)
        if not history:
            log.warning("  Skipping team %d — history fetch failed", tid)
            continue

        gw_rows_hist = {g["event"]: g for g in history.get("current", [])}
        has_gw1      = 1 in gw_rows_hist  # eligibility check

        # Fetch live caches for any team-specific GWs not yet in live_cache
        # (only relevant for new teams needing full backfill)
        missing_live_gws = team_gws_to_score - set(live_cache.keys())
        for gw in sorted(missing_live_gws):
            raw = fetch_live(gw)
            if raw:
                live_cache[gw] = {e["id"]: e["stats"].get("minutes", 0)      for e in raw.get("elements", [])}
                pts_cache[gw]  = {e["id"]: e["stats"].get("total_points", 0) for e in raw.get("elements", [])}
            time.sleep(0.3)

        # Fetch picks ONLY for GWs we're scoring for this team
        picks_cache: dict[int, dict] = {}
        for gw in sorted(team_gws_to_score):
            if gw not in gw_rows_hist:
                continue
            picks = fetch_picks(tid, gw)
            if picks:
                picks_cache[gw] = picks
            time.sleep(0.3)

        if not picks_cache:
            log.warning("  Team %d: no picks fetched, skipping", tid)
            continue

        # Score
        scored = score_team_season(
            team_id     = tid,
            history     = history,
            live_cache  = live_cache,
            pts_cache   = pts_cache,
            picks_cache = picks_cache,
            last_gw     = max(team_gws_to_score),
            player_type = player_type,
            live_gw     = live_gw,
        )

        # Filter scored to only the GWs we wanted (engine may return more)
        scored = [g for g in scored if g["gw"] in team_gws_to_score]

        # ── Cumulative total fix-up ──────────────────────────────────────────
        # If we only scored a subset of GWs, anti_total in the new rows needs
        # to be built on top of the previous Supabase anti_total.
        if scored and not (args.force_full or tid in new_team_ids):
            min_scored_gw = min(g["gw"] for g in scored)
            if min_scored_gw > 1:
                # Pull the anti_total from GW (min_scored_gw - 1) in Supabase
                prev = (sb.from_("gw_scores")
                          .select("anti_total")
                          .eq("season", SEASON)
                          .eq("team_id", tid)
                          .eq("gw", min_scored_gw - 1)
                          .limit(1)
                          .execute().data or [])
                prev_total = prev[0]["anti_total"] if prev else 0
            else:
                prev_total = 0

            # Recompute anti_total cumulatively from scored slice
            running = prev_total
            for g in sorted(scored, key=lambda r: r["gw"]):
                running += g.get("anti_gw_pts", 0) or 0
                g["anti_total"] = running

        # Build rows
        team_rows.append(build_team_row(tid, scored, SEASON, has_gw1))
        all_score_rows.extend(build_gw_score_rows(tid, scored, SEASON, live_gw))
        for gw, picks_data in picks_cache.items():
            if gw not in team_gws_to_score:
                continue
            is_live = gw == live_gw
            sel = build_selection_row(tid, gw, SEASON, picks_data, is_live)
            if sel:
                selection_rows.append(sel)

        time.sleep(0.4)

    # ── Persist ───────────────────────────────────────────────────────────────
    log.info("Upserting teams (%d rows)...", len(team_rows))
    upsert_in_batches(sb, "teams", team_rows, "team_id,season")

    log.info("Upserting gw_scores (%d rows)...", len(all_score_rows))
    upsert_in_batches(sb, "gw_scores", all_score_rows, "season,team_id,gw")

    log.info("Upserting team_gw_selections (%d rows)...", len(selection_rows))
    upsert_in_batches(sb, "team_gw_selections", selection_rows, "season,team_id,gw")

    # ── Update gw_rank only for GWs we processed ──────────────────────────────
    log.info("Updating per-GW ranks for processed GWs...")
    for gw in sorted(gws_to_process):
        update_ranks_for_gw(sb, SEASON, gw)

    # ── Update cumulative standings for the latest processed GW ──────────────
    # Re-rank teams by anti_total at the most recent GW we touched
    if gws_to_process:
        latest = max(gws_to_process)
        rows = (sb.from_("gw_scores")
                  .select("id,team_id,anti_total")
                  .eq("season", SEASON)
                  .eq("gw", latest)
                  .execute().data or [])
        rows.sort(key=lambda r: r.get("anti_total") if r.get("anti_total") is not None else 99999)
        updates = []
        for pos, r in enumerate(rows, 1):
            updates.append({
                "id":                  r["id"],
                "season":              SEASON,
                "team_id":             r["team_id"],
                "gw":                  latest,
                "cumulative_standing": pos,
            })
        if updates:
            sb.from_("gw_scores").upsert(updates, on_conflict="season,team_id,gw").execute()
        log.info("  GW%d: cumulative standings updated for %d teams", latest, len(updates))

    elapsed = time.time() - t0
    log.info("=" * 60)
    log.info("Migration complete in %.1f seconds.", elapsed)
    log.info("  Season:           %s", SEASON)
    log.info("  GWs processed:    %s", sorted(gws_to_process))
    log.info("  Teams touched:    %d", len(team_rows))
    log.info("  Score rows:       %d", len(all_score_rows))
    log.info("  Selection rows:   %d", len(selection_rows))
    log.info("  New teams:        %d", len(new_team_ids))
    log.info("=" * 60)


if __name__ == "__main__":
    main()