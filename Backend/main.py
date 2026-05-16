"""
Anti-FPL Backend — FastAPI app with smart polling.
====================================================
Runs as a persistent process on Render. Two responsibilities:

  1. Smart poller — checks FPL fixtures and decides polling frequency
     dynamically based on whether games are being played right now.

  2. HTTP API — a few simple endpoints:
       GET /              health check / status
       GET /trigger       manually run a migration cycle (auth required)
       GET /poll-status   inspect current poller state

The poller adapts as follows:
  - Live matches now:        poll every 120 seconds
  - Pre-match (next 2 hrs):  poll every 15 minutes
  - GW week, no games today: poll every 60 minutes
  - Off-season / quiet:      poll every 6 hours

Reads SUPABASE_URL and SUPABASE_KEY from environment variables.
"""

import asyncio
import logging
import os
import sys
import subprocess
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Header
from dotenv import load_dotenv

# Reuse the existing scoring & API helpers
from anti_fpl_scoring import fetch_bootstrap

# ── Config ────────────────────────────────────────────────────────────────────
load_dotenv()

POLL_LIVE_S     = 120        # 2 mins during live matches
POLL_PRE_S      = 15  * 60   # 15 mins in pre-match window
POLL_GW_QUIET_S = 60  * 60   # 1 hour during GW weeks but no games today
POLL_IDLE_S     = 6   * 60 * 60  # 6 hours when nothing's happening

PRE_MATCH_WINDOW_HRS = 2     # how far ahead to consider "pre-match"

# Auth token for triggering migrations manually via API
TRIGGER_TOKEN = os.environ.get("TRIGGER_TOKEN", "")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Poller state (in-memory, just for /poll-status visibility) ───────────────
poller_state = {
    "status":         "starting",
    "last_run":       None,
    "last_run_mode":  None,
    "next_run":       None,
    "next_interval":  None,
    "run_count":      0,
    "error_count":    0,
    "last_error":     None,
}


# ── Decide poll frequency from FPL fixtures ──────────────────────────────────

def fetch_fixtures(gw: int) -> list[dict] | None:
    """Fetch fixtures for a given GW."""
    import requests
    try:
        r = requests.get(
            f"https://fantasy.premierleague.com/api/fixtures/?event={gw}",
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("Fixtures fetch failed: %s", exc)
        return None


def decide_poll_interval(bootstrap: dict) -> tuple[int, str]:
    """
    Decide how many seconds to wait until the next poll cycle.
    Returns (seconds, mode_label).
    """
    now = datetime.now(timezone.utc)
    events = bootstrap.get("events", [])

    # Find current GW (the one in progress, or the next upcoming)
    current = next((e for e in events if e.get("is_current")), None)
    upcoming = next((e for e in events if e.get("is_next")),    None)

    target_gw = (current or upcoming)
    if not target_gw:
        return POLL_IDLE_S, "idle (no GW found)"

    # Fetch fixtures for the active GW
    fixtures = fetch_fixtures(target_gw["id"]) or []
    if not fixtures:
        return POLL_IDLE_S, f"idle (no fixtures for GW{target_gw['id']})"

    # Are any matches currently being played?
    live_now = any(
        f.get("started") and not f.get("finished")
        for f in fixtures
    )
    if live_now:
        return POLL_LIVE_S, "live (matches in play)"

    # Any matches starting within the next PRE_MATCH_WINDOW_HRS?
    horizon = now + timedelta(hours=PRE_MATCH_WINDOW_HRS)
    pre_match = any(
        f.get("kickoff_time") and
        now <= datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00")) <= horizon
        for f in fixtures
    )
    if pre_match:
        return POLL_PRE_S, f"pre-match (GW{target_gw['id']} within {PRE_MATCH_WINDOW_HRS}h)"

    # Any matches today at all (even later)?
    today  = now.date()
    today_games = any(
        f.get("kickoff_time") and
        datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00")).date() == today
        for f in fixtures
    )
    if today_games:
        return POLL_GW_QUIET_S, f"gw quiet (GW{target_gw['id']} games later today)"

    # GW active but no games today — idle-ish
    if current:
        return POLL_GW_QUIET_S, f"gw active (no games today)"

    return POLL_IDLE_S, "idle"


# ── Run a migration cycle (executes migrate_to_supabase.py as subprocess) ────

def run_migration() -> tuple[bool, str]:
    """
    Run migrate_to_supabase.py as a subprocess. Returns (success, log_summary).
    Running as subprocess keeps memory clean and isolates failures.
    """
    log.info("Running migration cycle...")
    try:
        result = subprocess.run(
            [sys.executable, "migrate_to_supabase.py"],
            capture_output=True,
            text=True,
            timeout=15 * 60,   # 15 min hard timeout
            env=os.environ.copy(),
        )
        ok = result.returncode == 0
        # Last 1000 chars of stdout for logging
        tail = (result.stdout or "")[-1000:]
        if not ok:
            log.error("Migration failed (exit %d): %s", result.returncode, result.stderr[-500:])
        return ok, tail
    except subprocess.TimeoutExpired:
        log.error("Migration timed out after 15 minutes")
        return False, "timeout"
    except Exception as exc:
        log.exception("Migration crashed: %s", exc)
        return False, str(exc)


# ── Background poller loop ───────────────────────────────────────────────────

async def poller_loop():
    log.info("Poller loop starting...")
    poller_state["status"] = "running"
    while True:
        try:
            bootstrap = fetch_bootstrap()
            if not bootstrap:
                log.warning("Bootstrap fetch failed — backing off 5 min")
                poller_state["next_interval"] = 300
                poller_state["next_run"] = (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat()
                await asyncio.sleep(300)
                continue

            interval, mode = decide_poll_interval(bootstrap)
            log.info("Poll mode: %s — running migration, next poll in %ds", mode, interval)

            ok, _ = run_migration()

            poller_state["last_run"]      = datetime.now(timezone.utc).isoformat()
            poller_state["last_run_mode"] = mode
            poller_state["next_interval"] = interval
            poller_state["next_run"]      = (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()
            poller_state["run_count"]    += 1
            if not ok:
                poller_state["error_count"] += 1
                poller_state["last_error"]   = "Migration subprocess failed"

            await asyncio.sleep(interval)

        except Exception as exc:
            log.exception("Poller loop error: %s", exc)
            poller_state["error_count"] += 1
            poller_state["last_error"]   = str(exc)
            await asyncio.sleep(300)   # back off 5 mins on unexpected errors


# ── FastAPI app ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the background poller as soon as the app boots
    task = asyncio.create_task(poller_loop())
    log.info("Poller task scheduled.")
    yield
    task.cancel()


app = FastAPI(title="Anti-FPL Backend", lifespan=lifespan)


@app.get("/")
def root():
    return {
        "status":  "ok",
        "service": "anti-fpl-backend",
        "season":  "2025/26",
        "now":     datetime.now(timezone.utc).isoformat(),
    }


@app.get("/poll-status")
def poll_status():
    return poller_state


@app.post("/trigger")
def trigger_migration(x_trigger_token: str = Header(default="")):
    """Manually trigger a migration cycle. Requires TRIGGER_TOKEN header."""
    if not TRIGGER_TOKEN or x_trigger_token != TRIGGER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    ok, tail = run_migration()
    return {"success": ok, "log_tail": tail}


@app.get("/healthz")
def healthz():
    """Lightweight health endpoint for uptime checks / keepalive pings."""
    return {"ok": True}