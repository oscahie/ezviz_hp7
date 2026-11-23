"""ChimeMusic volume range (0-100, like the EZVIZ app)."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from custom_components.ezviz_hp7.api import Hp7Api

DOOR = "BW0000000-BW0000001"
BLOB = {"doorbell": 9, "pir": 0, "volume": 7, "doorbell_enable": 0, "pir_enable": 0}


def _api() -> Hp7Api:
    api = Hp7Api(username="someone@example.com", password="secret", token={"username": "u"})
    api._client = MagicMock()
    api._chime_config_cache[DOOR] = (dict(BLOB), float("inf"))
    return api


def _sent(api: Hp7Api) -> dict:
    return json.loads(api._client._session.put.call_args.kwargs["params"]["value"])


def test_volume_above_seven_is_written_as_is_and_keeps_other_fields():
    api = _api()
    assert api.set_chime_volume(DOOR, 63) is True
    assert _sent(api) == {**BLOB, "volume": 63}


def test_volume_is_clamped_to_100():
    api = _api()
    api.set_chime_volume(DOOR, 101)
    assert _sent(api)["volume"] == 100


def test_volume_write_drops_the_cached_blob():
    api = _api()
    api.set_chime_volume(DOOR, 30)
    assert DOOR not in api._chime_config_cache
