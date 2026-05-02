#!/usr/bin/env python3
"""
Anti-FPL Mini League Data Scraper
Fetches from FPL API and antifpl.pythonanywhere.com, writes data.json.

Primary scoring comes from the Anti-FPL site (includes C/VC and inactive-player
penalties). FPL API supplies team names, chip usage, and the player name index.
"""

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
FPL_BASE    = "https://fantasy.premierleague.com/api"
ANTIFPL_URL = "https://antifpl.pythonanywhere.com/antifpl/manager/{id}/"
OUTPUT_FILE = "data.json"

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
    """Safe int conversion; returns None for blank/invalid values."""
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
    """Return {gw: fpl_record} from the FPL history endpoint."""
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
            "fpl_raw_pts":    raw_pts,
            "fpl_net_pts":    raw_pts - xfer_cost,
            "fpl_xfer_cost":  xfer_cost,
            "fpl_total":      gw["total_points"],
            "fpl_gw_rank":    gw.get("rank"),
            "chip":           chips.get(event, ""),
        }
    return result


# ── Anti-FPL scraper ─────────────────────────────────────────────────────────
# Expected table headers:
# GW Rank | Last Rank | Gameweek | Team Value | Bank | Transfers |
# Transfer Cost | Chip | C/VC Pens | Inactive Players | Last GW |
# Site Points | GW Points (With Pens) | Total | (team link)

def parse_antifpl_table(html: str) -> dict[int, dict]:
    """Parse the main GW history table from the Anti-FPL manager page.

    Returns {gw_number: record_dict}.
    """
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")
    if not tables:
        return {}

    # Use the first table — it's always the GW history
    tbl = tables[0]
    headers = [th.get_text(" ", strip=True) for th in tbl.find_all("th")]
    if not headers:
        return {}

    result: dict[int, dict] = {}
    for tr in tbl.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if not cells or len(cells) < len(headers) - 1:
            continue

        row = dict(zip(headers, cells))

        # Extract GW number from "1 ( Table / Stats )" format
        gw_cell = row.get("Gameweek", "")
        m = re.match(r"^(\d+)", gw_cell.strip())
        if not m:
            continue
        gw = int(m.group(1))

        result[gw] = {
            "gw_rank":        _int(row.get("GW Rank")),
            "last_rank":      _int(row.get("Last Rank")),
            "team_value":     _int(row.get("Team Value")),
            "bank":           _int(row.get("Bank")),
            "transfers":      _int(row.get("Transfers")),
            "xfer_cost_pens": _int(row.get("Transfer Cost")),
            "chip":           row.get("Chip", "").strip().lower() or "",
            "cvc_pens":       _int(row.get("C/VC Pens")),
            "inactive_pens":  _int(row.get("Inactive Players")),
            "last_gw_total":  _int(row.get("Last GW")),
            "site_pts":       _int(row.get("Site Points")),
            "gw_pts_pens":    _int(row.get("GW Points (With Pens)")),
            "running_total":  _int(row.get("Total")),
        }

    return result


def fetch_antifpl(team_id: int) -> dict[int, dict]:
    """Fetch and parse the Anti-FPL manager page. Returns {gw: record}."""
    url = ANTIFPL_URL.format(id=team_id)
    log.info("    Anti-FPL -> %s", url)
    r = _get(url)
    if r is None:
        log.warning("    Anti-FPL fetch failed for id=%d", team_id)
        return {}

    if "application/json" in r.headers.get("content-type", ""):
        # Future-proof: if the site ever switches to a JSON API
        try:
            raw = r.json()
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass

    return parse_antifpl_table(r.text)


