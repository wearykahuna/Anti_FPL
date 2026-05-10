"""
Anti-FPL Scoring Engine  —  Phase 1a
======================================
Fully independent of Joey's site. All data from FPL API only.
Scoring rules hardcoded per spec.

RULES:
  - Inactive player penalty : +9 pts per 0-min player in final XI (post auto-subs)
                               During Bench Boost: applies to all 15 players
  - Bank penalty             : +25 pts if bank > £3.0m (stored in 0.1m units, so > 30)
  - Hits                     : +4 pts per extra transfer (adds to score)
  - C/VC penalty             : +15 pts if BOTH captain AND vice-captain play 0 mins
                               (VC auto-sub logic applies first, as per normal FPL)
  - Chip penalty             : +25 pts per unused chip at GW19 (first half chips)
                               and GW38 (second half chips). Wildcard exempt.
                               Each chip is issued twice — split by GW≤19 / GW≥20.
  - Bench Boost              : Inactive penalty applies to all 15 players
  - Standings                : Lowest score wins

CUP:
  - Knockout, single GW per round, lowest anti score wins
  - Always 10 rounds, always starts GW29, top 1024 by standing qualify
  - Tiebreaker: lowest captain pts → lowest VC pts → lowest GW pens → league pos
"""

import time
import logging
from typing import Optional

import requests

log = logging.getLogger(__name__)

FPL_BASE = "https://fantasy.premierleague.com/api"

# ── Penalty constants ─────────────────────────────────────────────────────────
INACTIVE_PEN       = 9
BANK_PEN           = 25
BANK_THRESHOLD     = 30    # units of £0.1m  →  £3.0m
HIT_COST           = 4    # per extra transfer
CVC_PEN            = 15
UNUSED_CHIP_PEN    = 25

# Chips that must be used each half (wildcard exempt)
# API returns these names in the chips list
CHIPS_REQUIRED     = {"bboost", "3xc", "freehit"}

# GW boundaries
FIRST_HALF_END     = 19
SECOND_HALF_START  = 20
LAST_GW            = 38

# Cup constants
CUP_ROUNDS         = 10
CUP_START_GW       = LAST_GW - CUP_ROUNDS + 1   # = GW29
CUP_MAX_ENTRANTS   = 2 ** CUP_ROUNDS             # = 1024


# ── HTTP ──────────────────────────────────────────────────────────────────────

_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})


def _get_json(url: str, retries: int = 3) -> Optional[dict]:
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("Attempt %d/%d  %s  [%s]", attempt + 1, retries, url, exc)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


# ── FPL API helpers ───────────────────────────────────────────────────────────

def fetch_bootstrap() -> Optional[dict]:
    return _get_json(f"{FPL_BASE}/bootstrap-static/")

def fetch_team_info(team_id: int) -> Optional[dict]:
    return _get_json(f"{FPL_BASE}/entry/{team_id}/")

def fetch_team_history(team_id: int) -> Optional[dict]:
    return _get_json(f"{FPL_BASE}/entry/{team_id}/history/")

def fetch_picks(team_id: int, gw: int) -> Optional[dict]:
    return _get_json(f"{FPL_BASE}/entry/{team_id}/event/{gw}/picks/")

def fetch_live(gw: int) -> Optional[dict]:
    return _get_json(f"{FPL_BASE}/event/{gw}/live/")

def fetch_league_page(league_id: int, page: int = 1) -> Optional[dict]:
    return _get_json(
        f"{FPL_BASE}/leagues-classic/{league_id}/standings/"
        f"?page_standings={page}"
    )


def get_all_team_ids_from_league(league_id: int) -> list[int]:
    """Pull every team ID from an FPL classic mini-league (handles pagination)."""
    ids, page = [], 1
    while True:
        data = fetch_league_page(league_id, page)
        if not data:
            break
        standings = data.get("standings", {})
        results   = standings.get("results", [])
        if not results:
            break
        ids.extend(r["entry"] for r in results)
        if not standings.get("has_next"):
            break
        page += 1
        time.sleep(0.3)
    log.info("League %d: found %d teams across %d pages", league_id, len(ids), page)
    return ids


def current_gw(bootstrap: dict) -> int:
    finished = [e for e in bootstrap.get("events", []) if e.get("finished")]
    return max((e["id"] for e in finished), default=1) if finished else 1


def player_minutes(live_data: dict) -> dict[int, int]:
    return {
        e["id"]: e["stats"].get("minutes", 0)
        for e in live_data.get("elements", [])
    }


# ── Chip half-season helpers ──────────────────────────────────────────────────

def split_chips_by_half(chips: list[dict]) -> tuple[set[str], set[str]]:
    """
    Given the chips list from entry/{id}/history/, return two sets:
      first_half  — chip API names used in GW1–19
      second_half — chip API names used in GW20–38
    Each chip can appear twice (once per half). WC can appear in either half
    but is exempt from penalties regardless.
    """
    first, second = set(), set()
    for c in chips:
        name = c.get("name", "").lower()
        gw   = c.get("event", 0)
        if gw <= FIRST_HALF_END:
            first.add(name)
        else:
            second.add(name)
    return first, second


