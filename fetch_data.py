#!/usr/bin/env python3
"""
Anti-FPL Mini League Data Scraper

Usage:
  python fetch_data.py            # fetch Anti-FPL + FPL history, write data.json
  python fetch_data.py --picks    # also fetch/update picks cache, merge into data.json

Picks cache (picks_cache.json) is incremental — only new GWs are fetched each run.
Past GW picks never change, so no need to re-fetch.
"""

import argparse
import json
import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup

# ── Config ────────────────────────────────────────────────────────────────────
TEAM_IDS = [
    5388975, 6703903, 6595399, 3640882, 5399604,
    6654853, 7667159, 1610262, 3155889, 911549,
]
FPL_BASE       = "https://fantasy.premierleague.com/api"
ANTIFPL_URL    = "https://antifpl.pythonanywhere.com/antifpl/manager/{id}/"
OUTPUT_FILE    = "data.json"
PICKS_CACHE    = "picks_cache.json"

BANK_PEN_THRESHOLD = 30   # bank > 30 (units of £0.1m) = >£3.0m → +25 pts penalty

COLORS = [
    "#4ade80", "#60a5fa", "#f59e0b", "#f87171",
    "#a78bfa", "#34d399", "#fb923c", "#38bdf8",
    "#e879f9", "#facc15",
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Session ───────────────────────────────────────────────────────────────────
_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})


# ── Low-level HTTP ────────────────────────────────────────────────────────────
def _get(url: str, retries: int = 3) -> Optional[requests.Response]:
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=20)
            r.raise_for_status()
            return r
        except Exception as exc:
            log.warning("Attempt %d/%d  %s  [%s]", attempt + 1, retries, url, exc)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def fetch_json(url: str) -> Optional[dict]:
    r = _get(url)
    if r is None:
        return None
    try:
        return r.json()
    except Exception as exc:
        log.warning("JSON decode failed for %s: %s", url, exc)
        return None


def _int(val) -> Optional[int]:
    if val is None or str(val).strip() == "":
        return None
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return None


# ── FPL helpers ───────────────────────────────────────────────────────────────
def bootstrap_current_gw(bootstrap: dict) -> int:
    finished = [e for e in bootstrap.get("events", []) if e.get("finished")]
    return max((e["id"] for e in finished), default=1) if finished else 1


def team_info(team_id: int) -> dict:
    data = fetch_json(f"{FPL_BASE}/entry/{team_id}/")
    if not data:
        return {"id": team_id, "manager": f"Unknown {team_id}", "team_name": f"Team {team_id}"}
    return {
        "id": team_id,
        "manager": (
            f"{data.get('player_first_name', '')} "
            f"{data.get('player_last_name', '')}"
        ).strip(),
        "team_name": data.get("name", f"Team {team_id}"),
        "fpl_overall_rank": data.get("summary_overall_rank"),
        "fpl_overall_pts":  data.get("summary_overall_points"),
    }


def team_history(team_id: int) -> dict[int, dict]:
    data = fetch_json(f"{FPL_BASE}/entry/{team_id}/history/")
    if not data:
        return {}
    chips = {c["event"]: c["name"] for c in data.get("chips", [])}
    result: dict[int, dict] = {}
    for gw in data.get("current", []):
        event     = gw["event"]
        raw_pts   = gw["points"]
        xfer_cost = gw.get("event_transfers_cost", 0)
        result[event] = {
            "fpl_raw_pts":   raw_pts,
            "fpl_net_pts":   raw_pts - xfer_cost,
            "fpl_xfer_cost": xfer_cost,
            "fpl_total":     gw["total_points"],
            "fpl_gw_rank":   gw.get("rank"),
            "chip":          chips.get(event, ""),
        }
    return result