# ── Rank computation ──────────────────────────────────────────────────────────
def compute_cum_ranks(teams: list[dict]) -> dict[int, dict[int, int]]:
    """Return {team_id: {gw: standing}} based on Anti-FPL cumulative total."""
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
    log.info("=" * 52)
    log.info("Anti-FPL data fetch  -  %s", datetime.now().strftime("%Y-%m-%d %H:%M"))
    log.info("=" * 52)

    # Bootstrap (player names + current GW)
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

        info       = team_info(tid)
        fpl_hist   = team_history(tid)      # {gw: fpl_record}
        antifpl    = fetch_antifpl(tid)     # {gw: antifpl_record}

        # Determine GWs played — prefer Anti-FPL (may include GW not yet in bootstrap)
        all_gws = sorted(set(fpl_hist) | set(antifpl))

        gws: list[dict] = []
        for gw in all_gws:
            fpl = fpl_hist.get(gw, {})
            afl = antifpl.get(gw, {})

            # Anti-FPL score is authoritative; fall back to FPL net pts
            pts   = afl.get("gw_pts_pens") if afl.get("gw_pts_pens") is not None else fpl.get("fpl_net_pts")
            total = afl.get("running_total") if afl.get("running_total") is not None else fpl.get("fpl_total")

            # Chip: prefer Anti-FPL source (it shows the official chip name)
            chip = afl.get("chip") or fpl.get("chip") or ""

            gws.append({
                "gw":           gw,
                "pts":          pts,
                "total":        total,
                "chip":         chip,
                # Anti-FPL specific
                "mini_rank":    afl.get("gw_rank"),
                "cvc_pens":     afl.get("cvc_pens"),
                "inactive_pens": afl.get("inactive_pens"),
                "xfer_cost_pens": afl.get("xfer_cost_pens"),
                "site_pts":     afl.get("site_pts"),
                "team_value":   afl.get("team_value"),
                "bank":         afl.get("bank"),
                "transfers":    afl.get("transfers"),
                # FPL raw data
                "fpl_raw_pts":  fpl.get("fpl_raw_pts"),
                "fpl_net_pts":  fpl.get("fpl_net_pts"),
                "fpl_xfer_cost": fpl.get("fpl_xfer_cost"),
                "fpl_total":    fpl.get("fpl_total"),
                "fpl_gw_rank":  fpl.get("fpl_gw_rank"),
            })

        teams.append({
            **info,
            "color": COLORS[i % len(COLORS)],
            "gws":   gws,
        })

        latest_total = gws[-1]["total"] if gws else None
        log.info("  %s | %s | %d GWs | anti_total=%s  fpl_total=%s",
                 info["manager"], info["team_name"], len(gws),
                 latest_total, fpl_hist.get(all_gws[-1], {}).get("fpl_total") if all_gws else None)

        time.sleep(0.4)

    # True current GW = max GW we have data for
    gw_now = max(
        (g["gw"] for t in teams for g in t["gws"]),
        default=bootstrap_gw,
    )
    log.info("Effective current GW: %d", gw_now)

    # Compute cumulative standings (Anti-FPL totals)
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
        last_gw = max(g["gw"] for g in gws)
        team["summary"] = {
            "total_points":     next((g["total"] for g in reversed(gws) if g["total"] is not None), None),
            "current_standing": cum_ranks.get(team["id"], {}).get(gw_now),
            "last_gw":          last_gw,
            "best_gw":          min(pts_list) if pts_list else None,
            "worst_gw":         max(pts_list) if pts_list else None,
            "avg_gw":           round(sum(pts_list) / len(pts_list), 1) if pts_list else None,
            "total_cvc_pens":   sum(g["cvc_pens"] or 0 for g in gws),
            "total_inactive_pens": sum(g["inactive_pens"] or 0 for g in gws),
            "chips_used": [
                {"gw": g["gw"], "chip": g["chip"]}
                for g in gws if g["chip"]
            ],
        }

    # Sort by current Anti-FPL standing
    teams.sort(key=lambda t: t.get("summary", {}).get("total_points") or 99999)

    output = {
        "metadata": {
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "current_gw":   gw_now,
            "season":       "2025/26",
            "team_count":   len(teams),
        },
        "player_names": player_names,
        "teams": teams,
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2, ensure_ascii=False)
    log.info("Saved -> %s", OUTPUT_FILE)

    # Print standings
    sep = "-" * 58
    print()
    print(sep)
    print("  Anti-FPL League Standings  (lower = better)")
    print(sep)
    print(f"  {'#':>2}  {'Manager':<22}  {'Total':>5}  {'Avg GW':>6}  {'C/VC Pens':>9}")
    print(sep)
    for team in teams:
        s   = team.get("summary", {})
        pos = s.get("current_standing", "?")
        tot = s.get("total_points", "?")
        avg = s.get("avg_gw", "?")
        cvc = s.get("total_cvc_pens", 0)
        print(f"  {pos:>2}.  {team['manager']:<22}  {tot:>5}  {avg:>6}  {cvc:>9}")
    print(sep)
    print()


if __name__ == "__main__":
    main()
