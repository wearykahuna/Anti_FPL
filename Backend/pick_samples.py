"""
Pick a diverse set of sample teams from results JSON for manual validation.
Selects teams across different profiles to maximise scoring branch coverage.

Usage:
    python pick_samples.py results_1b.json
"""

import json
import sys


def pick_samples(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    teams = data.get("teams", [])
    if not teams:
        print("No teams found in JSON.")
        return

    samples = {}

    # 1. Most chip-heavy team — most chip penalties or most chips used
    chip_heavy = max(
        teams,
        key=lambda t: sum(g.get("chip_pen_pts", 0) or 0 for g in t.get("gws", [])),
    )
    samples["Most chip penalty"] = chip_heavy

    # 2. Most hits taken
    hit_heavy = max(
        teams,
        key=lambda t: sum(g.get("hit_pts", 0) or 0 for g in t.get("gws", [])),
    )
    samples["Most transfer hits"] = hit_heavy

    # 3. Most inactive player penalties
    inactive_heavy = max(
        teams,
        key=lambda t: sum(g.get("inactive_pen_pts", 0) or 0 for g in t.get("gws", [])),
    )
    samples["Most inactive penalties"] = inactive_heavy

    # 4. Used Bench Boost (any GW)
    bb_team = next(
        (t for t in teams if any(g.get("active_chip") == "bboost" for g in t.get("gws", []))),
        None,
    )
    if bb_team:
        samples["Used Bench Boost"] = bb_team

    # 5. Triggered C/VC penalty at some point
    cvc_team = next(
        (t for t in teams if any((g.get("cvc_pen_pts") or 0) > 0 for g in t.get("gws", []))),
        None,
    )
    if cvc_team:
        samples["Hit C/VC penalty"] = cvc_team

    # 6. Bank penalty at some point
    bank_team = next(
        (t for t in teams if any(g.get("bank_pen") for g in t.get("gws", []))),
        None,
    )
    if bank_team:
        samples["Hit bank penalty"] = bank_team

    # 7. League leader (lowest score)
    samples["Current leader"] = teams[0]

    # 8. League bottom (highest score)
    samples["Current bottom"] = teams[-1]

    # Print summary
    print("=" * 72)
    print(f"Validation samples from {path}")
    print("=" * 72)
    for label, team in samples.items():
        if not team:
            continue
        tid    = team["team_id"]
        name   = team.get("team_name", "Unknown")
        mgr    = team.get("manager", "?")
        total  = team.get("anti_total", "?")
        stand  = team.get("standing", "?")
        print()
        print(f"  [{label}]")
        print(f"    Team ID:  {tid}")
        print(f"    Manager:  {mgr}")
        print(f"    Team:     {name}")
        print(f"    Standing: #{stand}  |  Anti total: {total}")
        print(f"    FPL link: https://fantasy.premierleague.com/entry/{tid}/history")
    print()
    print("=" * 72)
    print("Validate by checking the FPL history page above against the GW")
    print("breakdown in your results JSON for that team_id.")
    print("=" * 72)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python pick_samples.py results_1b.json")
        sys.exit(1)
    pick_samples(sys.argv[1])
