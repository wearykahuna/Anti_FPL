"""
scoring.py — Pure Anti-FPL scoring engine.
============================================
No I/O. No FPL API calls. No Supabase. Takes data in, returns scores out.

RULES:
  - Inactive player penalty : +9 pts per 0-min player in final XI (post auto-subs)
                               During Bench Boost: applies to all 15 players
  - Bank penalty             : +25 pts if bank > £3.0m (stored in 0.1m units, > 30)
  - Hits                     : +4 pts per extra transfer (adds to score)
  - C/VC penalty             : +15 pts if BOTH captain AND vice-captain play 0 mins
  - Chip penalty             : +25 pts per unused chip at GW19 (first-half) and
                               GW38 (second-half). Wildcard exempt.
                               (Each chip is issued twice — split by GW≤19 / GW≥20.)
  - Bench Boost              : Inactive penalty applies to all 15 players
  - Standings                : Lowest score wins

LIVE GW RULES:
  - Chip penalties never applied mid-GW.
  - Inactive / C-VC penalties only apply once fixtures are finished
    (inferred from finished_players set).
  - Inactive penalty is a conservative floor while live: a blank starter only
    counts once every bench player who could still be subbed in for them has
    themselves finished playing (see live_confirmed_inactive()). This means
    the live inactive count can only go up over a gameweek, never down.
  - Bank and hit penalties apply immediately (known at GW start).
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Penalty constants ─────────────────────────────────────────────────────────
INACTIVE_PEN     = 9
BANK_PEN         = 25
BANK_THRESHOLD   = 30   # units of £0.1m → £3.0m
HIT_COST         = 4    # +4 per extra transfer
CVC_PEN          = 15
UNUSED_CHIP_PEN  = 25

CHIPS_REQUIRED   = {"bboost", "3xc", "freehit"}   # WC is exempt

# GW boundaries
FIRST_HALF_END    = 19
SECOND_HALF_START = 20
LAST_GW           = 38

# Cup constants (used by cup.py, exposed here for convenience)
CUP_ROUNDS       = 10
CUP_START_GW     = LAST_GW - CUP_ROUNDS + 1     # 29
CUP_MAX_ENTRANTS = 2 ** CUP_ROUNDS               # 1024


# ── FPL raw score calculator (finished / provisional GWs) ────────────────────

def calc_fpl_raw(
    squad:            list[int],
    captain_id:       int | None,
    vice_id:          int | None,
    active_chip:      str,
    pts_map:          dict[int, int],
    mins_map:         dict[int, int],
    player_type:      dict[int, int],
    finished_players: Optional[set[int]] = None,
) -> int:
    """
    Compute fpl_raw from player scores with auto-subs and VC promotion.

    For bench-boost: all 15 count, no auto-subs — already safe to call live,
    since unplayed players simply contribute their current (possibly still 0)
    score and the total updates naturally as they play.

    VC promotion: once the captain's own fixture is finished and they played 0
    mins, VC gets the cap_mult bonus instead (incl. TC 3×). Gated on the
    captain's fixture being finished (not just currently 0 mins) so a captain
    who simply hasn't kicked off yet doesn't prematurely hand the multiplier
    to the vice-captain.

    finished_players: player IDs whose own fixture has finished. Defaults to
    the whole squad — correct for a finished/provisional GW, where every
    fixture is known to be done. Pass the REAL, gameweek-in-progress set to
    get a conservative, progressively-accurate live figure instead: a given
    auto-sub or VC promotion applies the moment it's individually locked in
    (both sides' fixtures done), without waiting for the rest of the squad
    to finish. Mirrors live_confirmed_inactive()'s philosophy — a sub or
    promotion applied here is never later reversed as more fixtures resolve,
    it can only be applied earlier or later depending on when it locks in.
    """
    cap_mult = 3 if active_chip == "3xc" else 2
    finished = finished_players if finished_players is not None else set(squad)

    if active_chip == "bboost":
        raw = sum(pts_map.get(pid, 0) for pid in squad)
        if captain_id in squad and mins_map.get(captain_id, 0) > 0:
            raw += pts_map.get(captain_id, 0) * (cap_mult - 1)
        elif captain_id in finished and vice_id in squad and mins_map.get(vice_id, 0) > 0:
            raw += pts_map.get(vice_id, 0) * (cap_mult - 1)
        return raw

    starters = [{"element": pid, "position": i + 1}  for i, pid in enumerate(squad[:11])]
    bench    = [{"element": pid, "position": i + 12} for i, pid in enumerate(squad[11:])]
    xi       = infer_autosubs(starters, bench, player_type, finished, mins_map)
    xi_ids   = [p["element"] for p in xi]
    raw      = sum(pts_map.get(pid, 0) for pid in xi_ids)

    if captain_id in xi_ids and mins_map.get(captain_id, 0) > 0:
        raw += pts_map.get(captain_id, 0) * (cap_mult - 1)
    elif captain_id in finished and vice_id in xi_ids and mins_map.get(vice_id, 0) > 0:
        raw += pts_map.get(vice_id, 0) * (cap_mult - 1)

    return raw


# ── Bootstrap inspection helpers ──────────────────────────────────────────────

def current_gw(bootstrap: dict) -> int:
    """Last fully finished GW from bootstrap."""
    finished = [e for e in bootstrap.get("events", []) if e.get("finished")]
    return max((e["id"] for e in finished), default=1) if finished else 1


def detect_live_gw(bootstrap: dict) -> tuple[int, Optional[int]]:
    """
    Returns (last_gw, live_gw).
      last_gw : highest GW to include in scoring. Equals live_gw if a GW is
                in progress, otherwise the last fully finished GW.
      live_gw : GW currently in progress (deadline passed, not yet finished).
                None when every played GW is fully finished.
    """
    events  = bootstrap.get("events", [])
    current = next((e for e in events if e.get("is_current")), None)
    if current and not current.get("finished"):
        return current["id"], current["id"]
    finished = [e["id"] for e in events if e.get("finished")]
    return (max(finished, default=1) if finished else 1), None


def build_player_type_map(bootstrap: dict) -> dict[int, int]:
    """Maps player_id → element_type (1=GK, 2=DEF, 3=MID, 4=FWD)."""
    return {e["id"]: e["element_type"] for e in bootstrap.get("elements", [])}


def player_minutes(live_data: dict) -> dict[int, int]:
    """Extract {player_id: minutes} from a /event/{gw}/live/ response."""
    return {
        e["id"]: e["stats"].get("minutes", 0)
        for e in live_data.get("elements", [])
    }


def players_in_finished_fixtures(live_raw: dict, fixtures: list[dict]) -> set[int]:
    """Return player IDs whose fixture is fully finished for the live GW."""
    finished_ids = {f["id"] for f in fixtures if f.get("finished")}
    result: set[int] = set()
    for elem in live_raw.get("elements", []):
        for exp in elem.get("explain", []):
            if exp.get("fixture") in finished_ids:
                result.add(elem["id"])
                break
    return result


# ── Chip half-season helpers ──────────────────────────────────────────────────

def split_chips_by_half(chips: list[dict]) -> tuple[set[str], set[str]]:
    """
    From the chips list in entry/{id}/history/, return:
      (chips_used_in_first_half, chips_used_in_second_half)
    Lower-cased chip API names. WC included in sets but exempt from penalties.
    """
    first: set[str] = set()
    second: set[str] = set()
    for c in chips:
        name = (c.get("name") or "").lower()
        gw   = c.get("event", 0)
        if gw <= FIRST_HALF_END:
            first.add(name)
        else:
            second.add(name)
    return first, second


def unused_chip_penalty(chips_used_in_half: set[str]) -> tuple[int, set[str]]:
    """Returns (penalty_pts, set_of_unused_chip_names) for one half."""
    unused = CHIPS_REQUIRED - chips_used_in_half
    return len(unused) * UNUSED_CHIP_PEN, unused


# ── Auto-sub inference (live GW only) ─────────────────────────────────────────

def _valid_formation(player_ids: list[int], player_type: dict[int, int]) -> bool:
    """True if XI satisfies minimum FPL formation rules."""
    t = [player_type.get(pid, 3) for pid in player_ids]
    return (
        t.count(1) == 1 and    # exactly 1 GK
        t.count(2) >= 3 and    # min 3 DEF
        t.count(3) >= 2 and    # min 2 MID
        t.count(4) >= 1         # min 1 FWD
    )


def infer_autosubs(
    starters:         list[dict],
    bench:            list[dict],
    player_type:      dict[int, int],
    finished_players: set[int],
    mins:             dict[int, int],
) -> list[dict]:
    """
    Apply FPL auto-substitution rules. Returns the effective XI after subs.

    Eligible to be subbed OUT : starter whose fixture is finished AND played 0.
    Eligible to come ON       : bench player whose fixture is finished AND played > 0.
    Bench priority            : ascending position (12 → 13 → 14 → 15).
    GK rule                   : GK ↔ GK only; outfield ↔ outfield only.
    Formation rule            : resulting XI must satisfy _valid_formation.

    For a finished GW, pass all squad player IDs as finished_players.
    For a live GW, pass only players whose fixtures are done.
    """
    xi = list(starters)
    for bench_p in sorted(bench, key=lambda p: p["position"]):
        bp_id = bench_p["element"]
        if bp_id not in finished_players or mins.get(bp_id, 0) == 0:
            continue
        bp_gk = player_type.get(bp_id, 3) == 1
        for i, starter in enumerate(xi):
            sp_id = starter["element"]
            if sp_id not in finished_players or mins.get(sp_id, 0) > 0:
                continue
            sp_gk = player_type.get(sp_id, 3) == 1
            if sp_gk != bp_gk:
                continue
            test = [p["element"] for p in xi]
            test[i] = bp_id
            if not _valid_formation(test, player_type):
                continue
            xi[i] = bench_p
            break    # bench player used; next bench candidate
    return xi


def live_confirmed_inactive(
    starters:         list[dict],
    bench:            list[dict],
    player_type:      dict[int, int],
    finished_players: set[int],
    mins:             dict[int, int],
) -> list[dict]:
    """
    Conservative, never-decreasing count of starters guaranteed to finish the
    gameweek inactive (0 mins), based only on fixtures that have finished so
    far.

    A blank starter (fixture finished, 0 mins, not auto-subbed out by
    infer_autosubs) is locked in as confirmed-inactive only once no outcome
    of the still-unfinished bench fixtures could ever rescue that slot.
    That's determined by re-running the auto-sub simulation in the most
    optimistic case — every bench player whose fixture hasn't finished yet
    is assumed to eventually play — and checking whether the same blank
    starter is still stuck in that slot even then. If even the best case
    can't save it (formation rules block any further swap into that slot,
    or every eligible bench player is already exhausted/blank), it's locked
    for real; some *other* unresolved bench fixture being same-type is not
    enough on its own, since formation limits may mean it could only ever
    rescue a different slot. This means the count this returns can only
    grow over the course of a gameweek, never shrink and later need
    correcting back down.
    """
    effective_xi = infer_autosubs(starters, bench, player_type, finished_players, mins)

    # Best case for every still-unresolved bench player: assume they finish
    # having played, and see which slots that could ever unstick.
    optimistic_finished = set(finished_players)
    optimistic_mins      = dict(mins)
    for b in bench:
        bid = b["element"]
        if bid not in optimistic_finished:
            optimistic_finished.add(bid)
            if optimistic_mins.get(bid, 0) == 0:
                optimistic_mins[bid] = 1
    optimistic_xi           = infer_autosubs(starters, bench, player_type, optimistic_finished, optimistic_mins)
    optimistic_by_position  = {p["position"]: p["element"] for p in optimistic_xi}

    starter_ids = {s["element"] for s in starters}
    locked = []
    for p in effective_xi:
        pid = p["element"]
        if pid not in starter_ids:
            continue                                    # a bench player already subbed in
        if pid not in finished_players or mins.get(pid, 0) > 0:
            continue                                     # not blank, or not finished yet
        if optimistic_by_position.get(p["position"]) == pid:
            locked.append(p)                             # unrescuable even in the best case

    return locked


# ── Core GW scorer ────────────────────────────────────────────────────────────

def score_gw(
    team_id:           int,
    gw:                int,
    hist_gw:           dict,
    picks_data:        dict,
    mins:              dict[int, int],
    pts:               dict[int, int],
    player_type:       dict[int, int],
    first_half_chips:  set[str],
    second_half_chips: set[str],
    gw_finished:       bool = True,
    finished_players:  Optional[set[int]] = None,
) -> dict:
    """
    Compute Anti-FPL score for one team for one GW.

    Finished GW : all penalties applied normally.
    Live GW (gw_finished=False):
      - fpl_raw = current FPL live total
      - Bank and hits always apply (known at GW start)
      - Auto-subs inferred from finished_players
      - Inactive penalty only for confirmed 0-min players post-auto-sub
      - C/VC penalty only when both fixtures are done AND both played 0
      - Chip penalties NEVER applied mid-GW
    """
    picks       = picks_data.get("picks", [])
    active_chip = (picks_data.get("active_chip") or "").lower()
    bench_boost = active_chip == "bboost"

    starters       = [p for p in picks if p["position"] <= 11]
    bench          = [p for p in picks if p["position"] >  11]
    active_players = picks if bench_boost else starters

    captain        = next((p for p in picks if p.get("is_captain")),     None)
    vice           = next((p for p in picks if p.get("is_vice_captain")), None)
    cap_multiplier = captain.get("multiplier", 1) if captain else 1

    xfer_cost = hist_gw.get("event_transfers_cost", 0) or 0
    transfers = hist_gw.get("event_transfers", 0) or 0
    fpl_raw   = hist_gw.get("points", 0) or 0

    # Bank and hits — same in both live and finished branches
    bank         = hist_gw.get("bank", 0) or 0
    bank_pen     = bank > BANK_THRESHOLD
    bank_pen_pts = BANK_PEN if bank_pen else 0
    hit_pts      = abs(xfer_cost)

    if not gw_finished:
        # ── Live GW ───────────────────────────────────────────────────────────
        played = finished_players or set()

        if bench_boost:
            # No subs happen on a bench boost — every one of the 15 is final
            # the moment their own fixture ends, so this is already a floor.
            effective_xi = active_players
            inactive     = [p for p in effective_xi
                            if p["element"] in played and mins.get(p["element"], 0) == 0]
        else:
            effective_xi = infer_autosubs(starters, bench, player_type, played, mins)
            # Conservative floor: only count a blank starter once every bench
            # player who could still rescue that slot has finished playing —
            # see live_confirmed_inactive()'s docstring for why.
            inactive = live_confirmed_inactive(starters, bench, player_type, played, mins)

        inactive_pen_pts = len(inactive) * INACTIVE_PEN

        # C/VC: both fixtures must be done AND both played 0
        cap_id    = captain["element"] if captain else None
        vice_id   = vice["element"]    if vice    else None
        cap_done  = cap_id  in played if cap_id  else False
        vc_done   = vice_id in played if vice_id else False
        cap_mins  = mins.get(cap_id,  0) if cap_id  else 0
        vc_mins   = mins.get(vice_id, 0) if vice_id else 0
        cvc_pen_pts = CVC_PEN if (cap_done and vc_done and cap_mins == 0 and vc_mins == 0) else 0

        chip_pen_pts: int = 0
        unused_chips: set[str] = set()

    else:
        # ── Finished GW ───────────────────────────────────────────────────────
        if bench_boost:
            effective_xi = active_players              # all 15, no auto-sub
        else:
            all_squad_ids = {p["element"] for p in picks}
            effective_xi = infer_autosubs(starters, bench, player_type, all_squad_ids, mins)

        inactive         = [p for p in effective_xi if mins.get(p["element"], 0) == 0]
        inactive_pen_pts = len(inactive) * INACTIVE_PEN

        cap_mins  = mins.get(captain["element"], 0) if captain else 0
        vice_mins = mins.get(vice["element"],    0) if vice    else 0
        cvc_pen_pts = CVC_PEN if (cap_mins == 0 and vice_mins == 0) else 0

        chip_pen_pts, unused_chips = 0, set()
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

    total_pens_gw = hit_pts + inactive_pen_pts + bank_pen_pts + cvc_pen_pts + chip_pen_pts
    anti_gw       = fpl_raw + total_pens_gw

    return {
        "gw":                gw,
        "active_chip":       active_chip,
        # FPL base
        "fpl_raw_pts":       fpl_raw,
        "fpl_xfer_cost":     xfer_cost,
        "transfers_gw":      transfers,
        "fpl_gw_rank":       hist_gw.get("rank"),
        "fpl_total":         hist_gw.get("total_points"),
        # Anti penalties breakdown
        "hit_pts":           hit_pts,
        "inactive_count":    len(inactive),
        "inactive_pen_pts":  inactive_pen_pts,
        "bank":              bank,
        "bank_pen":          bank_pen,
        "bank_pen_pts":      bank_pen_pts,
        "cvc_pen_pts":       cvc_pen_pts,
        "chip_pen_pts":      chip_pen_pts,
        "unused_chips":      list(unused_chips),
        "total_pens_gw":     total_pens_gw,
        # Anti score
        "anti_gw_pts":       anti_gw,
        "anti_total":        None,   # filled by score_team_season / score_one_gw_for_team
        "standing":          None,   # filled by ranking step
        # Captain / VC tiebreaker fields
        "captain_element":   captain["element"] if captain else None,
        "captain_mult":      cap_multiplier,
        "vice_element":      vice["element"] if vice else None,
        "captain_pts":       (pts.get(captain["element"], 0) * cap_multiplier)
                              if captain and captain["element"] in pts else None,
        "vice_pts":          pts.get(vice["element"], 0)
                              if vice and vice["element"] in pts else None,
    }


# ── Multi-GW season scorer (used for full backfills) ─────────────────────────

def score_team_season(
    team_id:          int,
    history:          dict,
    live_cache:       dict[int, dict[int, int]],   # gw → {player_id: minutes}
    pts_cache:        dict[int, dict[int, int]],   # gw → {player_id: total_points}
    picks_cache:      dict[int, dict],
    last_gw:          int,
    player_type:      dict[int, int],
    live_gw:          Optional[int] = None,
    finished_players: Optional[set[int]] = None,
) -> list[dict]:
    """
    Score every GW for one team — used by full-season backfills.

    For per-GW (live polling) updates, use score_one_gw_for_team instead.
    """
    chips = history.get("chips", [])
    first_half_chips, second_half_chips = split_chips_by_half(chips)

    gw_rows: dict[int, dict] = {g["event"]: g for g in history.get("current", [])}
    scored:  list[dict]      = []
    running  = 0

    for gw in sorted(gw_rows):
        picks_data = picks_cache.get(gw)
        if not picks_data:
            log.warning("  No picks for team=%d GW=%d — skipping", team_id, gw)
            continue

        mins        = live_cache.get(gw, {})
        pts         = pts_cache.get(gw, {})
        gw_finished = (gw != live_gw)

        result = score_gw(
            team_id           = team_id,
            gw                = gw,
            hist_gw           = gw_rows[gw],
            picks_data        = picks_data,
            mins              = mins,
            pts               = pts,
            player_type       = player_type,
            first_half_chips  = first_half_chips,
            second_half_chips = second_half_chips,
            gw_finished       = gw_finished,
            finished_players  = finished_players if not gw_finished else None,
        )

        running += result["anti_gw_pts"]
        result["anti_total"] = running
        scored.append(result)

    return scored


# ── Single-GW scorer (used by live polling tasks) ─────────────────────────────

def score_one_gw_for_team(
    team_id:          int,
    gw:               int,
    hist_gw:          dict,
    picks_data:       dict,
    mins:             dict[int, int],   # flat {player_id: minutes} for this GW
    pts:              dict[int, int],   # flat {player_id: points} for this GW
    player_type:      dict[int, int],
    chips:            list[dict],        # full chips history for team (for half-split)
    previous_anti_total: int = 0,        # anti_total from gw-1 in Supabase
    gw_finished:      bool = True,
    finished_players: Optional[set[int]] = None,
) -> dict:
    """
    Score a single GW for a single team. Designed for the live-polling path
    where we read previous_anti_total from Supabase and only compute the new GW.

    Returns one scored GW dict with anti_total = previous_anti_total + anti_gw_pts.
    """
    first_half_chips, second_half_chips = split_chips_by_half(chips)

    result = score_gw(
        team_id           = team_id,
        gw                = gw,
        hist_gw           = hist_gw,
        picks_data        = picks_data,
        mins              = mins,
        pts               = pts,
        player_type       = player_type,
        first_half_chips  = first_half_chips,
        second_half_chips = second_half_chips,
        gw_finished       = gw_finished,
        finished_players  = finished_players,
    )

    result["anti_total"] = previous_anti_total + (result["anti_gw_pts"] or 0)
    return result