# ── Anti-FPL scraper ─────────────────────────────────────────────────────────
def parse_antifpl_table(html: str) -> dict[int, dict]:
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    if not tables:
        return {}

    tbl     = tables[0]
    headers = [th.get_text(" ", strip=True) for th in tbl.find_all("th")]
    if not headers:
        return {}

    result: dict[int, dict] = {}
    for tr in tbl.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if not cells or len(cells) < len(headers) - 1:
            continue

        row = dict(zip(headers, cells))
        gw_cell = row.get("Gameweek", "")
        m = re.match(r"^(\d+)", gw_cell.strip())
        if not m:
            continue
        gw = int(m.group(1))

        bank_val       = _int(row.get("Bank"))
        # Anti-FPL site "C/VC Pens" column stores penalty POINTS (15 per pen), not count
        cvc_pts_raw    = _int(row.get("C/VC Pens")) or 0
        cvc_count      = cvc_pts_raw // 15
        inactive_count = _int(row.get("Inactive Players")) or 0

        result[gw] = {
            "gw_rank":        _int(row.get("GW Rank")),
            "last_rank":      _int(row.get("Last Rank")),
            "team_value":     _int(row.get("Team Value")),
            "bank":           bank_val,
            "bank_pen":       bank_val is not None and bank_val > BANK_PEN_THRESHOLD,
            "transfers":      _int(row.get("Transfers")),
            "xfer_cost_pens": _int(row.get("Transfer Cost")),
            "chip":           row.get("Chip", "").strip().lower() or "",
            "cvc_pens":       cvc_count,
            "cvc_pen_pts":    cvc_pts_raw,
            "inactive_pens":  inactive_count,
            "inactive_pen_pts": inactive_count * 9,
            "site_pts":       _int(row.get("Site Points")),
            "gw_pts_pens":    _int(row.get("GW Points (With Pens)")),
            "running_total":  _int(row.get("Total")),
        }

    return result


def fetch_antifpl(team_id: int) -> dict[int, dict]:
    url = ANTIFPL_URL.format(id=team_id)
    log.info("    Anti-FPL -> %s", url)
    r = _get(url)
    if r is None:
        log.warning("    Anti-FPL fetch failed for id=%d", team_id)
        return {}

    if "application/json" in r.headers.get("content-type", ""):
        try:
            raw = r.json()
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass

    return parse_antifpl_table(r.text)


