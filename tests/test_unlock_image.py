"""Last Unlock image: follows the newest unlock record's picture."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.ezviz_hp7.image import Hp7UnlockImage, unlock_picture_url

SERIAL = "BK0000000-BK0000001"


def test_picture_url_fields():
    assert unlock_picture_url({"picUrl": "https://x/1.jpg"}) == "https://x/1.jpg"
    assert unlock_picture_url({"picUrl": "", "pic": "https://x/2.jpg"}) == "https://x/2.jpg"
    assert unlock_picture_url({"picUrlGroup": ["", "https://x/3.jpg"]}) == "https://x/3.jpg"
    assert unlock_picture_url({"picUrl": ""}) is None
    assert unlock_picture_url(None) is None


def _unlock(t, url):
    return {
        "unlock": {
            "time": t,
            "user": "Oscar",
            "category": "unlock_palm",
            "record": {"picUrl": url, "alarmStartTimeStr": "2026-11-27 11:02:03"},
        }
    }


async def test_updates_only_on_a_new_unlock(hass):
    coordinator = MagicMock()
    coordinator.data = _unlock(1, "https://x/1.jpg")
    entity = Hp7UnlockImage(hass, coordinator, SERIAL, "HP7 Pro", MagicMock(data={}, options={}))
    first = entity.image_last_updated
    assert first is not None and first.hour == 11
    assert entity._url == "https://x/1.jpg"
    entity._cached = b"jpeg"

    assert not entity._update_from_data()
    assert entity._cached == b"jpeg"

    coordinator.data = _unlock(2, "https://x/2.jpg")
    assert entity._update_from_data()
    assert entity._url == "https://x/2.jpg" and entity._cached is None
    assert entity.extra_state_attributes["user"] == "Oscar"
