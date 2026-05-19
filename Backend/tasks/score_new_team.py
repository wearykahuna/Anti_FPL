"""
tasks/score_new_team.py — Full backfill for one newly-registered team.
=========================================================================
On-demand task. When a new team registers, this:
  - Fetches their full FPL history
  - Fetches their picks for every GW played
  - Fetches live data for every GW (only those not already in player_gw_scores)
  - Scores them GW-by-GW from GW1 to current
  - Upserts teams, gw_scores, team_gw_selections, and player_gw_scores rows

Eligibility: team must have a valid GW1 row to be marked eligible.

Usage:
    python tasks/score_new_team.py --team 5388975
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fpl_api import (
    fetch_bootstrap,
    fetch_team_info,
    fetch_team_history,
    fetch_picks,
    fetch_live,
)
from db       import (
    DEFAULT_SEASON,
    get_client,
    upsert,
)
from scoring  import (
    INACTIVE_PEN,
    build_player_type_map,
    current_gw,
    detect_live_gw,
    score_team_season,
)

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON


# ── Helpers ───────────────────────────────────────────────────────────────────

def existing_live_gws(season: str) -> set[int]:
    """GW numbers we already have player_gw_scores for (any player row)."""
    sb   = get_client()
    rows = (sb.from_("player_gw_scores")
              .select("gw")
              .eq("season", season)
              .execute().data or [])
    return {r["gw"] for r in rows}


def build_player_gw_rows(gw: int, season: str, live_data: dict, is_live: bool) -> list[dict]:
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


def build_selection_row(team_id: int, gw: int, season: str,
                        picks_data: dict, is_live: bool) -> dict | None:
    picks   = picks_data.get("picks", [])
    captain = next((p["element"] for p in picks if p.get("is_captain")),      None)
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


def to_gw_scores_row(team_id: int, season: str, scored: dict, live_gw: int | None) -> dict:
    return {
        "season":              season,
        "team_id":             team_id,
        "gw":                  scored["gw"],
        "fpl_raw_pts":         scored.get("fpl_raw_pts"),
        "fpl_xfer_cost":       scored.get("fpl_xfer_cost"),
        "fpl_gw_rank":         scored.get("fpl_gw_rank"),
        "fpl_total":           scored.get("fpl_total"),
        "active_chip":         scored.get("active_chip") or "",
        "hit_pts":             scored.get("hit_pts", 0) or 0,
        "inactive_count":      scored.get("inactive_count"),
        "inactive_pen_pts":    scored.get("inactive_pen_pts", 0) or 0,
        "bank":                scored.get("bank"),
        "bank_pen":            scored.get("bank_pen", False) or False,
        "bank_pen_pts":        scored.get("bank_pen_pts", 0) or 0,
        "cvc_pen_pts":         scored.get("cvc_pen_pts", 0) or 0,
        "chip_pen_pts":        scored.get("chip_pen_pts", 0) or 0,
        "unused_chips":        scored.get("unused_chips", []) or [],
        "total_pens_gw":       scored.get("total_pens_gw", 0) or 0,
        "anti_gw_pts":         scored.get("anti_gw_pts", 0) or 0,
        "anti_total":          scored.get("anti_total", 0) or 0,
        "captain_element":     scored.get("captain_element"),
        "captain_mult":        scored.get("captain_mult", 1) or 1,
        "captain_pts":         scored.get("captain_pts"),
        "vice_element":        scored.get("vice_element"),
        "vice_pts":            scored.get("vice_pts"),
        "is_live":             scored["gw"] == live_gw,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

def run(team_id: int) -> int:
    log.info("=" * 60)
    log.info("Score new team %d — %s", team_id, datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 60)

    # ── Team info + history ──────────────────────────────────────────────────
    info = fetch_team_info(team_id)
    if not info:
        log.error("Team info fetch failed for %d", team_id)
        return 1

    history = fetch_team_history(team_id)
    if not history:
        log.error("History fetch failed for %d", team_id)
        return 1

    gw_rows = {g["event"]: g for g in history.get("current", [])}
    has_gw1 = 1 in gw_rows
    if not has_gw1:
        log.warning("Team %d has no GW1 entry — will be marked INELIGIBLE", team_id)

    # ── Bootstrap → last_gw, live_gw, player_type ────────────────────────────
    bootstrap = fetch_bootstrap()
    if not bootstrap:
        log.error("Bootstrap fetch failed.")
        return 1
    last_gw, live_gw = detect_live_gw(bootstrap)
    player_type      = build_player_type_map(bootstrap)
    log.info("Last GW: %d, Live GW: %s", last_gw, live_gw)

    # ── Fetch picks per GW ───────────────────────────────────────────────────
    picks_cache: dict[int, dict] = {}
    for gw in sorted(gw_rows):
        picks = fetch_picks(team_id, gw)
        if picks:
            picks_cache[gw] = picks
        time.sleep(0.3)

    # ── Fetch live data per GW (only those we don't already have in DB) ──────
    already_have = existing_live_gws(SEASON)
    live_cache: dict[int, dict[int, int]] = {}
    pts_cache:  dict[int, dict[int, int]] = {}
    new_player_rows: list[dict] = []

    for gw in sorted(gw_rows):
        # Always need cache populated for scoring; but only upsert rows if missing
        raw = fetch_live(gw)
        if not raw:
            continue
        live_cache[gw] = {e["id"]: e["stats"].get("minutes", 0)      for e in raw.get("elements", [])}
        pts_cache[gw]  = {e["id"]: e["stats"].get("total_points", 0) for e in raw.get("elements", [])}
        if gw not in already_have:
            new_player_rows.extend(build_player_gw_rows(gw, SEASON, raw, is_live=(gw == live_gw)))
        time.sleep(0.3)

    if new_player_rows:
        log.info("Backfilling %d new player_gw_score rows...", len(new_player_rows))
        upsert("player_gw_scores", new_player_rows, on_conflict="season,player_id,gw")

    # ── Score the team ───────────────────────────────────────────────────────
    scored = score_team_season(
        team_id     = team_id,
        history     = history,
        live_cache  = live_cache,
        pts_cache   = pts_cache,
        picks_cache = picks_cache,
        last_gw     = last_gw,
        player_type = player_type,
        live_gw     = live_gw,
    )

    # ── Build and upsert all rows for this team ──────────────────────────────
    team_row = {
        "team_id":        team_id,
        "season":         SEASON,
        "manager":        f"{info.get('player_first_name','')} {info.get('player_last_name','')}".strip(),
        "team_name":      info.get("name", f"Team {team_id}"),
        "fpl_joined_at":  info.get("joined_time"),
        "anti_joined_at": datetime.now(timezone.utc).isoformat(),
        "eligible":       has_gw1,
        "chips_history":  history.get("chips", []),
    }

    score_rows = [to_gw_scores_row(team_id, SEASON, s, live_gw) for s in scored]

    selection_rows = []
    for gw, picks_data in picks_cache.items():
        sel = build_selection_row(team_id, gw, SEASON, picks_data, is_live=(gw == live_gw))
        if sel:
            selection_rows.append(sel)

    log.info("Upserting team + %d gw_scores + %d selections...",
             len(score_rows), len(selection_rows))

    upsert("teams",              [team_row],       on_conflict="team_id,season")
    upsert("gw_scores",          score_rows,       on_conflict="season,team_id,gw")
    upsert("team_gw_selections", selection_rows,   on_conflict="season,team_id,gw")

    log.info("Score new team complete: %d (eligible=%s)", team_id, has_gw1)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--team", type=int, required=True, help="FPL team ID to score")
    args = parser.parse_args()
    return run(args.team)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(main())
