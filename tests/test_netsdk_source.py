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

def _pes(sid: int, payload: bytes, pts: int | None = None) -> bytes:
    if pts is None:
        hdr = b"\x80\x00\x00"
    else:
        hdr = bytes([
            0x80, 0x80, 5,
            0x21 | ((pts >> 29) & 0x0E), (pts >> 22) & 0xFF,
            0x01 | ((pts >> 14) & 0xFE), (pts >> 7) & 0xFF,
            0x01 | ((pts << 1) & 0xFE),
        ])
    body = hdr + payload
    return b"\x00\x00\x01" + bytes([sid]) + len(body).to_bytes(2, "big") + body


PACK_HEADER = b"\x00\x00\x01\xba" + b"\x44" * 9 + b"\xf8"


def _frame(pts: int, parts: int = 1, sid: int = 0xE0) -> bytes:
    """One pack: a PES with the PTS, then continuation PES without one."""
    out = PACK_HEADER + _pes(sid, b"f%d" % pts, pts)
    for i in range(1, parts):
        out += _pes(sid, b"c%d.%d" % (pts, i))
    return out


PACK = PACK_HEADER


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
    stream = _frame(6000) + _frame(12000) + PACK_HEADER
    chunks = [header + stream[:2], stream[2:30], stream[30:]]
    out = b"".join(_source_with(chunks).iter_payloads())
    assert out == _frame(6000) + _frame(12000)


def test_payloads_drop_the_old_hp7_ring_buffer_replay():
    # Measured on a CS-HP7-R100: the device loops its ring buffer, so the
    # same frames come again and again with the PTS jumping back.
    frames = {p: _frame(p * 6000, parts=3) for p in range(1, 8)}
    audio = {p: _frame(p * 6000 + 100, sid=0xC0) for p in range(1, 8)}
    sent = []
    for lap in ([1, 2, 3], [1, 2, 3, 4], [2, 3, 4, 5, 6], [1, 2, 3, 4, 5, 6, 7]):
        for p in lap:
            sent += [frames[p], audio[p]]
    stream = b"".join(sent) + PACK_HEADER
    # Split at awkward places to exercise buffering.
    chunks = [b"IMKH" + b"\x00" * 60] + [
        stream[i:i + 37] for i in range(0, len(stream), 37)
    ]
    out = b"".join(_source_with(chunks).iter_payloads())
    assert out == b"".join(frames[p] + audio[p] for p in range(1, 8))


def test_replay_filter_handles_the_33_bit_pts_wrap():
    from custom_components.ezviz_hp7._pes import PsReplayFilter

    f = PsReplayFilter()
    a, b, c = (1 << 33) - 6000, 0, 6000
    out = f.feed(_frame(a) + _frame(b) + _frame(a) + _frame(c) + PACK_HEADER)
    assert out == _frame(a) + _frame(b) + _frame(c)


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



def test_decrypt_key_falls_back_to_the_verification_code():
    # Unchanged password: the label code alone decrypts.
    assert _relay("local_sdk", verification_code="CODE")._configured_key == "CODE"
    # Changed password: the password option wins for decryption only.
    relay = _relay("local_sdk", encryption_key="PASS", verification_code="CODE")
    assert relay._configured_key == "PASS"
    assert relay._login_code == "CODE"
    assert _relay("local")._configured_key == ""

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


def test_replay_filter_keeps_every_pack_of_a_large_frame_once():
    from custom_components.ezviz_hp7._pes import PsReplayFilter

    # A keyframe split over three packs that all carry the same PTS.
    key = [PACK_HEADER + _pes(0xE0, b"k%d" % i, 6000) for i in range(3)]
    nxt = _frame(12000)
    f = PsReplayFilter()
    # Lap 1 ends after the first two packs; lap 2 replays all of it.
    out = f.feed(key[0] + key[1] + key[0] + key[1] + key[2] + nxt + PACK_HEADER)
    assert out == key[0] + key[1] + key[2] + nxt


def test_replay_filter_drops_the_pack_cut_short_at_the_ring_seam():
    from custom_components.ezviz_hp7._pes import PsReplayFilter

    old, cut, new = _frame(1500, parts=2), _frame(12000, parts=3), _frame(18000)
    # The ring lap restarts mid-way through `cut`: its last PES length now
    # runs into the replayed `old` pack, which must not slip through.
    stream = _frame(3000) + cut[:-3] + old + cut + new + PACK_HEADER
    f = PsReplayFilter()
    assert f.feed(stream) == _frame(3000) + cut + new
    assert f.bad_packs >= 1


def test_replay_filter_drops_a_pack_with_any_stale_pts():
    from custom_components.ezviz_hp7._pes import PsReplayFilter

    f = PsReplayFilter()
    first = _frame(12000) + _frame(12100, sid=0xC0)
    # New audio first, but the video in the same pack is stale.
    mixed = PACK_HEADER + _pes(0xC0, b"a", 18100) + _pes(0xE0, b"v", 6000)
    nxt = _frame(18000)
    assert f.feed(first + mixed + nxt + PACK_HEADER) == first + nxt
