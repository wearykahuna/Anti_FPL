"""
Regression tests for the live-scoring latency fixes.

These guard two bugs that were silent in production — no error, no failed run,
just stale scores:

  1. worker.py used the slow 300s idle interval for the "settling" state, even
     though scheduler.tick() runs the full live scoring path for it. Settling
     is the window right after a block of simultaneous kickoffs ends, when
     bonus points land — the worst possible moment to drop to a 5-min cadence.

  2. fpl_api._get retried every failure including permanent 4xx, so one 404
     cost 3 requests plus 3s of sleep inside the tick, and a 429 got no
     Retry-After handling at all.

Run from the repo root:
    pytest Backend/tests
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tasks"))

import fpl_api
from worker import FAST_STATES, IDLE_INTERVAL_S, LIVE_INTERVAL_S


# ── worker cadence ───────────────────────────────────────────────────────────

def _interval_for(state):
    """Mirrors the interval choice in worker.main()'s loop."""
    return LIVE_INTERVAL_S if state in FAST_STATES else IDLE_INTERVAL_S


def test_settling_uses_the_fast_interval():
    """The original bug: scheduler does full live work for 'settling', but the
    worker slept 300s through it."""
    assert _interval_for("settling") == LIVE_INTERVAL_S


def test_every_working_state_is_fast():
    # Every state for which scheduler.tick() does scoring work.
    for state in ("live", "settling", "provisional", "finalize"):
        assert _interval_for(state) == LIVE_INTERVAL_S, state


def test_idle_is_the_only_slow_state():
    assert _interval_for("idle") == IDLE_INTERVAL_S
    assert "idle" not in FAST_STATES


def test_live_interval_is_60s():
    assert LIVE_INTERVAL_S == 60


# ── fpl_api._get ─────────────────────────────────────────────────────────────

def _response(status, payload=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.json.return_value = payload if payload is not None else {}
    r.raise_for_status.side_effect = None
    return r


def test_404_returns_none_without_retrying():
    """A pre-deadline 404 from fetch_picks is routine — retrying it 3x burned
    3 requests and 3s of sleep per team, every tick."""
    with patch.object(fpl_api._session, "get", return_value=_response(404)) as get, \
         patch.object(fpl_api.time, "sleep") as sleep:
        assert fpl_api._get("http://x/") is None
        assert get.call_count == 1, "404 must not be retried"
        sleep.assert_not_called()


def test_other_4xx_also_permanent():
    with patch.object(fpl_api._session, "get", return_value=_response(403)) as get:
        assert fpl_api._get("http://x/") is None
        assert get.call_count == 1


def test_429_honours_retry_after_then_succeeds():
    responses = [_response(429, headers={"Retry-After": "2"}), _response(200, {"ok": True})]
    with patch.object(fpl_api._session, "get", side_effect=responses), \
         patch.object(fpl_api.time, "sleep") as sleep:
        assert fpl_api._get("http://x/") == {"ok": True}
        sleep.assert_called_once_with(2.0)


def test_retry_after_is_clamped():
    r = _response(429, headers={"Retry-After": "99999"})
    assert fpl_api._retry_after_seconds(r, default=1) == fpl_api.MAX_RETRY_AFTER


def test_retry_after_http_date_falls_back_to_default():
    r = _response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert fpl_api._retry_after_seconds(r, default=4) == 4


def test_transient_exception_is_still_retried():
    """Genuine network failures must keep their retry behaviour."""
    responses = [ConnectionError("boom"), _response(200, {"ok": True})]
    with patch.object(fpl_api._session, "get", side_effect=responses), \
         patch.object(fpl_api.time, "sleep"):
        assert fpl_api._get("http://x/") == {"ok": True}


def test_success_returns_payload():
    with patch.object(fpl_api._session, "get", return_value=_response(200, {"a": 1})):
        assert fpl_api._get("http://x/") == {"a": 1}
