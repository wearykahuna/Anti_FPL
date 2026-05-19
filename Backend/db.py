"""
db.py — Supabase data access layer.
=====================================
Single responsibility: read from and write to Supabase. No FPL API calls,
no scoring logic. Tasks compose calls to fpl_api + db + scoring.

All functions take SEASON-aware filters where relevant.
"""

import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from postgrest import SyncPostgrestClient

log = logging.getLogger(__name__)

DEFAULT_SEASON = "2025/26"
BATCH_SIZE     = 500
PAGE_SIZE      = 1000


# ── Client ────────────────────────────────────────────────────────────────────

_client: Optional[SyncPostgrestClient] = None


def get_client() -> SyncPostgrestClient:
    """Return a cached Supabase client. Reads .env on first call."""
    global _client
    if _client is not None:
        return _client

    load_dotenv()
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        log.error("Missing SUPABASE_URL or SUPABASE_KEY in environment.")
        sys.exit(1)

    _client = SyncPostgrestClient(
        f"{url}/rest/v1",
        headers={
            "apikey":        key,
            "Authorization": f"Bearer {key}",
        },
    )
    log.info("Connected to Supabase: %s", url)
    return _client


# ── Generic helpers ───────────────────────────────────────────────────────────

def upsert(table: str, rows: list[dict], on_conflict: str) -> None:
    """Upsert rows in batches to avoid hitting payload limits."""
    if not rows:
        return
    sb = get_client()
    total = len(rows)
    for i in range(0, total, BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        sb.from_(table).upsert(batch, on_conflict=on_conflict).execute()
        log.info("  %s: %d / %d rows upserted",
                 table, min(i + BATCH_SIZE, total), total)


def select_all(table: str, filters: dict | None = None,
               select: str = "*") -> list[dict]:
    """
    Fetch all rows from a table with pagination.
    Filters dict applies .eq() to each column.
    """
    sb = get_client()
    rows: list[dict] = []
    start = 0
    while True:
        q = sb.from_(table).select(select)
        for col, val in (filters or {}).items():
            q = q.eq(col, val)
        result = q.range(start, start + PAGE_SIZE - 1).execute()
        batch = result.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


def delete_where(table: str, filters: dict) -> None:
    """Delete rows matching all filters (.eq for each)."""
    sb = get_client()
    q = sb.from_(table).delete()
    for col, val in filters.items():
        q = q.eq(col, val)
    q.execute()


# ── Domain getters ────────────────────────────────────────────────────────────

def get_teams(season: str = DEFAULT_SEASON, eligible_only: bool = True) -> list[dict]:
    """Return all teams for the season."""
    filters = {"season": season}
    if eligible_only:
        filters["eligible"] = True
    return select_all("teams", filters)


def get_team_ids(season: str = DEFAULT_SEASON, eligible_only: bool = True) -> list[int]:
    """Return just the team IDs."""
    return [t["team_id"] for t in get_teams(season, eligible_only)]


def get_gw_scores(season: str = DEFAULT_SEASON,
                  gw: int | None = None,
                  team_id: int | None = None) -> list[dict]:
    """Flexible gw_scores fetcher."""
    filters: dict = {"season": season}
    if gw is not None:
        filters["gw"] = gw
    if team_id is not None:
        filters["team_id"] = team_id
    return select_all("gw_scores", filters)


def get_player_scores(season: str = DEFAULT_SEASON, gw: int | None = None) -> list[dict]:
    filters: dict = {"season": season}
    if gw is not None:
        filters["gw"] = gw
    return select_all("player_gw_scores", filters)


def get_selections(season: str = DEFAULT_SEASON,
                   gw: int | None = None,
                   team_id: int | None = None) -> list[dict]:
    filters: dict = {"season": season}
    if gw is not None:
        filters["gw"] = gw
    if team_id is not None:
        filters["team_id"] = team_id
    return select_all("team_gw_selections", filters)


def get_fixtures(season: str = DEFAULT_SEASON, gw: int | None = None) -> list[dict]:
    filters: dict = {"season": season}
    if gw is not None:
        filters["gw"] = gw
    return select_all("fixtures", filters)


def get_gameweeks(season: str = DEFAULT_SEASON) -> list[dict]:
    return select_all("gameweeks", {"season": season})


def get_current_gw(season: str = DEFAULT_SEASON) -> Optional[int]:
    """Return the GW number flagged as is_current=true, or None."""
    rows = select_all("gameweeks", {"season": season, "is_current": True})
    return rows[0]["gw"] if rows else None


def get_players_ref(season: str = DEFAULT_SEASON) -> list[dict]:
    """Reference data for all FPL players."""
    return select_all("players", {"season": season})


def get_team_anti_total(season: str, team_id: int, gw: int) -> int:
    """
    Return the anti_total for one team after a specific GW.
    Returns 0 if no row exists (e.g. team didn't play that GW).
    """
    rows = get_gw_scores(season=season, team_id=team_id, gw=gw)
    return rows[0].get("anti_total", 0) if rows else 0


# ── Live match window helper ──────────────────────────────────────────────────

def is_live_window_open(season: str = DEFAULT_SEASON) -> tuple[bool, int | None]:
    """
    Returns (is_open, current_gw).

    The live window is open when:
      - There's a current GW (gameweeks.is_current=true, is_finished=false)
      - AND at least one fixture in that GW has started=true, finished=false
        OR has started=false and kickoff_time has passed (catches kickoff window)

    Returns (False, None) if no current GW or no active fixtures.
    """
    from datetime import datetime, timezone

    current = get_current_gw(season)
    if current is None:
        return False, None

    fixtures = get_fixtures(season, gw=current)
    if not fixtures:
        return False, current

    now = datetime.now(timezone.utc)
    any_active = False
    for f in fixtures:
        if f.get("finished") or f.get("finished_provisional"):
            continue
        if f.get("started"):
            any_active = True
            break
        # Catch fixtures whose kickoff has passed but FPL hasn't marked started yet
        ko = f.get("kickoff_time")
        if ko:
            try:
                ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
                if ko_dt <= now:
                    any_active = True
                    break
            except ValueError:
                pass

    return any_active, current


# ── Cleanup helpers ───────────────────────────────────────────────────────────

def wipe_season(season: str = DEFAULT_SEASON,
                preserve_mini_leagues: bool = True) -> None:
    """Wipe scoring data for the season. Preserves mini-leagues by default."""
    log.warning("Wiping season data: %s", season)
    tables = [
        "gw_scores", "cup_fixtures",
        "player_gw_scores", "team_gw_selections",
        "teams", "players", "fixtures", "gameweeks",
    ]
    if not preserve_mini_leagues:
        tables.extend(["mini_league_members", "mini_leagues"])

    for tbl in tables:
        delete_where(tbl, {"season": season})
        log.info("  Cleared %s", tbl)
