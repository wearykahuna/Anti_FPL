"""
fpl_api.py — Pure FPL API wrapper.
====================================
Single responsibility: make HTTP calls to the Fantasy Premier League API
and return JSON. No DB writes, no business logic, no scoring.

All functions return None on failure rather than raising, so callers can
decide how to handle missing data.
"""

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

FPL_BASE = "https://fantasy.premierleague.com/api"

_session = requests.Session()
_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})


# ── Low-level HTTP ────────────────────────────────────────────────────────────

def _get(url: str, retries: int = 3) -> Optional[dict]:
    """Internal: GET with retries and exponential backoff."""
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            log.warning("FPL fetch attempt %d/%d failed [%s]: %s",
                        attempt + 1, retries, url, exc)
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_bootstrap() -> Optional[dict]:
    """All players, teams, events, game settings for current season."""
    return _get(f"{FPL_BASE}/bootstrap-static/")


def fetch_fixtures(gw: Optional[int] = None) -> Optional[list[dict]]:
    """All fixtures for the season, or just for one GW if specified."""
    url = f"{FPL_BASE}/fixtures/"
    if gw is not None:
        url += f"?event={gw}"
    return _get(url)


def fetch_team_info(team_id: int) -> Optional[dict]:
    """Manager name, team name, joined date for an FPL team."""
    return _get(f"{FPL_BASE}/entry/{team_id}/")


def fetch_team_history(team_id: int) -> Optional[dict]:
    """GW-by-GW history + chips used for an FPL team."""
    return _get(f"{FPL_BASE}/entry/{team_id}/history/")


def fetch_picks(team_id: int, gw: int) -> Optional[dict]:
    """Squad + captain + auto-subs + active chip for a team for one GW."""
    return _get(f"{FPL_BASE}/entry/{team_id}/event/{gw}/picks/")


def fetch_live(gw: int) -> Optional[dict]:
    """Live player stats (minutes, points, etc.) for one GW."""
    return _get(f"{FPL_BASE}/event/{gw}/live/")


def fetch_league_standings_page(league_id: int, page: int = 1) -> Optional[dict]:
    """One page of standings for a classic league."""
    return _get(
        f"{FPL_BASE}/leagues-classic/{league_id}/standings/"
        f"?page_standings={page}"
    )


def fetch_all_league_team_ids(league_id: int) -> list[int]:
    """Walk all pages of a classic league and return every team_id."""
    ids: list[int] = []
    page = 1
    while True:
        data = fetch_league_standings_page(league_id, page)
        if not data:
            break
        standings = data.get("standings", {})
        results = standings.get("results", [])
        if not results:
            break
        ids.extend(r["entry"] for r in results)
        if not standings.get("has_next"):
            break
        page += 1
        time.sleep(0.3)
    log.info("League %d: %d teams across %d pages", league_id, len(ids), page)
    return ids
