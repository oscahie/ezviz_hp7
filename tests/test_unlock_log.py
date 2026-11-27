"""Unlock log: rows from the message list, paging, websocket rows."""
from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

from homeassistant.setup import async_setup_component

from custom_components.ezviz_hp7 import unlock_log
from custom_components.ezviz_hp7.const import DOMAIN

SERIAL = "BK0000000-BK0000001"


def _msg(i, alarm_type, who=None, t=None):
    ext = {"alarmType": str(alarm_type)}
    if who:
        ext["customerType"] = who
    return {
        "msgId": f"m{i}",
        "subType": "2401",
        "time": t if t is not None else 1_000_000 + i,
        "timeStr": f"2026-11-27 13:{i % 60:02d}:00",
        "title": "x",
        "pic": f"https://pic/{i}",
        "picCrypt": 1,
        # The list sends ext as JSON text on some answers.
        "ext": json.dumps(ext) if i % 2 else ext,
    }


def test_entry_from_message():
    row = unlock_log.entry_from_message(_msg(1, 10242, "Oscar (F)"), lambda w: "unlock_face")
    assert row["lock"] == "door" and row["who"] == "Oscar (F)"
    assert row["category"] == "unlock_face" and row["crypt"] and row["id"] == "m1"
    assert unlock_log.entry_from_message(_msg(2, 10243, "Card"))["lock"] == "gate"
    assert unlock_log.entry_from_message(_msg(3, 10079)) is None


def test_fetch_pages_and_days():
    # Today: a full page of detections then one unlock; yesterday: unlocks.
    today = [_msg(i, 10079, t=5000 - i) for i in range(50)]
    today_p2 = [_msg(100, 10242, "Card", t=4000)]
    yesterday = [_msg(200 + i, 10242, "EZVIZ app", t=3000 - i) for i in range(12)]
    client = MagicMock()

    def answer(serial, limit, date, end_time):
        assert serial == SERIAL and limit == 50
        if date == "20261127":
            return {"message": today if end_time == "" else today_p2}
        if date == "20261126":
            return {"message": yesterday}
        return {"message": []}

    client.get_device_messages_list.side_effect = answer
    rows = unlock_log.fetch_unlock_log(client, SERIAL, now=datetime(2026, 11, 27, 12))
    assert len(rows) == 10
    assert rows[0]["id"] == "m100"  # newest first
    # Page 2 asked with the time of the last entry of page 1.
    assert client.get_device_messages_list.call_args_list[1].kwargs["end_time"] == str(5000 - 49)
    # Stopped once 10 were found: no third day read.
    assert all(c.kwargs["date"] != "20261125" for c in client.get_device_messages_list.call_args_list)


async def test_websocket_rows_have_signed_pictures(hass, hass_ws_client):
    assert await async_setup_component(hass, "http", {})
    await unlock_log.async_setup_unlock_log(hass)
    coordinator = MagicMock()
    coordinator.data = {
        "unlock_log": [
            unlock_log.entry_from_message(_msg(1, 10242, "Oscar (F)"), lambda w: "unlock_face"),
            unlock_log.entry_from_message(_msg(2, 10243, "Card")),
        ]
    }
    hass.data.setdefault(DOMAIN, {})["entry1"] = {"serial": SERIAL, "coordinator": coordinator}
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "ezviz_hp7/unlock_log"})
    res = await client.receive_json()
    assert res["success"]
    rows = res["result"]["unlocks"]
    assert [r["lock"] for r in rows] == ["gate", "door"]
    assert rows[1]["picture"].startswith("/api/ezviz_hp7/unlock_picture/entry1/m1?authSig=")
    assert "pic" not in rows[0]  # the EZVIZ link stays on the server

    await client.send_json({"id": 2, "type": "ezviz_hp7/unlock_log", "serial": "OTHER"})
    assert (await client.receive_json())["result"]["unlocks"] == []


async def test_picture_view(hass, hass_client, aioclient_mock):
    assert await async_setup_component(hass, "http", {})
    await unlock_log.async_setup_unlock_log(hass)
    coordinator = MagicMock()
    coordinator.data = {"unlock_log": [unlock_log.entry_from_message(_msg(1, 10242, "Card"))]}
    hass.data.setdefault(DOMAIN, {})["entry1"] = {"serial": SERIAL, "coordinator": coordinator}
    aioclient_mock.get("https://pic/1", content=b"\xff\xd8jpeg")
    client = await hass_client()
    resp = await client.get("/api/ezviz_hp7/unlock_picture/entry1/m1")
    assert resp.status == 200 and await resp.read() == b"\xff\xd8jpeg"
    # Served from memory the second time.
    await client.get("/api/ezviz_hp7/unlock_picture/entry1/m1")
    assert aioclient_mock.call_count == 1
    assert (await client.get("/api/ezviz_hp7/unlock_picture/entry1/nope")).status == 404
