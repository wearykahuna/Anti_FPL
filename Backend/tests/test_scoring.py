"""
Unit tests for the pure Anti-FPL scoring engine (Backend/scoring.py).

Run from the repo root:
    pytest Backend/tests
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scoring import (
    BANK_PEN,
    CVC_PEN,
    INACTIVE_PEN,
    UNUSED_CHIP_PEN,
    _valid_formation,
    calc_fpl_raw,
    infer_autosubs,
    score_gw,
    score_one_gw_for_team,
    split_chips_by_half,
    unused_chip_penalty,
)

# ── Fixtures: a legal 15-man squad ────────────────────────────────────────────
# Starting XI: GK 101 / DEF 102-105 / MID 106-109 / FWD 110-111  (1-4-4-2)
# Bench (in priority order): GK 112, DEF 113, MID 114, FWD 115

GK1, GK2 = 101, 112
SQUAD = [101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111,  # XI
         112, 113, 114, 115]                                      # bench

PLAYER_TYPE = {
    101: 1, 112: 1,                     # GKs
    102: 2, 103: 2, 104: 2, 105: 2, 113: 2,   # DEFs
    106: 3, 107: 3, 108: 3, 109: 3, 114: 3,   # MIDs
    110: 4, 111: 4, 115: 4,                    # FWDs
}

ALL_IDS = set(SQUAD)


def mins(played_zero=(), bench_played=True):
    """90 mins for everyone except `played_zero`; bench optionally played."""
    m = {pid: 90 for pid in SQUAD}
    for pid in played_zero:
        m[pid] = 0
    if not bench_played:
        for pid in SQUAD[11:]:
            m[pid] = 0
    return m


def pts(default=2, **overrides):
    p = {pid: default for pid in SQUAD}
    for k, v in overrides.items():
        p[int(k)] = v
    return p


def make_picks(captain=110, vice=106, chip="", cap_mult=2):
    return {
        "active_chip": chip,
        "picks": [
            {
                "element":         pid,
                "position":        i + 1,
                "is_captain":      pid == captain,
                "is_vice_captain": pid == vice,
                "multiplier":      cap_mult if pid == captain else 1,
            }
            for i, pid in enumerate(SQUAD)
        ],
    }


def make_hist(points=40, xfer_cost=0, bank=10):
    return {
        "event": 5, "points": points, "event_transfers_cost": xfer_cost,
        "bank": bank, "rank": 100, "total_points": 200,
    }


def starters_bench():
    starters = [{"element": pid, "position": i + 1} for i, pid in enumerate(SQUAD[:11])]
    bench    = [{"element": pid, "position": i + 12} for i, pid in enumerate(SQUAD[11:])]
    return starters, bench


# ── Formation validation ──────────────────────────────────────────────────────

def test_valid_formation_142():
    assert _valid_formation(SQUAD[:11], PLAYER_TYPE)

def test_invalid_formation_two_gks():
    xi = [101, 112] + SQUAD[1:10]
    assert not _valid_formation(xi, PLAYER_TYPE)

def test_invalid_formation_two_defs():
    xi = [101, 102, 103] + [106, 107, 108, 109, 114] + [110, 111, 115]
    assert not _valid_formation(xi, PLAYER_TYPE)


# ── Auto-sub inference ────────────────────────────────────────────────────────

def test_autosub_outfield_swap():
    starters, bench = starters_bench()
    m = mins(played_zero=[106])                       # MID 106 played 0
    xi = infer_autosubs(starters, bench, PLAYER_TYPE, ALL_IDS, m)
    xi_ids = [p["element"] for p in xi]
    assert 106 not in xi_ids
    assert 113 in xi_ids                              # first outfield bench (DEF 113)

def test_autosub_gk_for_gk_only():
    starters, bench = starters_bench()
    m = mins(played_zero=[101])                       # GK played 0
    xi = infer_autosubs(starters, bench, PLAYER_TYPE, ALL_IDS, m)
    xi_ids = [p["element"] for p in xi]
    assert 101 not in xi_ids and 112 in xi_ids        # replaced by bench GK
    assert 113 not in xi_ids

def test_autosub_respects_bench_priority():
    starters, bench = starters_bench()
    m = mins(played_zero=[106, 107])                  # two MIDs played 0
    xi = infer_autosubs(starters, bench, PLAYER_TYPE, ALL_IDS, m)
    xi_ids = [p["element"] for p in xi]
    assert 113 in xi_ids and 114 in xi_ids            # bench order 13 then 14

def test_autosub_blocked_by_formation():
    # All 4 starting DEFs played 0; only 1 bench DEF available. After that sub,
    # further DEF replacements by MID/FWD would leave <3 DEFs → blocked.
    starters, bench = starters_bench()
    m = mins(played_zero=[102, 103, 104, 105])
    xi = infer_autosubs(starters, bench, PLAYER_TYPE, ALL_IDS, m)
    types = [PLAYER_TYPE[p["element"]] for p in xi]
    assert types.count(2) >= 3                        # never below 3 DEF

def test_autosub_live_skips_unfinished_players():
    starters, bench = starters_bench()
    m = mins(played_zero=[106])
    finished = ALL_IDS - {106}                        # 106's fixture not finished
    xi = infer_autosubs(starters, bench, PLAYER_TYPE, finished, m)
    xi_ids = [p["element"] for p in xi]
    assert 106 in xi_ids                              # not subbed until fixture done


# ── calc_fpl_raw ──────────────────────────────────────────────────────────────

def test_raw_simple_sum_with_captain():
    p = pts(default=2)
    m = mins()
    raw = calc_fpl_raw(SQUAD, 110, 106, "", p, m, PLAYER_TYPE)
    assert raw == 11 * 2 + 2                          # XI + captain doubled

def test_raw_triple_captain():
    p = pts(default=2)
    m = mins()
    raw = calc_fpl_raw(SQUAD, 110, 106, "3xc", p, m, PLAYER_TYPE)
    assert raw == 11 * 2 + 2 * 2                      # captain ×3 → +2 extra

def test_raw_vice_promoted_when_captain_blanks():
    p = pts(default=2, **{"106": 5})
    m = mins(played_zero=[110])                       # captain played 0
    raw = calc_fpl_raw(SQUAD, 110, 106, "", p, m, PLAYER_TYPE)
    # Captain 110 subbed out; bench priority puts DEF 113 on (5-4-1 is legal).
    # XI = 10 players on 2 pts + vice 106 on 5 pts = 25; vice bonus doubles 106.
    assert raw == 25 + 5

def test_raw_bench_boost_counts_all_15():
    p = pts(default=1)
    m = mins()
    raw = calc_fpl_raw(SQUAD, 110, 106, "bboost", p, m, PLAYER_TYPE)
    assert raw == 15 * 1 + 1                          # all 15 + captain double


# ── Chip helpers ──────────────────────────────────────────────────────────────

def test_split_chips_by_half_boundary():
    chips = [{"name": "wildcard", "event": 19}, {"name": "bboost", "event": 20}]
    first, second = split_chips_by_half(chips)
    assert "wildcard" in first and "bboost" in second

def test_unused_chip_penalty_wildcard_exempt():
    pen, unused = unused_chip_penalty({"wildcard"})
    assert pen == 3 * UNUSED_CHIP_PEN
    assert unused == {"bboost", "3xc", "freehit"}

def test_unused_chip_penalty_all_used():
    pen, unused = unused_chip_penalty({"bboost", "3xc", "freehit"})
    assert pen == 0 and unused == set()


# ── score_gw: finished GW ─────────────────────────────────────────────────────

def base_score_kwargs(**overrides):
    kw = dict(
        team_id=1, gw=5,
        hist_gw=make_hist(),
        picks_data=make_picks(),
        mins=mins(),
        pts=pts(),
        player_type=PLAYER_TYPE,
        first_half_chips=set(),
        second_half_chips=set(),
        gw_finished=True,
    )
    kw.update(overrides)
    return kw

def test_finished_no_penalties():
    r = score_gw(**base_score_kwargs())
    assert r["total_pens_gw"] == 0
    assert r["anti_gw_pts"] == 40                     # fpl_raw straight through

def test_finished_hit_points():
    r = score_gw(**base_score_kwargs(hist_gw=make_hist(xfer_cost=8)))
    assert r["hit_pts"] == 8
    assert r["anti_gw_pts"] == 48

def test_bank_penalty_boundary():
    r30 = score_gw(**base_score_kwargs(hist_gw=make_hist(bank=30)))
    r31 = score_gw(**base_score_kwargs(hist_gw=make_hist(bank=31)))
    assert r30["bank_pen_pts"] == 0
    assert r31["bank_pen_pts"] == BANK_PEN

def test_inactive_penalty_after_autosubs():
    # 106 played 0 → auto-subbed for 113 (who played) → no inactive pen.
    r = score_gw(**base_score_kwargs(mins=mins(played_zero=[106])))
    assert r["inactive_pen_pts"] == 0
    # 106 played 0 AND all bench played 0 → no sub possible → 1 inactive pen.
    r2 = score_gw(**base_score_kwargs(mins=mins(played_zero=[106], bench_played=False)))
    assert r2["inactive_count"] == 1
    assert r2["inactive_pen_pts"] == INACTIVE_PEN

def test_cvc_penalty_both_blank():
    m = mins(played_zero=[110, 106], bench_played=False)     # cap + vice 0 mins
    r = score_gw(**base_score_kwargs(mins=m))
    assert r["cvc_pen_pts"] == CVC_PEN

def test_cvc_no_penalty_if_vice_played():
    m = mins(played_zero=[110], bench_played=False)
    r = score_gw(**base_score_kwargs(mins=m))
    assert r["cvc_pen_pts"] == 0

def test_chip_penalty_applied_at_gw19_only():
    r18 = score_gw(**base_score_kwargs(gw=18))
    r19 = score_gw(**base_score_kwargs(gw=19))
    assert r18["chip_pen_pts"] == 0
    assert r19["chip_pen_pts"] == 3 * UNUSED_CHIP_PEN

def test_chip_penalty_at_gw38_second_half():
    r = score_gw(**base_score_kwargs(gw=38, second_half_chips={"bboost", "freehit"}))
    assert r["chip_pen_pts"] == 1 * UNUSED_CHIP_PEN   # only 3xc unused
    assert r["unused_chips"] == ["3xc"]

def test_bench_boost_inactive_applies_to_all_15():
    m = mins(bench_played=False)                       # 4 bench players at 0 mins
    r = score_gw(**base_score_kwargs(picks_data=make_picks(chip="bboost"), mins=m))
    assert r["inactive_count"] == 4
    assert r["inactive_pen_pts"] == 4 * INACTIVE_PEN


# ── score_gw: live GW ─────────────────────────────────────────────────────────

def test_live_no_chip_penalty_at_gw19():
    r = score_gw(**base_score_kwargs(gw=19, gw_finished=False, finished_players=ALL_IDS))
    assert r["chip_pen_pts"] == 0

def test_live_inactive_only_when_fixture_done():
    m = mins(played_zero=[106], bench_played=False)
    # 106's fixture not finished → no penalty yet
    r = score_gw(**base_score_kwargs(gw_finished=False, mins=m,
                                     finished_players=ALL_IDS - {106}))
    assert r["inactive_pen_pts"] == 0
    # fixture done → penalty confirmed
    r2 = score_gw(**base_score_kwargs(gw_finished=False, mins=m,
                                      finished_players=ALL_IDS))
    assert r2["inactive_pen_pts"] == INACTIVE_PEN

def test_live_inactive_is_conservative_floor_while_bench_unresolved():
    # Two MIDs blank (106, 107). Bench DEF (113) already played and covers
    # 106. Bench MID/FWD (114, 115) haven't finished yet — 107 *might* still
    # be rescued by one of them, so it must not count as inactive yet.
    m = mins(played_zero=[106, 107])
    m[113] = 90                                    # bench DEF already played
    finished = ALL_IDS - {114, 115}                # 114/115 fixtures not done yet
    r = score_gw(**base_score_kwargs(gw_finished=False, mins=m,
                                     finished_players=finished))
    assert r["inactive_count"] == 0
    assert r["inactive_pen_pts"] == 0

def test_live_inactive_floor_locks_in_once_bench_exhausted():
    # Same setup, but 114 and 115 have now ALSO finished, both blank — so
    # neither could have covered 107 either. Its inactive status is locked
    # in for good; note this is strictly >= the previous (0), never <.
    m = mins(played_zero=[106, 107])
    m[113] = 90                                    # bench DEF already played
    m[114] = 0                                     # bench MID finished blank
    m[115] = 0                                     # bench FWD finished blank
    r = score_gw(**base_score_kwargs(gw_finished=False, mins=m,
                                     finished_players=ALL_IDS))
    assert r["inactive_count"] == 1
    assert r["inactive_pen_pts"] == INACTIVE_PEN

def test_live_cvc_requires_both_fixtures_done():
    m = mins(played_zero=[110, 106], bench_played=False)
    r = score_gw(**base_score_kwargs(gw_finished=False, mins=m,
                                     finished_players=ALL_IDS - {106}))
    assert r["cvc_pen_pts"] == 0
    r2 = score_gw(**base_score_kwargs(gw_finished=False, mins=m,
                                      finished_players=ALL_IDS))
    assert r2["cvc_pen_pts"] == CVC_PEN

def test_live_bank_and_hits_apply_immediately():
    r = score_gw(**base_score_kwargs(gw_finished=False, finished_players=set(),
                                     hist_gw=make_hist(xfer_cost=4, bank=40)))
    assert r["hit_pts"] == 4
    assert r["bank_pen_pts"] == BANK_PEN


# ── score_one_gw_for_team ─────────────────────────────────────────────────────

def test_running_total_chains_from_previous():
    r = score_one_gw_for_team(
        team_id=1, gw=5,
        hist_gw=make_hist(points=30),
        picks_data=make_picks(),
        mins=mins(), pts=pts(), player_type=PLAYER_TYPE,
        chips=[], previous_anti_total=100, gw_finished=True,
    )
    assert r["anti_total"] == 130
