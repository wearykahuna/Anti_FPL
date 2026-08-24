"""
Unit tests for the scheduler's fixture-state classifier (Backend/tasks/scheduler.py).

Run from the repo root:
    pytest Backend/tests
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tasks"))

from tasks.scheduler import _classify

NOW = datetime.now(timezone.utc)


def fixture(started=False, finished=False, finished_provisional=False, kickoff_delta_min=None):
    ko = (NOW + timedelta(minutes=kickoff_delta_min)) if kickoff_delta_min is not None else NOW
    return {
        "started": started,
        "finished": finished,
        "finished_provisional": finished_provisional,
        "kickoff_time": ko.isoformat(),
    }


def test_classify_live_when_fixture_in_play():
    fixtures = [fixture(started=True)]
    state, _ = _classify(fixtures)
    assert state == "live"


def test_classify_live_when_kickoff_imminent():
    fixtures = [fixture(kickoff_delta_min=10)]  # within IMMINENT_MINUTES=15
    state, _ = _classify(fixtures)
    assert state == "live"


def test_classify_idle_when_kickoff_far_away():
    fixtures = [fixture(kickoff_delta_min=120)]
    state, _ = _classify(fixtures)
    assert state == "idle"


def test_classify_provisional_when_all_fixtures_finished_provisional():
    fixtures = [
        fixture(started=True, finished_provisional=True),
        fixture(started=True, finished_provisional=True),
    ]
    state, _ = _classify(fixtures)
    assert state == "provisional"


def test_classify_idle_when_all_fixtures_officially_finished():
    fixtures = [fixture(started=True, finished=True, finished_provisional=True)]
    state, _ = _classify(fixtures)
    assert state == "idle"


def test_classify_settling_when_some_provisional_and_one_not_started():
    # Exactly the GW1 scenario this was built for: most fixtures are done
    # (finished_provisional), but one hasn't kicked off yet and isn't imminent.
    fixtures = [
        fixture(started=True, finished_provisional=True),
        fixture(started=True, finished_provisional=True),
        fixture(kickoff_delta_min=300),  # hours away — not live, not imminent
    ]
    state, reason = _classify(fixtures)
    assert state == "settling"
    assert "finished_provisional" in reason

def test_classify_live_takes_priority_over_settling():
    # Some fixtures already settled, but another is live right now —
    # should still classify as "live" (fast ticking), not "settling".
    fixtures = [
        fixture(started=True, finished_provisional=True),
        fixture(started=True),  # currently in play
    ]
    state, _ = _classify(fixtures)
    assert state == "live"
