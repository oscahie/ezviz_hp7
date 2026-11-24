"""Hang up a ringing call through a cloud talk session, and log why when it fails."""
from __future__ import annotations

import logging
import struct
from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.ezviz_hp7.api import Hp7Api, _local_wall_epoch
from custom_components.ezviz_hp7.button import EzvizHp7Button
from custom_components.ezviz_hp7.pylocalapi import talk

SERIAL = "BW0000000-BW0000001"
OK_REPLY = b"restult: OK;audio_code_type: AAC;audio_code_value=7;"
REFUSED_REPLY = b"timestamp=&uuid=&cln=0"


class FakeSocket:
    """Plays back server bytes; ``None`` in the script is a read timeout."""

    def __init__(self, script: list[bytes | None]) -> None:
        self.script = list(script)
        self.sent = b""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, size: int) -> bytes:
        if not self.script:
            return b""
        item = self.script.pop(0)
        if item is None:
            raise TimeoutError
        out, rest = item[:size], item[size:]
        if rest:
            self.script.insert(0, rest)
        return out


def _reply(body: bytes, code: int = 0) -> bytes:
    return struct.pack(">IIII", 2, 1, code, len(body)) + body


def _connect(sock: FakeSocket):
    return lambda addr, timeout: sock


def test_start_message_frames_the_talk_url():
    sock = FakeSocket([_reply(OK_REPLY), b"\x00\x00\x00\x03audio"])
    talk.hang_up("1.2.3.4", 8664, SERIAL, "tok", connect=_connect(sock))

    kind, four, zero, size = struct.unpack(">IIII", sock.sent[:16])
    body = sock.sent[16:].decode()
    assert (kind, four, zero, size) == (1, 4, 0, len(body))
    assert (
        f'"url":"tts://1.2.3.4:8664/talk?dev={SERIAL}&chann=0&encodetype=1'
        "&authssn=tok&cln=3&talktype=3&tls=0\""
    ) in body


def test_refused_session_raises_with_the_server_code():
    sock = FakeSocket([_reply(REFUSED_REPLY, code=14)])
    with pytest.raises(talk.TalkError, match="code 14"):
        talk.hang_up("h", 1, SERIAL, "tok", connect=_connect(sock))


def test_device_not_joining_raises():
    """Closing before the doorbell joins leaves the call ringing."""
    sock = FakeSocket([_reply(OK_REPLY), None])
    with pytest.raises(talk.TalkError, match="did not join"):
        talk.hang_up("h", 1, SERIAL, "tok", connect=_connect(sock))


def _api(ring_age: float | None) -> Hp7Api:
    api = Hp7Api(username="someone@example.com", password="secret", token={"username": "u"})
    api._client = MagicMock()
    api._client._api_get_pagelist.return_value = {
        SERIAL: {"externalIp": "1.2.3.4", "domain": "tts.example", "port": "8664"}
    }
    messages = [] if ring_age is None else [{"time": int((_local_wall_epoch() - ring_age) * 1000)}]
    api._client._request_json.return_value = {"message": messages}
    return api


@pytest.fixture
def talk_ok(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "custom_components.ezviz_hp7.api.get_vtdu_token_v2", lambda client: {"tokens": ["tok"]}
    )
    monkeypatch.setattr(talk, "hang_up", lambda *a, **k: calls.append(a))
    return calls


def test_hang_up_during_a_call(talk_ok, caplog):
    api = _api(ring_age=5)
    with caplog.at_level(logging.INFO):
        assert api.hang_up_call(SERIAL) is True
    assert talk_ok == [("1.2.3.4", 8664, SERIAL, "tok")]
    assert "call hung up" in caplog.text


def test_hang_up_without_a_recent_call_warns(talk_ok, caplog):
    api = _api(ring_age=600)
    with caplog.at_level(logging.WARNING):
        assert api.hang_up_call(SERIAL) is True
    assert "no doorbell call in the last" in caplog.text


def test_hang_up_failure_is_logged_and_returns_false(monkeypatch, caplog):
    api = _api(ring_age=5)
    monkeypatch.setattr(
        "custom_components.ezviz_hp7.api.get_vtdu_token_v2", lambda client: {"tokens": ["tok"]}
    )

    def refuse(*a, **k):
        raise talk.TalkError("talk server refused the session (code 14)")

    monkeypatch.setattr(talk, "hang_up", refuse)
    with caplog.at_level(logging.WARNING):
        assert api.hang_up_call(SERIAL) is False
    assert "hang up failed" in caplog.text
    assert "code 14" in caplog.text


async def test_button_raises_when_hang_up_fails(hass):
    api = MagicMock()
    api.hang_up_call.return_value = False
    button = EzvizHp7Button(hass, api, SERIAL, "HP7", "hang_up")
    with pytest.raises(HomeAssistantError) as err:
        await button.async_press()
    assert err.value.translation_key == "hang_up_failed"
