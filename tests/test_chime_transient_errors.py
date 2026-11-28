"""ChimeMusic reads: scattered cloud hiccups must not take a monitor offline."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.ezviz_hp7 import api as api_mod
from custom_components.ezviz_hp7.api import Hp7Api

MONITOR = "BK0000000"
GOOD = {"valueInfo": '{"doorbell":1,"pir":0,"volume":2,"doorbell_enable":1,"pir_enable":0}'}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(api_mod.time, "monotonic", c)
    return c


def _api():
    api = Hp7Api("u", "p")
    api._client = MagicMock()
    api._token = {"session": "x"}
    return api


def test_scattered_failures_do_not_disable(clock):
    api = _api()
    for _ in range(5):
        api._client.get_dev_config.side_effect = ConnectionError("reset")
        clock.t += 3600
        api.get_chime_state(MONITOR)
        api._client.get_dev_config.side_effect = None
        api._client.get_dev_config.return_value = GOOD
        clock.t += 3600
        assert api.get_chime_state(MONITOR) is True
    assert MONITOR not in api._chime_dead_serials


def test_single_failure_keeps_last_value(clock):
    api = _api()
    api._client.get_dev_config.return_value = GOOD
    assert api.get_chime_state(MONITOR) is True
    clock.t += 120
    api._client.get_dev_config.side_effect = ConnectionError("reset")
    assert api.get_chime_state(MONITOR) is True
    clock.t += 3600
    assert api.get_chime_state(MONITOR) is None


def test_repeated_failures_back_off_then_retry(clock):
    api = _api()
    api._client.get_dev_config.side_effect = Exception("")
    for _ in range(3):
        clock.t += 30
        api.get_chime_state(MONITOR)
    calls = api._client.get_dev_config.call_count
    clock.t += 30
    api.get_chime_state(MONITOR)
    assert api._client.get_dev_config.call_count == calls
    clock.t += api_mod.CHIME_RETRY_BACKOFF
    api._client.get_dev_config.side_effect = None
    api._client.get_dev_config.return_value = GOOD
    assert api.get_chime_state(MONITOR) is True


def test_no_access_answer_is_cached(clock):
    api = _api()
    api._client.get_dev_config.side_effect = Exception("403 Client Error: Forbidden")
    api.get_chime_state(MONITOR)
    assert MONITOR in api._chime_dead_serials


def test_backoff_grows_and_is_capped(clock):
    api = _api()
    api._client.get_dev_config.side_effect = Exception("timeout")
    waits = []
    for _ in range(12):
        api.get_chime_state(MONITOR)
        wait = api._chime_retry_after.get(MONITOR, clock.t) - clock.t
        waits.append(wait)
        clock.t += max(wait, 1.0)
    grown = [w for w in waits if w > 0]
    assert grown[0] == api_mod.CHIME_RETRY_BACKOFF
    assert grown == sorted(grown)
    assert max(grown) == api_mod.CHIME_RETRY_BACKOFF_MAX


def test_problem_reported_after_an_hour_and_cleared(clock):
    api = _api()
    api._client.get_dev_config.side_effect = Exception("timeout")
    api.get_chime_state(MONITOR)
    assert api.chime_problem(MONITOR) is None
    clock.t += api_mod.CHIME_ISSUE_AFTER + 1
    problem = api.chime_problem(MONITOR)
    assert problem["kind"] == "failing" and problem["minutes"] >= 60
    api._client.get_dev_config.side_effect = None
    api._client.get_dev_config.return_value = GOOD
    api._chime_retry_after.clear()
    assert api.get_chime_state(MONITOR) is True
    assert api.chime_problem(MONITOR) is None


def test_refused_is_reported_at_once(clock):
    api = _api()
    api._client.get_dev_config.side_effect = Exception("403 Client Error: Forbidden")
    api.get_chime_state(MONITOR)
    assert api.chime_problem(MONITOR)["kind"] == "refused"