def unused_chip_penalty(chips_used_in_half: set[str]) -> tuple[int, set[str]]:
    """
    Returns (penalty_pts, set_of_unused_chip_names) for one half of the season.
    Wildcard always exempt.
    """
    unused = CHIPS_REQUIRED - chips_used_in_half
    return len(unused) * UNUSED_CHIP_PEN, unused


# ── GW scorer ─────────────────────────────────────────────────────────────────

def score_gw(
    team_id:    int,
    gw:         int,
    hist_gw:    dict,
    picks_data: dict,
    mins:       dict[int, int],
    first_half_chips:  set[str],
    second_half_chips: set[str],
) -> dict:
    """
    Compute Anti-FPL score for one team for one GW.
    Chip penalties applied at GW19 (first half) and GW38 (second half).
    Returns full scoring breakdown dict.
    """
    picks       = picks_data.get("picks", [])
    auto_subs   = picks_data.get("automatic_subs", [])
    active_chip = (picks_data.get("active_chip") or "").lower()
    bench_boost = active_chip == "bboost"

    # Final XI / bench after auto-subs (API positions are post-sub)
    starters       = [p for p in picks if p["position"] <= 11]
    bench          = [p for p in picks if p["position"] >  11]
    active_players = picks if bench_boost else starters

    # ── Inactive player penalty ───────────────────────────────────────────────
    inactive         = [p for p in active_players if mins.get(p["element"], 0) == 0]
    inactive_pen_pts = len(inactive) * INACTIVE_PEN

    # ── Bank penalty ──────────────────────────────────────────────────────────
    bank         = hist_gw.get("bank", 0) or 0
    bank_pen     = bank > BANK_THRESHOLD
    bank_pen_pts = BANK_PEN if bank_pen else 0

    # ── Hits ──────────────────────────────────────────────────────────────────
    xfer_cost = hist_gw.get("event_transfers_cost", 0) or 0  # negative in FPL API
    hit_pts   = abs(xfer_cost)

    # ── C/VC penalty ─────────────────────────────────────────────────────────
    captain = next((p for p in picks if p.get("is_captain")),     None)
    vice    = next((p for p in picks if p.get("is_vice_captain")), None)

    cap_mins  = mins.get(captain["element"], 0) if captain else 0
    vice_mins = mins.get(vice["element"],    0) if vice    else 0
    cvc_pen_pts = CVC_PEN if (cap_mins == 0 and vice_mins == 0) else 0

    # Captain points for tiebreaker (boosted: 2x normal, 3x if TC active)
    cap_raw_pts = 0
    if captain:
        # mins_map doesn't have points; caller should pass pts_map separately
        # for now store element id — caller merges actual pts later
        pass

    # Captain multiplier for tiebreaker
    cap_multiplier = captain.get("multiplier", 1) if captain else 1

    # ── Chip penalties (GW19 and GW38 only) ──────────────────────────────────
    chip_pen_pts   = 0
    unused_chips   = set()
    if gw == FIRST_HALF_END:
        chip_pen_pts, unused_chips = unused_chip_penalty(first_half_chips)
        if unused_chips:
            log.info("  GW19 chip penalty team=%d unused=%s pts=%d",
                     team_id, unused_chips, chip_pen_pts)
    elif gw == LAST_GW:
        chip_pen_pts, unused_chips = unused_chip_penalty(second_half_chips)
        if unused_chips:
            log.info("  GW38 chip penalty team=%d unused=%s pts=%d",
                     team_id, unused_chips, chip_pen_pts)

    # ── Anti-FPL GW score ─────────────────────────────────────────────────────
    fpl_raw   = hist_gw.get("points", 0) or 0
    anti_gw   = fpl_raw + hit_pts + inactive_pen_pts + bank_pen_pts + cvc_pen_pts + chip_pen_pts

    # Total penalties this GW (used as cup tiebreaker)
    total_pens_gw = hit_pts + inactive_pen_pts + bank_pen_pts + cvc_pen_pts + chip_pen_pts

    return {
        "gw":               gw,
        "active_chip":      active_chip,
        # FPL base
        "fpl_raw_pts":      fpl_raw,
        "fpl_xfer_cost":    xfer_cost,
        "fpl_gw_rank":      hist_gw.get("rank"),
        "fpl_total":        hist_gw.get("total_points"),
        # Anti penalties breakdown
        "hit_pts":          hit_pts,
        "inactive_count":   len(inactive),
        "inactive_pen_pts": inactive_pen_pts,
        "bank":             bank,
        "bank_pen":         bank_pen,
        "bank_pen_pts":     bank_pen_pts,
        "cvc_pen_pts":      cvc_pen_pts,
        "chip_pen_pts":     chip_pen_pts,
        "unused_chips":     list(unused_chips),
        "total_pens_gw":    total_pens_gw,
        # Anti score
        "anti_gw_pts":      anti_gw,
        "anti_total":       None,   # filled by score_team_season
        "standing":         None,   # filled by score_league
        # Cup tiebreaker fields (captain/VC pts filled by caller with live pts)
        "captain_element":  captain["element"] if captain else None,
        "captain_mult":     cap_multiplier,
        "vice_element":     vice["element"] if vice else None,
        "captain_pts":      None,   # boosted pts — filled in score_team_season
        "vice_pts":         None,
    }


