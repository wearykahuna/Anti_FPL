"""
Anti-FPL Knockout Cup Engine
==============================
- Always 10 rounds, always starts GW29, finishes GW38
- Top 1024 by league standing at GW28 qualify
- Random draw; top seed gets bye if odd entrants
- Single GW score per round, lowest anti score wins

Tiebreaker chain (all lowest wins):
  1. anti_gw_pts for that round's GW
  2. captain_pts (boosted)
  3. vice_pts
  4. total_pens_gw
  5. league standing (lower number = better)
"""

import random
import logging
from typing import Optional

log = logging.getLogger(__name__)

CUP_START_GW   = 29
CUP_END_GW     = 38
CUP_ROUNDS     = CUP_END_GW - CUP_START_GW + 1   # 10
CUP_MAX        = 2 ** CUP_ROUNDS                   # 1024


# ── Tiebreaker ────────────────────────────────────────────────────────────────

def tiebreak(a: dict, b: dict) -> dict:
    """
    Compare two cup entries for one GW. Return the winner.
    All comparisons: lower is better.

    Each entry dict must contain:
      anti_gw_pts, captain_pts, vice_pts, total_pens_gw, standing
    """
    for key in ("anti_gw_pts", "captain_pts", "vice_pts", "total_pens_gw", "standing"):
        va = a.get(key) or 0
        vb = b.get(key) or 0
        if va < vb:
            return a
        if vb < va:
            return b
    # Absolute tie — keep higher seed (lower standing number)
    return a if (a.get("standing") or 9999) <= (b.get("standing") or 9999) else b


# ── Draw ──────────────────────────────────────────────────────────────────────

def make_draw(qualifiers: list[dict], seed: int = None) -> list[tuple[dict, Optional[dict]]]:
    """
    Create the round 1 draw from the list of qualifying teams.
    qualifiers: sorted by standing (index 0 = top seed).
    Returns list of (home, away) pairs. Away=None means bye.
    """
    if seed is not None:
        random.seed(seed)

    top_seed  = qualifiers[0]
    remaining = qualifiers[1:]
    random.shuffle(remaining)

    # If odd total, top seed gets bye
    if len(qualifiers) % 2 == 1:
        fixtures = [(top_seed, None)]
        pool = remaining
    else:
        pool = [top_seed] + remaining
        fixtures = []

    # Pair up the rest randomly
    for i in range(0, len(pool) - 1, 2):
        fixtures.append((pool[i], pool[i + 1]))

    # If pool has odd length after top seed bye, last team also gets bye
    if len(pool) % 2 == 1:
        fixtures.append((pool[-1], None))

    return fixtures


# ── Round resolver ────────────────────────────────────────────────────────────

def resolve_round(
    fixtures:  list[tuple[dict, Optional[dict]]],
    gw:        int,
    scored_gws: dict[int, dict[int, dict]],  # team_id → gw → scored gw dict
) -> tuple[list[dict], list[dict]]:
    """
    Resolve all fixtures for one cup round.

    scored_gws: {team_id: {gw: scored_gw_dict}}
    Returns (winners, results) where results is a list of match result dicts.
    """
    winners = []
    results = []

    for home, away in fixtures:
        if away is None:
            # Bye — home team advances automatically
            winners.append(home)
            results.append({
                "gw":       gw,
                "home":     home["team_id"],
                "away":     None,
                "winner":   home["team_id"],
                "bye":      True,
            })
            continue

        home_gw = scored_gws.get(home["team_id"], {}).get(gw, {})
        away_gw = scored_gws.get(away["team_id"], {}).get(gw, {})

        # Build comparison dicts
        home_entry = {
            "team_id":       home["team_id"],
            "anti_gw_pts":   home_gw.get("anti_gw_pts", 9999),
            "captain_pts":   home_gw.get("captain_pts") or 9999,
            "vice_pts":      home_gw.get("vice_pts")    or 9999,
            "total_pens_gw": home_gw.get("total_pens_gw", 9999),
            "standing":      home.get("standing", 9999),
        }
        away_entry = {
            "team_id":       away["team_id"],
            "anti_gw_pts":   away_gw.get("anti_gw_pts", 9999),
            "captain_pts":   away_gw.get("captain_pts") or 9999,
            "vice_pts":      away_gw.get("vice_pts")    or 9999,
            "total_pens_gw": away_gw.get("total_pens_gw", 9999),
            "standing":      away.get("standing", 9999),
        }

        winner_entry = tiebreak(home_entry, away_entry)
        winner_team  = home if winner_entry["team_id"] == home["team_id"] else away
        winners.append(winner_team)

        results.append({
            "gw":            gw,
            "home":          home["team_id"],
            "home_pts":      home_entry["anti_gw_pts"],
            "away":          away["team_id"],
            "away_pts":      away_entry["anti_gw_pts"],
            "winner":        winner_team["team_id"],
            "tiebreak_used": home_entry["anti_gw_pts"] == away_entry["anti_gw_pts"],
            "bye":           False,
        })

        log.info(
            "  GW%d  %s (%d) vs %s (%d)  → winner: %s",
            gw,
            home.get("team_name", home["team_id"]), home_entry["anti_gw_pts"],
            away.get("team_name", away["team_id"]), away_entry["anti_gw_pts"],
            winner_team.get("team_name", winner_team["team_id"]),
        )

    return winners, results


