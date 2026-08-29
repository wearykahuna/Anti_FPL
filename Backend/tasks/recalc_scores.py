"""
tasks/recalc_scores.py — Recompute team scores from Supabase data.
====================================================================
Self-gating: only runs when the live window is open. Reads everything from
Supabase (zero FPL API calls), recomputes anti scores for the live GW, and
upserts the results back to gw_scores.

Smart filter: only recalcs teams whose squad includes at least one player
whose club has an active fixture this GW.

API calls per run: 0 (pure DB + scoring).
Chips history per team is read from teams.chips_history (kept fresh by
refresh_picks each GW).
Player type (GK/DEF/MID/FWD) is read from the players table (kept fresh
by refresh_reference) — no bootstrap call needed.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db       import (
    DEFAULT_SEASON,
    get_client,
    get_fixtures,
    get_gw_scores,
    get_player_scores,
    get_players_ref,
    get_selections,
    get_teams,
    is_live_window_open,
    upsert,
)
from scoring  import calc_fpl_raw, score_one_gw_for_team

log = logging.getLogger(__name__)

SEASON = DEFAULT_SEASON

_POS_TO_TYPE = {"GKP": 1, "DEF": 2, "MID": 3, "FWD": 4}


# ── Smart filter helpers ──────────────────────────────────────────────────────

def get_active_club_ids(season: str, gw: int, fixtures: list[dict] | None = None) -> set[int]:
    """Clubs that have started or finished a fixture this GW."""
    if fixtures is None:
        fixtures = get_fixtures(season, gw)
    active = set()
    for f in fixtures:
        if f.get("started") or f.get("finished") or f.get("finished_provisional"):
            active.add(f["team_h"])
            active.add(f["team_a"])
    return active


def teams_with_active_players(
    selections:     list[dict],
    player_to_club: dict[int, int],
    active_clubs:   set[int],
) -> set[int]:
    """Team IDs whose squad has any player in an active fixture."""
    active_teams = set()
    for sel in selections:
        for pid in sel.get("squad", []) or []:
            if player_to_club.get(pid) in active_clubs:
                active_teams.add(sel["team_id"])
                break
    return active_teams


# ── Score one team for the live GW ────────────────────────────────────────────

def score_team_for_gw(
    team_id:          int,
    gw:               int,
    selection:        dict,
    mins_map:         dict[int, int],
    pts_map:          dict[int, int],
    player_type:      dict[int, int],
    finished_players: set[int],
    chips_history:    list[dict],
    hist_gw_row:      dict,
    gw_finished:      bool,
    prev_anti_total:  int = 0,
) -> dict | None:
    """Score one team for one GW using already-fetched data."""
    squad = selection.get("squad") or []
    if not squad:
        return None
    captain_id  = selection.get("captain_id")
    vice_id     = selection.get("vice_captain_id")
    active_chip = selection.get("active_chip") or ""
    cap_mult    = 3 if active_chip == "3xc" else 2

    picks_data = {
        "active_chip": active_chip,
        "picks": [
            {
                "element":         pid,
                "position":        i + 1,
                "is_captain":      pid == captain_id,
                "is_vice_captain": pid == vice_id,
                "multiplier":      cap_mult if pid == captain_id else 1,
            }
            for i, pid in enumerate(squad)
        ],
    }

    return score_one_gw_for_team(
        team_id             = team_id,
        gw                  = gw,
        hist_gw             = hist_gw_row,
        picks_data          = picks_data,
        mins                = mins_map,
        pts                 = pts_map,
        player_type         = player_type,
        chips               = chips_history,
        previous_anti_total = prev_anti_total,
        gw_finished         = gw_finished,
        finished_players    = finished_players if not gw_finished else None,
    )


# ── Row formatter ─────────────────────────────────────────────────────────────

def to_gw_scores_row(
    team_id: int, gw: int, season: str, scored: dict,
    is_live: bool, is_provisional: bool = False,
) -> dict:
    return {
        "season":           season,
        "team_id":          team_id,
        "gw":               gw,
        "fpl_raw_pts":      scored.get("fpl_raw_pts"),
        "fpl_xfer_cost":    scored.get("fpl_xfer_cost"),
        "transfers_gw":     scored.get("transfers_gw", 0) or 0,
        "fpl_gw_rank":      scored.get("fpl_gw_rank"),
        "fpl_total":        scored.get("fpl_total"),
        "active_chip":      scored.get("active_chip") or "",
        "hit_pts":          scored.get("hit_pts", 0) or 0,
        "inactive_count":   scored.get("inactive_count"),
        "inactive_pen_pts": scored.get("inactive_pen_pts", 0) or 0,
        "bank_pen":         scored.get("bank_pen", False) or False,
        "bank_pen_pts":     scored.get("bank_pen_pts", 0) or 0,
        "cvc_pen_pts":      scored.get("cvc_pen_pts", 0) or 0,
        "chip_pen_pts":     scored.get("chip_pen_pts", 0) or 0,
        "unused_chips":     scored.get("unused_chips", []) or [],
        "total_pens_gw":    scored.get("total_pens_gw", 0) or 0,
        "anti_gw_pts":      scored.get("anti_gw_pts", 0) or 0,
        "anti_total":       scored.get("anti_total", 0) or 0,
        "captain_element":  scored.get("captain_element"),
        "captain_mult":     scored.get("captain_mult", 1) or 1,
        "captain_pts":      scored.get("captain_pts"),
        "vice_element":     scored.get("vice_element"),
        "vice_pts":         scored.get("vice_pts"),
        "is_live":          is_live,
        "is_provisional":   is_provisional,
    }


# ── Re-rank helpers ───────────────────────────────────────────────────────────

def update_rankings_for_gw(season: str, gw: int) -> None:
    """
    Compute gw_rank and cumulative_standing for every scored team in one pass
    and write them back with a single batched upsert (2 round trips total,
    instead of 2 per team).

    gw_rank tiebreak chain (all lower = better, mirrors the cup engine):
      anti_gw_pts → captain_pts → vice_pts → total_pens_gw → team_id.
    cumulative_standing tiebreak: anti_total → anti_gw_pts → team_id.
    """
    sb   = get_client()
    rows = (sb.from_("gw_scores")
              .select("id,team_id,anti_gw_pts,anti_total,captain_pts,vice_pts,total_pens_gw")
              .eq("season", season)
              .eq("gw", gw)
              .execute().data or [])
    rows = [r for r in rows if r.get("anti_gw_pts") is not None]
    if not rows:
        return

    z = lambda v: 0 if v is None else v

    gw_order = sorted(rows, key=lambda r: (
        z(r["anti_gw_pts"]), z(r["captain_pts"]), z(r["vice_pts"]),
        z(r["total_pens_gw"]), r["team_id"],
    ))
    total_order = sorted(rows, key=lambda r: (
        z(r["anti_total"]), z(r["anti_gw_pts"]), r["team_id"],
    ))

    # Include the fields we already fetched (not just gw_rank/cumulative_standing)
    # so that IF the on_conflict match ever misses a row that was deleted/moved
    # between the SELECT above and this upsert — a rare race, not the norm —
    # the fallback INSERT still has real values instead of tripping a NOT NULL
    # constraint on a column this function was never trying to touch.
    updates = {
        r["id"]: {
            "season": season, "team_id": r["team_id"], "gw": gw,
            "anti_gw_pts": r["anti_gw_pts"], "anti_total": z(r["anti_total"]),
            "captain_pts": z(r["captain_pts"]), "vice_pts": z(r["vice_pts"]),
            "total_pens_gw": z(r["total_pens_gw"]),
        }
        for r in rows
    }
    for rank, r in enumerate(gw_order, 1):
        updates[r["id"]]["gw_rank"] = rank
    for pos, r in enumerate(total_order, 1):
        updates[r["id"]]["cumulative_standing"] = pos

    upsert("gw_scores", list(updates.values()), on_conflict="season,team_id,gw")


# ── Entry point ───────────────────────────────────────────────────────────────

def run(provisional: bool = False) -> int:
    log.info("Recalc scores — %s%s",
             datetime.now().strftime("%Y-%m-%d %H:%M"),
             " [provisional]" if provisional else "")

    is_open, current_gw = is_live_window_open(SEASON)

    if not is_open:
        if not provisional:
            log.info("Live window closed — exiting (no-op).")
            return 0
        # Provisional mode: verify the provisional window is actually open
        from db import is_provisional_window_open
        prov_open, current_gw = is_provisional_window_open(SEASON)
        if not prov_open:
            log.info("Provisional window closed — exiting.")
            return 0
        log.info("Provisional window OPEN for GW%d", current_gw)
    else:
        log.info("Live window OPEN for GW%d", current_gw)

    # Players ref — provides both player→club mapping and player_type.
    # players table is kept current by refresh_reference; no API call needed.
    players_ref    = get_players_ref(SEASON)
    player_to_club = {p["player_id"]: p.get("team_id")                       for p in players_ref}
    player_type    = {p["player_id"]: _POS_TO_TYPE.get(p.get("position"), 3) for p in players_ref}

    # Selections + teams (with chips_history)
    selections = get_selections(SEASON, gw=current_gw)
    if not selections:
        log.info("No selections for GW%d — has refresh_picks run yet?", current_gw)
        return 0
    sel_by_team = {s["team_id"]: s for s in selections}

    teams     = get_teams(SEASON)
    team_meta = {t["team_id"]: t for t in teams}

    # Fetched once and reused below for both the active-club filter and the
    # finished-players calc — this table doesn't change within a single tick.
    fixtures = get_fixtures(SEASON, gw=current_gw)

    # Active fixture filter — in provisional mode all fixtures are done so all teams count
    if provisional:
        active_teams = {s["team_id"] for s in selections}
        log.info("Teams to recalc (provisional): %d", len(active_teams))
    else:
        active_clubs = get_active_club_ids(SEASON, current_gw, fixtures=fixtures)
        if not active_clubs:
            log.info("No active fixtures yet for GW%d — exiting.", current_gw)
            return 0
        active_teams = teams_with_active_players(selections, player_to_club, active_clubs)
        log.info("Teams to recalc: %d (of %d total)", len(active_teams), len(selections))

    # Player stats from DB (refresh_live keeps these current)
    player_scores = get_player_scores(SEASON, gw=current_gw)
    mins_map      = {p["player_id"]: p.get("minutes", 0)  for p in player_scores}
    pts_map       = {p["player_id"]: p.get("base_pts", 0) for p in player_scores}

    # Players whose fixture is finished (for auto-sub inference)
    finished_fixture_ids = {f["fixture_id"] for f in fixtures
                            if f.get("finished") or f.get("finished_provisional")}
    finished_clubs       = (
        {f["team_h"] for f in fixtures if f["fixture_id"] in finished_fixture_ids} |
        {f["team_a"] for f in fixtures if f["fixture_id"] in finished_fixture_ids}
    )
    finished_players = {pid for pid, club in player_to_club.items() if club in finished_clubs}

    # Existing current-GW scores (bank, transfer cost, FPL rank — static within GW)
    sb = get_client()
    existing_rows = (sb.from_("gw_scores")
                       .select("team_id,fpl_raw_pts,fpl_xfer_cost,transfers_gw,fpl_total,fpl_gw_rank,bank")
                       .eq("season", SEASON)
                       .eq("gw", current_gw)
                       .execute().data or [])
    existing_by_team = {r["team_id"]: r for r in existing_rows}

    # Previous-GW anti_totals — one batch query instead of N per-team calls
    prev_gw_rows = get_gw_scores(SEASON, gw=current_gw - 1) if current_gw > 1 else []
    prev_totals  = {r["team_id"]: r.get("anti_total", 0) for r in prev_gw_rows}

    # ── Score each active team ────────────────────────────────────────────────
    scored_rows: list[dict] = []
    for tid in active_teams:
        sel = sel_by_team.get(tid)
        if not sel:
            continue

        team_info     = team_meta.get(tid, {})
        chips_history = team_info.get("chips_history") or []

        prev_row    = existing_by_team.get(tid, {})
        squad       = sel.get("squad") or []
        captain_id  = sel.get("captain_id")
        vice_id     = sel.get("vice_captain_id")
        active_chip = sel.get("active_chip") or ""

        # Team-wide resolution is only used for the is_provisional flag below —
        # a team's own squad can finish well before the rest of the gameweek
        # does (e.g. no Monday-night player), at which point its score is as
        # final as a provisional-pass score would be.
        team_fully_resolved = bool(squad) and all(pid in finished_players for pid in squad)

        # Pass the REAL finished_players set (not provisional mode's "treat
        # everyone as done") so auto-subs and VC promotion apply the moment
        # each one is individually locked in — a bench player who's already
        # played can replace a confirmed-blank starter immediately, without
        # waiting for the rest of either player's teammates to finish.
        fpl_raw = calc_fpl_raw(squad, captain_id, vice_id, active_chip,
                               pts_map, mins_map, player_type,
                               finished_players=None if provisional else finished_players)

        hist_gw_row = {
            "event":                current_gw,
            "points":               fpl_raw,
            "event_transfers_cost": prev_row.get("fpl_xfer_cost", 0) or 0,
            "event_transfers":      prev_row.get("transfers_gw", 0) or 0,
            "bank":                 prev_row.get("bank", 0) or 0,
            "rank":                 prev_row.get("fpl_gw_rank"),
            "total_points":         prev_row.get("fpl_total"),
        }

        scored = score_team_for_gw(
            team_id          = tid,
            gw               = current_gw,
            selection        = sel,
            mins_map         = mins_map,
            pts_map          = pts_map,
            player_type      = player_type,
            finished_players = finished_players,
            chips_history    = chips_history,
            hist_gw_row      = hist_gw_row,
            gw_finished      = False,
            prev_anti_total  = prev_totals.get(tid, 0),
        )
        if scored:
            scored_rows.append(to_gw_scores_row(
                tid, current_gw, SEASON, scored,
                is_live=True, is_provisional=provisional or team_fully_resolved,
            ))

    log.info("Upserting %d gw_scores rows...", len(scored_rows))
    upsert("gw_scores", scored_rows, on_conflict="season,team_id,gw")

    # Rankings are derived from the scores just written above, which are the
    # part that actually matters — don't let a rare write race here (or
    # anything else about this secondary pass) take down the whole run and
    # any live ticks still due after it. It self-heals next tick regardless.
    try:
        update_rankings_for_gw(SEASON, current_gw)
    except Exception:
        log.exception("update_rankings_for_gw failed — scores were saved, rankings will retry next tick.")
        log.info("Recalc scores complete (with ranking-update error).")
        return 1

    log.info("Recalc scores complete.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )
    sys.exit(run())
