"""Where a cold viewer's first chunk is cut (Hp7StreamRelay._fanout)."""
from __future__ import annotations

from custom_components.ezviz_hp7.live_camera import _from_keyframe

PACK = b"\x00\x00\x01\xba" + b"\x44" * 10
P_SLICE = b"\x00\x00\x00\x01\x41" + b"\x11" * 20
SPS_PPS_IDR = (
    b"\x00\x00\x00\x01\x67" + b"\x22" * 15
    + b"\x00\x00\x00\x01\x68" + b"\x33" * 4
    + b"\x00\x00\x00\x01\x65" + b"\x55" * 40
)


def _pes(es: bytes) -> bytes:
    hdr = b"\x80\x80\x05" + b"\x21\x00\x01\x00\x01"
    return b"\x00\x00\x01\xe0" + (len(hdr) + len(es)).to_bytes(2, "big") + hdr + es


def test_cuts_at_the_pack_in_front_of_the_keyframe():
    chunk = _pes(P_SLICE) + PACK + _pes(SPS_PPS_IDR)
    assert _from_keyframe(chunk) == PACK + _pes(SPS_PPS_IDR)


def test_keeps_the_pes_header_when_the_pack_is_in_an_earlier_chunk():
    # The decrypt path emits pack + PSM and the video PES run separately, so
    # the keyframe chunk can start straight on the PES. Cutting at the SPS
    # dropped the PES header and the viewer only got video a GOP later.
    keyframe_pes = _pes(SPS_PPS_IDR)
    assert _from_keyframe(_pes(P_SLICE) + keyframe_pes) == keyframe_pes
    assert _from_keyframe(keyframe_pes) == keyframe_pes


def test_bare_elementary_stream_is_cut_at_the_nal():
    assert _from_keyframe(P_SLICE + SPS_PPS_IDR) == SPS_PPS_IDR


def test_chunk_without_keyframe_is_unchanged():
    chunk = _pes(P_SLICE)
    assert _from_keyframe(chunk) == chunk
