"""
Phase 1a/1b — Back-score the season, validate scoring, and run the cup.

Usage:
    # Score your 10 founding teams only
    python run_phase1a.py

    # Score all ~200 teams from the test mini-league (Phase 1b)
    python run_phase1a.py --league 248502

    # Save output to JSON (recommended — use this to validate scores)
    python run_phase1a.py --out results.json

    # Run with cup simulation (requires enough GW data up to GW38)
    python run_phase1a.py --league 248502 --cup --out results.json
"""

import argparse
import json
import logging
from datetime import datetime, timezone

from anti_fpl_scoring import (
    fetch_bootstrap,
    get_all_team_ids_from_league,
    score_league,
    print_standings,
    detect_live_gw,
)
from anti_fpl_cup import (
    run_cup,
    print_cup_results,
    CUP_START_GW,
    CUP_MAX,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Founding team IDs ─────────────────────────────────────────────────────────
FOUNDING_TEAMS = [
    5388975, 6703903, 6595399, 3640882, 5399604,
    6654853, 7667159, 1610262, 3155889, 911549,
]

# ── Scoring & cup rules (single source of truth for frontend later) ───────────
RULES = {
    "scoring": [
        {
            "id":          "inactive_player",
            "title":       "Inactive Player Penalty",
            "description": (
                "After auto-subs, any player in your final XI who played 0 minutes "
                "earns you +9 points per player. During Bench Boost, this applies "
                "to all 15 players in your squad."
            ),
            "penalty":     "+9 pts per inactive player",
        },
        {
            "id":          "bank_penalty",
            "title":       "Bank Penalty",
            "description": (
                "You must spend your money. If your bank exceeds £3.0m at any point "
                "during a Gameweek, you receive a +25 point penalty for that week. "
                "Repeat offenders may be removed from the league."
            ),
            "penalty":     "+25 pts per GW over £3.0m",
        },
        {
            "id":          "hits",
            "title":       "Transfer Hits",
            "description": (
                "Normal FPL transfer rules apply, but in Anti-FPL hits work in your "
                "favour — each hit costs you +4 points rather than -4. Taking hits "
                "is encouraged!"
            ),
            "penalty":     "+4 pts per extra transfer",
        },
        {
            "id":          "cvc_penalty",
            "title":       "Captain / Vice-Captain Penalty",
            "description": (
                "If both your Captain AND Vice-Captain play 0 minutes, you receive "
                "a +15 point penalty. If your Captain doesn't play, the Vice-Captain "
                "takes over as normal — the penalty only applies if neither plays."
            ),
            "penalty":     "+15 pts if both C and VC play 0 minutes",
        },
        {
            "id":          "chip_penalty",
            "title":       "Chip Usage Penalty",
            "description": (
                "This season every manager receives two of each chip: "
                "Wildcard (×2), Free Hit (×2), Bench Boost (×2), Triple Captain (×2). "
                "Wildcards are exempt from penalties. All other chips MUST be used — "
                "the first set by GW19, the second set by GW38. "
                "Each unused chip earns +25 points at the respective checkpoint. "
                "Unused chips also make you ineligible to win the league or the cup."
            ),
            "penalty":     "+25 pts per unused chip at GW19 and GW38",
        },
        {
            "id":          "bench_boost",
            "title":       "Bench Boost Special Rule",
            "description": (
                "When you play your Bench Boost, scoring is applied across all 15 "
                "players. If any of your bench players register 0 minutes, the "
                "inactive player penalty applies to them too. Choose your BB week wisely."
            ),
            "penalty":     "Inactive penalty extends to all 15 players",
        },
    ],
    "standings": {
        "description": (
            "Anti-FPL is the opposite of FPL — the LOWEST score wins. "
            "Every penalty point added to your score is a point in your favour. "
            "Standings are ranked lowest total score to highest."
        ),
    },
    "cup": {
        "description": (
            "The Anti-FPL Knockout Cup runs alongside the main league from GW29 to GW38. "
            "The top 1,024 managers by league standing at GW28 qualify. "
            "Each round is decided over a single Gameweek — lowest anti score wins. "
            "The draw is random, but the league leader at GW28 receives a bye "
            "if there is an odd number of entrants."
        ),
        "format":      "Single elimination, 10 rounds, GW29–GW38",
        "qualifies":   "Top 1,024 by league standing at GW28",
        "tiebreakers": [
            "Lowest anti-GW score",
            "Lowest captain points (including chip multiplier)",
            "Lowest vice-captain points",
            "Lowest total penalties that GW",
            "Better league standing",
        ],
        "notes": [
            "The cup has no effect on league standings.",
            "Managers with unused chips at GW19 or GW38 are ineligible to win the cup.",
        ],
    },
    "registration": {
        "description": (
            "Registration for the global Anti-FPL league opens when FPL opens for "
            "the new season (typically June/July) and closes hard at the GW1 deadline. "
            "No mid-season joins are permitted — joining late would give an unfair "
            "points advantage in a lowest-score-wins format."
        ),
        "deadline": "GW1 kickoff — no exceptions",
    },
}


def build_scored_gws_index(teams: list[dict]) -> dict[int, dict[int, dict]]:
    """Build {team_id: {gw: scored_gw_dict}} index for the cup engine."""
    return {
        t["team_id"]: {g["gw"]: g for g in t.get("gws", [])}
        for t in teams
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--league", type=int,            help="FPL mini-league ID to pull team IDs from")
    parser.add_argument("--team",   type=int,            help="Score only this single team ID (skips league/founders)")
    parser.add_argument("--out",    type=str,            help="Save full results to this JSON file")
    parser.add_argument("--cup",    action="store_true", help="Simulate the cup (needs GW29+ data)")
    parser.add_argument("--seed",   type=int, default=42, help="Random seed for cup draw (default: 42)")
    args = parser.parse_args()

    log.info("=" * 56)
    log.info("Anti-FPL Phase 1  —  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 56)

    # Bootstrap
    log.info("Fetching bootstrap...")
    bootstrap = fetch_bootstrap()
    if not bootstrap:
        log.error("Bootstrap fetch failed — check your internet connection.")
        raise SystemExit(1)

    last_gw, live_gw = detect_live_gw(bootstrap)
    if live_gw:
        log.info("GW%d is live (deadline passed, fixtures in progress)", live_gw)
    else:
        log.info("Last finished GW: %d", last_gw)

    # Team IDs
    if args.team:
        team_ids = [args.team]
        log.info("Single-team mode: scoring team %d only.", args.team)
    elif args.league:
        log.info("Pulling team IDs from mini-league %d...", args.league)
        team_ids = get_all_team_ids_from_league(args.league)
        log.info("Found %d teams.", len(team_ids))
    else:
        team_ids = FOUNDING_TEAMS
        log.info("Using %d founding teams.", len(team_ids))

    # Score league
    log.info("Scoring %d teams for GW1–%d...", len(team_ids), last_gw)
    teams = score_league(team_ids, last_gw, live_gw=live_gw)
    print_standings(teams)

    # Cup (optional)
    cup_results = None
    if args.cup:
        if last_gw < CUP_START_GW:
            log.warning(
                "Cup starts GW%d — current GW is %d. Skipping cup simulation.",
                CUP_START_GW, last_gw,
            )
        else:
            log.info("Running cup simulation (seed=%d)...", args.seed)
            # League standings at GW28 = standings before cup starts
            # Use current standings as proxy (full season = same thing at GW38)
            scored_index = build_scored_gws_index(teams)
            cup_results  = run_cup(
                league_standings = teams,
                scored_gws       = scored_index,
                draw_seed        = args.seed,
            )
            id_to_name = {t["team_id"]: t["team_name"] for t in teams}
            print_cup_results(cup_results, id_to_name)

    # Save output
    if args.out:
        output = {
            "metadata": {
                "generated":  datetime.now(timezone.utc).isoformat(),
                "last_gw":    last_gw,
                "team_count": len(teams),
            },
            "rules":  RULES,       # single source of truth — frontend reads this
            "teams":  teams,
            "cup":    cup_results,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=2, ensure_ascii=False)
        log.info("Results saved -> %s", args.out)
        log.info("Note: rules are embedded in output JSON for frontend consumption.")


if __name__ == "__main__":
    main()
