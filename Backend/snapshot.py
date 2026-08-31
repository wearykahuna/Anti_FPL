"""
Anti-FPL Snapshot Export
=========================
Reads the current season's data from Supabase and writes a JSON snapshot
to snapshots/{season}/{gw}_{date}.json in the repo.

Snapshots contain:
  - teams table (all registered teams + eligibility)
  - gw_scores table (full scoring breakdown for all teams, all GWs)

These two tables are sufficient to fully reconstruct the leaderboard
at any point in time. Player scores and selections can be re-fetched
from the FPL API if ever needed.

Usage:
    python snapshot.py               # snapshot current season
    python snapshot.py --gw 36       # label snapshot as GW36 explicitly
    python snapshot.py --season 2025/26
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from postgrest import SyncPostgrestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
# Reuse db.py's auto-detected season rather than keeping a second, independent
# constant here. This used to be a hardcoded "2025/26" that never got bumped —
# so every scheduled snapshot silently wrote last season's (empty) data while
# the live site had moved on to 2026/27. db.py already solves this once, by
# asking the FPL API at import time; importing it here means there is exactly
# one season constant in the whole codebase to go stale, not two.
from db import DEFAULT_SEASON

# ── Config ────────────────────────────────────────────────────────────────────
SNAPSHOTS_DIR    = Path(__file__).parent.parent / "snapshots"
PAGE_SIZE        = 1000   # Supabase returns max 1000 rows per request

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── Supabase client ───────────────────────────────────────────────────────────

def get_supabase() -> SyncPostgrestClient:
    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_KEY in .env")
        sys.exit(1)
    return SyncPostgrestClient(
        f"{url}/rest/v1",
        headers={
            "apikey":        key,
            "Authorization": f"Bearer {key}",
        },
    )


# ── Paginated fetch ───────────────────────────────────────────────────────────

def fetch_all(sb: SyncPostgrestClient, table: str, filters: dict) -> list[dict]:
    """
    Fetch all rows from a table with pagination.
    Supabase returns max 1000 rows per request — this handles larger tables.
    """
    rows  = []
    start = 0
    while True:
        q = sb.from_(table).select("*")
        for col, val in filters.items():
            q = q.eq(col, val)
        result = q.range(start, start + PAGE_SIZE - 1).execute()
        batch  = result.data or []
        rows.extend(batch)
        log.info("  %s: fetched %d rows (total so far: %d)", table, len(batch), len(rows))
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


# ── Change detection ──────────────────────────────────────────────────────────

def _data_fingerprint(teams: list[dict], gw_scores: list[dict]) -> str:
    """
    Canonical JSON of the snapshot payload (metadata excluded, rows sorted)
    so two snapshots of identical data compare equal regardless of row order.
    """
    key_t = lambda r: (r.get("team_id") or 0)
    key_s = lambda r: (r.get("team_id") or 0, r.get("gw") or 0)
    return json.dumps(
        {"teams": sorted(teams, key=key_t), "gw_scores": sorted(gw_scores, key=key_s)},
        sort_keys=True, default=str,
    )


def _latest_snapshot_path(season_dir: Path) -> Path | None:
    if not season_dir.is_dir():
        return None
    files = sorted(season_dir.glob("gw*.json"))
    return files[-1] if files else None


def unchanged_since_last_snapshot(season_dir: Path, teams: list[dict],
                                  gw_scores: list[dict]) -> bool:
    """True if the latest existing snapshot holds identical teams + gw_scores."""
    latest = _latest_snapshot_path(season_dir)
    if latest is None:
        return False
    try:
        with open(latest, encoding="utf-8") as f:
            prev = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    prev_fp = _data_fingerprint(prev.get("teams", []), prev.get("gw_scores", []))
    return prev_fp == _data_fingerprint(teams, gw_scores)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--season", default=DEFAULT_SEASON, help="Season string e.g. 2025/26")
    parser.add_argument("--gw",     type=int,               help="GW number label for filename")
    args = parser.parse_args()

    season = args.season
    log.info("=" * 56)
    log.info("Anti-FPL Snapshot  —  season=%s", season)
    log.info("=" * 56)

    sb = get_supabase()

    # ── Fetch teams ───────────────────────────────────────────────────────────
    log.info("Fetching teams...")
    teams = fetch_all(sb, "teams", {"season": season})
    log.info("  %d team rows fetched", len(teams))

    # ── Fetch gw_scores ───────────────────────────────────────────────────────
    log.info("Fetching gw_scores...")
    gw_scores = fetch_all(sb, "gw_scores", {"season": season})
    log.info("  %d gw_score rows fetched", len(gw_scores))

    if not teams or not gw_scores:
        log.error("No data found for season %s — aborting snapshot.", season)
        sys.exit(1)

    # ── Skip if nothing changed since the last snapshot ───────────────────────
    season_dir_check = SNAPSHOTS_DIR / season.replace("/", "-")
    if unchanged_since_last_snapshot(season_dir_check, teams, gw_scores):
        log.info("Data identical to latest snapshot — skipping write.")
        return

    # ── Determine current GW from data ────────────────────────────────────────
    max_gw = max((r["gw"] for r in gw_scores), default=0)
    gw_label = args.gw or max_gw
    log.info("Snapshot GW label: GW%d", gw_label)

    # ── Build output ──────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    output = {
        "metadata": {
            "season":       season,
            "gw":           gw_label,
            "generated_at": now.isoformat(),
            "team_count":   len(teams),
            "score_rows":   len(gw_scores),
        },
        "teams":     teams,
        "gw_scores": gw_scores,
    }

    # ── Write snapshot file ───────────────────────────────────────────────────
    # Season folder: "2025/26" → "2025-26" (safe for filesystem)
    season_dir = SNAPSHOTS_DIR / season.replace("/", "-")
    season_dir.mkdir(parents=True, exist_ok=True)

    date_str  = now.strftime("%Y-%m-%d")
    filename  = f"gw{gw_label:02d}_{date_str}.json"
    out_path  = season_dir / filename

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False, default=str)

    log.info("Snapshot saved -> %s", out_path)
    log.info("  Size: %.1f KB", out_path.stat().st_size / 1024)
    log.info("=" * 56)
    log.info("Done.")


if __name__ == "__main__":
    main()