# ── Team scorer ───────────────────────────────────────────────────────────────

def score_team_season(
    team_id:     int,
    history:     dict,
    live_cache:  dict[int, dict[int, int]],   # gw → {player_id: minutes}
    pts_cache:   dict[int, dict[int, int]],   # gw → {player_id: total_points}
    picks_cache: dict[int, dict],
    last_gw:     int,
) -> list[dict]:
    chips = history.get("chips", [])
    first_half_chips, second_half_chips = split_chips_by_half(chips)
    gw_rows     = {g["event"]: g for g in history.get("current", [])}
    scored_gws  = []
    running     = 0

    for gw in sorted(gw_rows):
        picks_data = picks_cache.get(gw)
        if not picks_data:
            log.warning("  No picks for team=%d GW=%d — skipping", team_id, gw)
            continue

        mins = live_cache.get(gw, {})
        pts  = pts_cache.get(gw, {})

        result = score_gw(
            team_id           = team_id,
            gw                = gw,
            hist_gw           = gw_rows[gw],
            picks_data        = picks_data,
            mins              = mins,
            first_half_chips  = first_half_chips,
            second_half_chips = second_half_chips,
        )

        # Fill captain / VC boosted pts for cup tiebreaker
        cap_el  = result["captain_element"]
        vice_el = result["vice_element"]
        mult    = result["captain_mult"]
        if cap_el and cap_el in pts:
            result["captain_pts"] = pts[cap_el] * mult
        if vice_el and vice_el in pts:
            # VC multiplier is 1 normally; if TC active cap_mult=3, VC stays 1
            result["vice_pts"] = pts.get(vice_el, 0)

        running += result["anti_gw_pts"]
        result["anti_total"] = running
        scored_gws.append(result)

    return scored_gws


# ── League scorer ─────────────────────────────────────────────────────────────

def score_league(team_ids: list[int], last_gw: int) -> list[dict]:
    """Score all teams. Returns list sorted lowest → highest (lowest wins)."""

    # Fetch live data once for all GWs — minutes AND points per player
    log.info("Fetching live data for GW1–%d...", last_gw)
    live_cache: dict[int, dict[int, int]] = {}
    pts_cache:  dict[int, dict[int, int]] = {}
    for gw in range(1, last_gw + 1):
        raw = fetch_live(gw)
        if raw:
            live_cache[gw] = {e["id"]: e["stats"].get("minutes", 0)     for e in raw.get("elements", [])}
            pts_cache[gw]  = {e["id"]: e["stats"].get("total_points", 0) for e in raw.get("elements", [])}
            log.info("  GW%d: %d players", gw, len(live_cache[gw]))
        time.sleep(0.3)

    results = []
    for i, tid in enumerate(team_ids, 1):
        log.info("[%d/%d] Scoring team %d...", i, len(team_ids), tid)

        info    = fetch_team_info(tid)
        history = fetch_team_history(tid)
        if not info or not history:
            log.warning("  Skipping team %d — fetch failed", tid)
            continue

        picks_cache: dict[int, dict] = {}
        for gw in sorted(g["event"] for g in history.get("current", [])):
            picks = fetch_picks(tid, gw)
            if picks:
                picks_cache[gw] = picks
            time.sleep(0.3)

        scored = score_team_season(
            team_id     = tid,
            history     = history,
            live_cache  = live_cache,
            pts_cache   = pts_cache,
            picks_cache = picks_cache,
            last_gw     = last_gw,
        )

        latest = scored[-1] if scored else {}
        results.append({
            "team_id":    tid,
            "manager":    f"{info.get('player_first_name','')} {info.get('player_last_name','')}".strip(),
            "team_name":  info.get("name", f"Team {tid}"),
            "anti_total": latest.get("anti_total", 0),
            "gws":        scored,
        })
        time.sleep(0.4)

    # Sort lowest → highest
    results.sort(key=lambda t: t["anti_total"] or 99999)
    for pos, team in enumerate(results, 1):
        team["standing"] = pos

    return results


# ── Standings printer ─────────────────────────────────────────────────────────

def print_standings(teams: list[dict]) -> None:
    sep = "-" * 70
    print()
    print(sep)
    print("  Anti-FPL Standings  (lowest score wins)")
    print(sep)
    print(f"  {'#':>3}  {'Manager':<22}  {'Team':<22}  {'Total':>6}")
    print(sep)
    for t in teams:
        print(f"  {t['standing']:>3}.  {t['manager']:<22}  {t['team_name']:<22}  {t['anti_total']:>6}")
    print(sep)
    print()
