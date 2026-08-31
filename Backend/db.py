"""
db.py — Supabase data access layer.
=====================================
Single responsibility: read from and write to Supabase. No scoring logic.
Tasks compose calls to fpl_api + db + scoring.

The one deliberate exception: DEFAULT_SEASON is auto-detected from the FPL
API at import time (see _detect_default_season below), so the season never
needs a manual code bump when a new one starts. Falls back to a hardcoded
string if the API is unreachable when this module loads.

All functions take SEASON-aware filters where relevant.
"""

import logging
import os
import sys
from typing import Optional

from dotenv import load_dotenv
from postgrest import SyncPostgrestClient

log = logging.getLogger(__name__)

_FALLBACK_SEASON = "2026/27"
BATCH_SIZE       = 500
PAGE_SIZE        = 1000
CLIENT_TIMEOUT_S = 15    # per-request cap; see get_client()


def _detect_default_season() -> str:
    """Best-effort auto-detect from the live FPL API; falls back on any failure."""
    try:
        from fpl_api import fetch_bootstrap, detect_season
        bootstrap = fetch_bootstrap()
        season = detect_season(bootstrap) if bootstrap else None
        if season:
            return season
    except Exception:
        log.warning("Season auto-detect failed — using fallback %s", _FALLBACK_SEASON, exc_info=True)
    return _FALLBACK_SEASON


DEFAULT_SEASON = _detect_default_season()


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
        # postgrest defaults to 120s — twice the whole live tick interval, so
        # one hung query would stall the loop past its next two ticks.
        timeout=CLIENT_TIMEOUT_S,
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


def update_rows(table: str, rows: list[dict], key_cols: list[str]) -> None:
    """
    Update each row individually, matched by key_cols — unlike upsert(), this
    can never INSERT a new row, so it's safe for supplementary fields (like
    gw_scores.team_value) that should only ever patch a row another task
    already created. An upsert() on a stale/missing key would try to INSERT
    a placeholder row and trip NOT NULL constraints on unrelated columns;
    a plain UPDATE just matches zero rows silently instead.
    One HTTP request per row — fine for the small, infrequent call sites
    this is meant for (not the live scoring loop).
    """
    if not rows:
        return
    sb = get_client()
    for row in rows:
        keys  = {k: row[k] for k in key_cols}
        patch = {k: v for k, v in row.items() if k not in key_cols}
        q = sb.from_(table).update(patch)
        for k, v in keys.items():
            q = q.eq(k, v)
        q.execute()


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


def load_excluded_team_ids(path: str | None = None) -> set[int]:
    """
    Manually excluded team_ids, read from Backend/excluded_teams.txt.

    A `teams.eligible = false` tombstone is NOT self-protecting: any later
    score_new_team or create_mini_league run upserts `eligible` back to
    whatever the gate computes, silently resurrecting a purged team. This file
    is the durable record — it is version-controlled, reviewable in a diff, and
    survives a wipe_season().

    Format: one team_id per line, `#` starts a comment. Missing file = no
    exclusions.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "excluded_teams.txt")
    if not os.path.exists(path):
        return set()

    excluded: set[int] = set()
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            try:
                excluded.add(int(line))
            except ValueError:
                log.warning("excluded_teams.txt:%d — ignoring unparseable line %r", lineno, line)
    return excluded


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

def is_live_window_open(season: str = DEFAULT_SEASON,
                        fixtures: list[dict] | None = None,
                        current_gw: int | None = None) -> tuple[bool, int | None]:
    """
    Returns (is_open, current_gw).

    `fixtures` / `current_gw` let a caller that already holds freshly-synced
    values pass them in instead of re-reading them. The scheduler syncs both at
    the top of every tick, so threading them through removes two round trips
    per call — and this is called twice per tick.

    The live window is open when:
      - There's a current GW (gameweeks.is_current=true, is_finished=false)
      - AND at least one fixture in that GW has started=true, finished=false
        OR has started=false and kickoff_time has passed (catches kickoff window)
        OR is finished_provisional but not yet officially finished (may still
        get late stat corrections — e.g. bonus points — and its players'
        scores shouldn't sit stale just because a LATER fixture in the same
        GW hasn't kicked off yet)

    Returns (False, None) if no current GW or no active fixtures.
    """
    from datetime import datetime, timezone

    current = current_gw if current_gw is not None else get_current_gw(season)
    if current is None:
        return False, None

    if fixtures is None:
        fixtures = get_fixtures(season, gw=current)
    if not fixtures:
        return False, current

    now = datetime.now(timezone.utc)
    any_active = False
    for f in fixtures:
        if f.get("finished"):
            continue
        if f.get("finished_provisional"):
            any_active = True
            break
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


# ── Provisional window helper ─────────────────────────────────────────────────

def is_provisional_window_open(season: str = DEFAULT_SEASON,
                               fixtures: list[dict] | None = None,
                               current_gw: int | None = None) -> tuple[bool, int | None]:
    """
    Returns (is_open, current_gw).

    Open when all GW fixtures are finished/finished_provisional but at least one
    hasn't been officially confirmed yet — the gap between final whistle and
    FPL's official GW confirmation (can be several hours).
    """
    current = current_gw if current_gw is not None else get_current_gw(season)
    if current is None:
        return False, None

    if fixtures is None:
        fixtures = get_fixtures(season, gw=current)
    if not fixtures:
        return False, current

    all_done  = all(f.get("finished") or f.get("finished_provisional") for f in fixtures)
    any_prov  = any(f.get("finished_provisional") and not f.get("finished") for f in fixtures)
    return all_done and any_prov, current


# ── Finalize helper ───────────────────────────────────────────────────────────

def has_unfinalized_scores(season: str, gw: int) -> bool:
    """
    True if any gw_scores row for this GW is still flagged is_live or
    is_provisional — i.e. the GW has never had its final pass.
    """
    sb   = get_client()
    rows = (sb.from_("gw_scores")
              .select("id")
              .eq("season", season)
              .eq("gw", gw)
              .or_("is_live.eq.true,is_provisional.eq.true")
              .limit(1)
              .execute().data or [])
    return bool(rows)


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
