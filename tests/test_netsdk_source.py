"""Port-8000 (HCNetSDK) LAN source: plan, payload framing and source selection."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from custom_components.ezviz_hp7 import live_camera
from custom_components.ezviz_hp7.live_camera import (
    Hp7StreamRelay,
    NetSdkLanSource,
    _PreStartedSource,
)
from custom_components.ezviz_hp7.pylocalapi.local_stream import (
    hcnetsdk_command_port_native_lan_live_view_plan,
)

PACK = b"\x00\x00\x01\xba" + b"\x44" * 10


def test_plan_requests_the_given_channel():
    plan = hcnetsdk_command_port_native_lan_live_view_plan(channel=33)
    media = next(step for step in plan.steps if step.media_socket)
    assert media.control_templates[0].body_tail == bytes.fromhex(
        "000000210000000000000401"
    )


def test_plan_defaults_to_channel_one():
    plan = hcnetsdk_command_port_native_lan_live_view_plan()
    media = next(step for step in plan.steps if step.media_socket)
    assert media.control_templates[0].body_tail[:4] == (1).to_bytes(4, "big")


class _FakeSock:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def recv(self, _size):
        return self._chunks.pop(0) if self._chunks else b""


def _source_with(chunks) -> NetSdkLanSource:
    src = NetSdkLanSource(MagicMock(), "BK0000000-BK0000001", "ABCDEF")
    src._sock = _FakeSock(chunks)
    return src


def test_payloads_skip_the_imkh_header_and_stop_at_eof():
    header = b"IMKH" + b"\x00" * 60
    # The pack start code straddles two reads.
    chunks = [header + PACK[:2], PACK[2:] + b"rest", b"more"]
    out = b"".join(_source_with(chunks).iter_payloads())
    assert out == PACK + b"rest" + b"more"


def test_start_without_code_fails_before_touching_the_network():
    api = MagicMock()
    with pytest.raises(RuntimeError):
        NetSdkLanSource(api, "BK0000000-BK0000001", "").start()
    api.get_local_ip.assert_not_called()


def _relay(source: str, **kwargs) -> Hp7StreamRelay:
    return Hp7StreamRelay(
        api=MagicMock(), serial="BK0000000-BK0000001", stream_source=source, **kwargs
    )


def test_login_code_falls_back_to_the_encryption_key():
    assert _relay("local_sdk", encryption_key="KEY")._login_code == "KEY"
    assert (
        _relay("local_sdk", encryption_key="KEY", verification_code="CODE")._login_code
        == "CODE"
    )


@pytest.fixture
def started(monkeypatch):
    """Record which sources were started; CPD7 always fails, port 8000 works."""
    calls: list[str] = []

    def cpd7_start(self):
        calls.append("cpd7")
        raise RuntimeError("invalid LAN AES key from CAS")

    def sdk_start(self):
        calls.append("sdk")
        return self

    monkeypatch.setattr(live_camera.Cpd7LanSource, "start", cpd7_start)
    monkeypatch.setattr(NetSdkLanSource, "start", sdk_start)
    monkeypatch.setattr(
        live_camera, "open_cloud_stream", lambda *a, **k: calls.append("cloud") or "CLOUD"
    )
    return calls


async def test_local_sdk_source_opens_the_port_8000_source(started):
    src = await _relay("local_sdk", verification_code="CODE")._open_source(
        asyncio.get_running_loop(), MagicMock()
    )
    assert isinstance(src, NetSdkLanSource)


async def test_auto_tries_port_8000_after_cpd7_when_a_code_is_set(started):
    src = await _relay("auto", verification_code="CODE")._open_source(
        asyncio.get_running_loop(), MagicMock()
    )
    assert started == ["cpd7", "sdk"]
    assert isinstance(src, _PreStartedSource)


async def test_auto_without_a_code_goes_straight_to_cloud(started):
    src = await _relay("auto")._open_source(asyncio.get_running_loop(), MagicMock())
    assert started == ["cpd7", "cloud"]
    assert src == "CLOUD"


class _ScrambledAfter:
    """PesParser stand-in: reports 8 scrambled packets after ``n`` feeds."""

    def __init__(self, n):
        self._n = n
        self.scrambled_packets = 0

    def feed(self, _data):
        self._n -= 1
        if self._n <= 0:
            self.scrambled_packets = 8
        return []


def _lan_relay(**kwargs) -> Hp7StreamRelay:
    relay = _relay("local_sdk", **kwargs)
    relay._active_lan = True
    return relay


def test_startup_hold_decides_before_releasing_anything(monkeypatch):
    monkeypatch.setattr(live_camera, "PesParser", lambda: _ScrambledAfter(3))
    relay = _lan_relay(encryption_key="KEY")

    def payloads():
        for i in range(5):
            yield bytes([i])

    calls = []
    monkeypatch.setattr(
        Hp7StreamRelay, "_check_scrambled", lambda self, parser: calls.append(parser)
    )
    gen = relay._hold_until_scrambling_known(payloads())
    assert next(gen) == b"\x00"
    assert len(calls) == 1  # scrambling handled before the first payload
    assert list(gen) == [b"\x01", b"\x02", b"\x03", b"\x04"]


def test_startup_hold_is_skipped_without_a_configured_key():
    relay = _lan_relay()
    assert list(relay._hold_until_scrambling_known([b"a", b"b"])) == [b"a", b"b"]


def test_decrypted_output_waits_for_the_encryption_mode(monkeypatch):
    from custom_components.ezviz_hp7.pylocalapi import stream

    evidence = iter([(5, 5), (7, 7)])
    monkeypatch.setattr(
        stream, "hevc_ps_non_irap_slices_look_clear", lambda chunk: next(evidence)
    )
    monkeypatch.setattr(stream, "mpeg_ps_decryptable_prefix_length", len)
    modes = []

    def fake_decrypt(chunk, key, *, nalu_header_size, encrypted_nal_types):
        modes.append(encrypted_nal_types is not None)
        return chunk

    monkeypatch.setattr(stream, "decrypt_hikvision_ps_video", fake_decrypt)
    relay = _lan_relay(encryption_key="KEY")
    relay._decrypt_key = "KEY"
    assert relay._maybe_decrypt(PACK) == b""
    assert relay._maybe_decrypt(PACK + b"x") == PACK + PACK + b"x"
    assert modes == [True, True]  # both chunks decrypted as IRAP-only