# ── Full cup runner ───────────────────────────────────────────────────────────

def run_cup(
    league_standings: list[dict],
    scored_gws:       dict[int, dict[int, dict]],
    draw_seed:        int = None,
) -> dict:
    """
    Run the full cup from GW29 to GW38.

    league_standings: full sorted league (standing 1 = leader) at GW28.
    scored_gws:       {team_id: {gw: scored_gw_dict}} for GW29–38.
    draw_seed:        optional random seed for reproducible draws.

    Returns a cup result dict with all rounds and the winner.
    """
    # Qualify top 1024
    qualifiers = league_standings[:CUP_MAX]
    log.info("Cup: %d teams qualify (max %d)", len(qualifiers), CUP_MAX)

    all_rounds = []
    current_round = qualifiers
    fixtures = make_draw(current_round, seed=draw_seed)

    for round_num in range(1, CUP_ROUNDS + 1):
        gw = CUP_START_GW + round_num - 1   # GW29, 30, ... 38
        log.info("Cup round %d / GW%d — %d fixtures", round_num, gw, len(fixtures))

        winners, results = resolve_round(fixtures, gw, scored_gws)
        all_rounds.append({
            "round":    round_num,
            "gw":       gw,
            "fixtures": results,
            "winners":  [w["team_id"] for w in winners],
        })

        if len(winners) == 1:
            champion = winners[0]
            break

        # Next round draw (random reshuffle of winners each round)
        if draw_seed is not None:
            draw_seed += 1   # advance seed so each round differs
        fixtures = make_draw(winners, seed=draw_seed)
    else:
        champion = winners[0] if winners else None

    log.info("Cup winner: %s", champion.get("team_name") if champion else "unknown")

    return {
        "rounds":    all_rounds,
        "champion":  champion,
        "qualifiers": [q["team_id"] for q in qualifiers],
    }


# ── Printer ───────────────────────────────────────────────────────────────────

def print_cup_results(cup: dict, id_to_name: dict[int, str]) -> None:
    name = lambda tid: id_to_name.get(tid, str(tid))
    print()
    print("=" * 60)
    print("  Anti-FPL Cup Results")
    print("=" * 60)
    for r in cup["rounds"]:
        print(f"\n  Round {r['round']}  (GW{r['gw']})")
        print("  " + "-" * 50)
        for f in r["fixtures"]:
            if f["bye"]:
                print(f"    {name(f['home']):25}  BYE")
            else:
                tb = " *TB*" if f["tiebreak_used"] else ""
                w  = "✓" if f["winner"] == f["home"] else " "
                w2 = "✓" if f["winner"] == f["away"] else " "
                print(
                    f"    {w} {name(f['home']):22} {f['home_pts']:>4} pts"
                    f"  vs  {f['away_pts']:>4} pts  {name(f['away']):22} {w2}{tb}"
                )
    champ = cup.get("champion")
    if champ:
        print()
        print(f"  🏆  CHAMPION: {name(champ['team_id'])}")
    print("=" * 60)
    print()