# ── Picks cache ───────────────────────────────────────────────────────────────
def load_picks_cache() -> dict:
    try:
        with open(PICKS_CACHE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_picks_cache(cache: dict) -> None:
    with open(PICKS_CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)
    log.info("Picks cache saved -> %s", PICKS_CACHE)


def compute_gw_picks(team_id: int, gw: int, live_pts: dict[int, int],
                     player_names: dict[str, str]) -> dict:
    """Fetch picks for one team/GW and compute bench-bumming + walking-through-traffic stats."""
    data = fetch_json(f"{FPL_BASE}/entry/{team_id}/event/{gw}/picks/")
    if not data:
        return {}

    picks       = data.get("picks", [])
    auto_subs   = data.get("automatic_subs", [])
    auto_in_set  = {s["element_in"]  for s in auto_subs}
    auto_out_set = {s["element_out"] for s in auto_subs}
    name_of  = lambda eid: player_names.get(str(eid), f"#{eid}")
    pts_of   = lambda eid: live_pts.get(eid, 0)

    captain  = next((p for p in picks if p["is_captain"]), None)

    # After auto-subs the API shows FINAL positions: auto_in players move to pos ≤11,
    # auto_out players move to pos >11.  Reconstruct original starting XI / bench.
    starters    = [p for p in picks if p["position"] <= 11]
    final_bench = [p for p in picks if p["position"] >  11]

    # best/worst pick: lowest/highest scorer in the FINAL starting XI
    starter_pts = [(p["element"], pts_of(p["element"])) for p in starters]
    best_pick   = min(starter_pts, key=lambda x: x[1])
    worst_pick  = max(starter_pts, key=lambda x: x[1])

    # bench bummings: pts scored by players who auto-subbed IN from the bench
    bench_bumming_pts = sum(pts_of(p["element"]) for p in starters if p["element"] in auto_in_set)

    # Reconstruct original bench (for display):
    # = auto_in players (came from bench, now at pos ≤11)
    # + final bench players not in auto_out_set (they stayed on bench the whole GW)
    orig_bench = sorted(
        [p for p in starters    if p["element"] in auto_in_set] +
        [p for p in final_bench if p["element"] not in auto_out_set],
        key=lambda p: p["position"],
    )
    bench_players = [
        {
            "name":       name_of(p["element"]),
            "pts":        pts_of(p["element"]),
            "autoSubbed": p["element"] in auto_in_set,
        }
        for p in orig_bench
    ]

    # Walking through traffic: highest-scoring original bench player who did NOT auto-sub on
    non_subbed = [
        {"name": name_of(p["element"]), "pts": pts_of(p["element"])}
        for p in final_bench if p["element"] not in auto_out_set
    ]
    walking_thru_traffic = max(non_subbed, key=lambda x: x["pts"], default=None)

    return {
        "bestPick":            {"name": name_of(best_pick[0]),  "pts": best_pick[1]},
        "worstPick":           {"name": name_of(worst_pick[0]), "pts": worst_pick[1]},
        "captain": {
            "name":       name_of(captain["element"]) if captain else "?",
            "pts":        pts_of(captain["element"]) if captain else 0,
            "multiplier": captain.get("multiplier", 1) if captain else 1,
        },
        "benchBummingPts":     bench_bumming_pts,
        "benchPlayers":        bench_players,
        "walkingThruTraffic":  walking_thru_traffic,
    }


def update_picks_cache(teams: list[dict], player_names: dict[str, str],
                       cache: dict) -> dict:
    """Fetch picks for any (team, gw) pairs not already in the cache."""
    needed: dict[int, set[int]] = {}
    for team in teams:
        tid         = team["id"]
        cached_gws  = {int(k) for k in cache.get(str(tid), {})}
        team_gws    = {g["gw"] for g in team["gws"]}
        missing     = team_gws - cached_gws
        if missing:
            needed[tid] = missing

    if not needed:
        log.info("Picks cache is complete — nothing to fetch.")
        return cache

    all_missing_gws = sorted({gw for gws in needed.values() for gw in gws})
    log.info("Fetching live data for %d GWs...", len(all_missing_gws))

    live_cache: dict[int, dict[int, int]] = {}
    for gw in all_missing_gws:
        log.info("  Live GW%d...", gw)
        raw = fetch_json(f"{FPL_BASE}/event/{gw}/live/")
        if raw:
            live_cache[gw] = {
                e["id"]: e["stats"]["total_points"]
                for e in raw.get("elements", [])
            }
        time.sleep(0.3)

    total = sum(len(gws) for gws in needed.values())
    done  = 0
    for team in teams:
        tid = team["id"]
        if tid not in needed:
            continue
        for gw in sorted(needed[tid]):
            done += 1
            log.info("  [%d/%d] Picks — team=%d GW%d", done, total, tid, gw)
            picks = compute_gw_picks(tid, gw, live_cache.get(gw, {}), player_names)
            if picks:
                cache.setdefault(str(tid), {})[str(gw)] = picks
            time.sleep(0.3)

    return cache


def merge_picks_into_teams(teams: list[dict], cache: dict) -> None:
    """Merge picks cache data into each team's gw entries (in-place)."""
    for team in teams:
        tid_cache = cache.get(str(team["id"]), {})
        for g in team["gws"]:
            picks = tid_cache.get(str(g["gw"]), {})
            if picks:
                g.update(picks)


# ── Rank computation ──────────────────────────────────────────────────────────
def compute_cum_ranks(teams: list[dict]) -> dict[int, dict[int, int]]:
    max_gw = max((g["gw"] for t in teams for g in t["gws"]), default=0)
    ranks: dict[int, dict[int, int]] = {t["id"]: {} for t in teams}
    for gw in range(1, max_gw + 1):
        totals = [
            (t["id"], next((g["total"] for g in t["gws"] if g["gw"] == gw), None))
            for t in teams
        ]
        totals = [(tid, tot) for tid, tot in totals if tot is not None]
        for pos, (tid, _) in enumerate(sorted(totals, key=lambda x: x[1]), 1):
            ranks[tid][gw] = pos
    return ranks


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Anti-FPL data scraper")
    parser.add_argument(
        "--picks", "-p", action="store_true",
        help="Fetch and cache GW picks data (incrementally — only new GWs fetched)"
    )
    args = parser.parse_args()

    log.info("=" * 52)
    log.info("Anti-FPL data fetch  -  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    if args.picks:
        log.info("Picks mode ON")
    log.info("=" * 52)

    # Bootstrap
    log.info("Fetching bootstrap-static...")
    bootstrap = fetch_json(f"{FPL_BASE}/bootstrap-static/")
    if bootstrap is None:
        log.error("Failed to fetch bootstrap -- aborting.")
        raise SystemExit(1)

    bootstrap_gw = bootstrap_current_gw(bootstrap)
    log.info("Bootstrap GW: %d", bootstrap_gw)

    player_names: dict[str, str] = {
        str(e["id"]): e.get("web_name", str(e["id"]))
        for e in bootstrap.get("elements", [])
    }
    log.info("Players indexed: %d", len(player_names))

    # Fetch all teams
    teams: list[dict] = []
    for i, tid in enumerate(TEAM_IDS):
        log.info("-- [%d/%d] id=%d --", i + 1, len(TEAM_IDS), tid)

        info     = team_info(tid)
        fpl_hist = team_history(tid)
        antifpl  = fetch_antifpl(tid)

        all_gws = sorted(set(fpl_hist) | set(antifpl))
        gws: list[dict] = []

        for gw in all_gws:
            fpl = fpl_hist.get(gw, {})
            afl = antifpl.get(gw, {})

            pts   = afl.get("gw_pts_pens") if afl.get("gw_pts_pens") is not None else fpl.get("fpl_net_pts")
            total = afl.get("running_total") if afl.get("running_total") is not None else fpl.get("fpl_total")
            chip  = afl.get("chip") or fpl.get("chip") or ""

            gws.append({
                "gw":              gw,
                "pts":             pts,
                "total":           total,
                "chip":            chip,
                # Anti-FPL scoring detail
                "mini_rank":       afl.get("gw_rank"),
                "last_rank":       afl.get("last_rank"),
                "site_pts":        afl.get("site_pts"),
                "cvc_pens":        afl.get("cvc_pens", 0),
                "cvc_pen_pts":     afl.get("cvc_pen_pts", 0),
                "inactive_pens":   afl.get("inactive_pens", 0),
                "inactive_pen_pts": afl.get("inactive_pen_pts", 0),
                "xfer_cost_pens":  afl.get("xfer_cost_pens", 0),
                "bank":            afl.get("bank"),
                "bank_pen":        afl.get("bank_pen", False),
                "team_value":      afl.get("team_value"),
                "transfers":       afl.get("transfers"),
                # FPL raw
                "fpl_raw_pts":     fpl.get("fpl_raw_pts"),
                "fpl_net_pts":     fpl.get("fpl_net_pts"),
                "fpl_xfer_cost":   fpl.get("fpl_xfer_cost"),
                "fpl_total":       fpl.get("fpl_total"),
                "fpl_gw_rank":     fpl.get("fpl_gw_rank"),
            })

        teams.append({
            **info,
            "color": COLORS[i % len(COLORS)],
            "gws":   gws,
        })

        latest_total = gws[-1]["total"] if gws else None
        log.info("  %s | %s | %d GWs | total=%s",
                 info["manager"], info["team_name"], len(gws), latest_total)
        time.sleep(0.4)

    # Current GW
    gw_now = max(
        (g["gw"] for t in teams for g in t["gws"]),
        default=bootstrap_gw,
    )
    log.info("Effective current GW: %d", gw_now)

    # Picks cache
    picks_cache = load_picks_cache()
    if args.picks:
        log.info("Updating picks cache...")
        picks_cache = update_picks_cache(teams, player_names, picks_cache)
        save_picks_cache(picks_cache)
    if picks_cache:
        merge_picks_into_teams(teams, picks_cache)
        log.info("Picks data merged into %d teams.", len(teams))

    # Cumulative standings
    log.info("Computing cumulative standings...")
    cum_ranks = compute_cum_ranks(teams)
    for team in teams:
        tid = team["id"]
        for g in team["gws"]:
            g["standing"] = cum_ranks.get(tid, {}).get(g["gw"])

    # Per-team summary
    for team in teams:
        gws = team["gws"]
        if not gws:
            team["summary"] = {}
            continue
        pts_list = [g["pts"] for g in gws if g["pts"] is not None]
        last_gw  = max(g["gw"] for g in gws)

        total_cvc_pens    = sum(g.get("cvc_pens", 0)    for g in gws)
        total_cvc_pen_pts = sum(g.get("cvc_pen_pts", 0)  for g in gws)
        total_inac_pens   = sum(g.get("inactive_pens", 0) for g in gws)
        total_inac_pen_pts = sum(g.get("inactive_pen_pts", 0) for g in gws)
        total_bank_pens   = sum(1 for g in gws if g.get("bank_pen"))
        total_xfer_pts    = sum(g.get("xfer_cost_pens") or 0 for g in gws)

        team["summary"] = {
            "total_points":       next((g["total"] for g in reversed(gws) if g["total"] is not None), None),
            "current_standing":   cum_ranks.get(team["id"], {}).get(gw_now),
            "last_gw":            last_gw,
            "best_gw":            min(pts_list) if pts_list else None,
            "worst_gw":           max(pts_list) if pts_list else None,
            "avg_gw":             round(sum(pts_list) / len(pts_list), 1) if pts_list else None,
            "total_cvc_pens":     total_cvc_pens,
            "total_cvc_pen_pts":  total_cvc_pen_pts,
            "total_inactive_pens":  total_inac_pens,
            "total_inactive_pen_pts": total_inac_pen_pts,
            "total_bank_pens":    total_bank_pens,
            "total_bank_pen_pts": total_bank_pens * 25,
            "total_xfer_pts":     total_xfer_pts,
            "total_pen_pts":      total_cvc_pen_pts + total_inac_pen_pts + total_bank_pens * 25 + total_xfer_pts,
            "chips_used": [
                {"gw": g["gw"], "chip": g["chip"]}
                for g in gws if g["chip"]
            ],
        }

    teams.sort(key=lambda t: t.get("summary", {}).get("total_points") or 99999)

    output = {
        "metadata": {
            "last_updated":     datetime.now(timezone.utc).isoformat(),
            "current_gw":       gw_now,
            "gw_in_progress":   gw_now > bootstrap_gw,
            "season":           "2025/26",
            "team_count":       len(teams),
        },
        "player_names": player_names,
        "teams": teams,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)
    log.info("Saved -> %s", OUTPUT_FILE)

    # Print standings + penalty summary
    sep = "-" * 72
    print()
    print(sep)
    print("  Anti-FPL League Standings")
    print(sep)
    print(f"  {'#':>2}  {'Manager':<22}  {'Total':>5}  {'Avg':>5}  {'CVC':>4}  {'Inac':>5}  {'Bank':>5}  {'Pens':>5}")
    print(sep)
    for team in teams:
        s    = team.get("summary", {})
        pos  = s.get("current_standing", "?")
        tot  = s.get("total_points", "?")
        avg  = s.get("avg_gw", "?")
        cvc  = s.get("total_cvc_pen_pts", 0)
        inac = s.get("total_inactive_pen_pts", 0)
        bnk  = s.get("total_bank_pen_pts", 0)
        pens = s.get("total_pen_pts", 0)
        print(f"  {pos:>2}.  {team['manager']:<22}  {tot:>5}  {avg:>5}  {cvc:>4}  {inac:>5}  {bnk:>5}  {pens:>5}")
    print(sep)
    print()


if __name__ == "__main__":
    main()
