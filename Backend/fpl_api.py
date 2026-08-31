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

# (connect, read). A single call must never eat a large slice of the live tick:
# the old flat timeout=20 with 3 blind retries could block for 63s inside a
# 60s tick.
DEFAULT_TIMEOUT = (5, 10)
MAX_TOTAL_S     = 25    # hard ceiling on one logical fetch, retries included
MAX_RETRY_AFTER = 30    # never honour an absurd Retry-After


def _retry_after_seconds(response, default: float) -> float:
    """Parse a Retry-After header (seconds form only), clamped."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return default
    try:
        return max(0.0, min(float(raw), MAX_RETRY_AFTER))
    except (TypeError, ValueError):
        return default    # HTTP-date form — not worth parsing, back off normally


def _get(url: str, retries: int = 3, timeout=DEFAULT_TIMEOUT) -> Optional[dict]:
    """
    Internal: GET with bounded retries.

    Retries only what is actually retryable. A 4xx is permanent — retrying a
    404 three times just burns 3 requests and 3s of sleep, and a pre-deadline
    404 from fetch_picks is an expected, routine response.

    Returns None on failure (the contract every caller relies on).
    """
    started = time.monotonic()
    for attempt in range(retries):
        try:
            r = _session.get(url, timeout=timeout)

            if r.status_code == 429:
                wait = _retry_after_seconds(r, default=2 ** attempt)
                log.warning("FPL rate-limited (429) [%s] — waiting %.1fs", url, wait)
                if attempt < retries - 1:
                    time.sleep(wait)
                    continue
                return None

            if 400 <= r.status_code < 500:
                # Permanent. 404 is routine (picks before a deadline), so log
                # it quietly — it used to spam a warning per team per tick.
                logger = log.info if r.status_code == 404 else log.warning
                logger("FPL %d (no retry) [%s]", r.status_code, url)
                return None

            r.raise_for_status()
            return r.json()

        except Exception as exc:
            log.warning("FPL fetch attempt %d/%d failed [%s]: %s",
                        attempt + 1, retries, url, exc)

        if time.monotonic() - started > MAX_TOTAL_S:
            log.warning("FPL fetch budget (%ds) exhausted [%s] — giving up.", MAX_TOTAL_S, url)
            return None
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    return None


# ── Public API ────────────────────────────────────────────────────────────────

def fetch_bootstrap() -> Optional[dict]:
    """All players, teams, events, game settings for current season."""
    return _get(f"{FPL_BASE}/bootstrap-static/")


def detect_season(bootstrap: dict) -> Optional[str]:
    """
    Derive the '2026/27'-style season string from bootstrap's events list.
    The PL season always starts in August, so the earliest event deadline's
    year is the season's start year — stable all season regardless of
    today's date, and rolls over automatically once FPL publishes new events.
    """
    deadlines = [e["deadline_time"] for e in bootstrap.get("events", []) if e.get("deadline_time")]
    if not deadlines:
        return None
    start_year = min(int(d[:4]) for d in deadlines)
    return f"{start_year}/{str(start_year + 1)[-2:]}"


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
