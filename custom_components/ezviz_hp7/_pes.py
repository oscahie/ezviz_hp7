"""Incremental MPEG-PS PES splitter.

Used to split the VTM stream's MPEG-PS bytestream into per-stream_id payloads
so the video (H.264, stream_id 0xE0) and audio (AAC ADTS, stream_id 0xC0)
can be fed separately to ffmpeg — the standard ffmpeg mpeg demuxer
mis-identifies the audio PID as MP2 and rejects every packet.

Usage:

    parser = PesParser()
    while True:
        chunk = source.read()
        for stream_id, payload in parser.feed(chunk):
            ...

The parser keeps an internal byte buffer and yields exactly one
(stream_id, payload) tuple per complete PES packet. Pack headers (0xBA),
system headers (0xBB) and padding (0xBE) are silently consumed.
"""
from __future__ import annotations

import zlib
from typing import Iterator, List, Tuple


# stream_ids that carry an optional PES header (PTS/DTS/etc) before the
# payload. Outside this range the payload starts immediately after the 6-byte
# length field.
def _has_optional_header(stream_id: int) -> bool:
    if 0xC0 <= stream_id <= 0xEF:
        return True
    return stream_id in (0xBD, 0xFD)


# Headers we recognise as "container" framing rather than actual PES packets —
# we want to consume them and move on without yielding.
def _is_container_marker(stream_id: int) -> bool:
    # 0xBE = padding stream; 0xBF = private_stream_2;
    # 0xF0..0xF2, 0xF8 = ECM / EMM / DSMCC; 0xFF = program_stream_directory.
    return stream_id in (0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF)


class PesParser:
    """Stateful PES splitter; call feed() with each new chunk of bytes."""

    def __init__(self) -> None:
        self._buf = bytearray()
        # diagnostics
        self.packets_yielded = 0
        # Count video PES packets whose PES_scrambling_control is non-zero.
        # The device sets it when Image/Video Encryption is enabled: the
        # container and NAL framing stay readable but the payloads are
        # scrambled, so ffmpeg spins on garbage and it looks exactly like a
        # demux bug (#41 — three weeks to identify). Detecting it lets us
        # tell the user plainly instead.
        self.scrambled_packets = 0
        self.resync_drops = 0

    def feed(self, data: bytes) -> List[Tuple[int, bytes]]:
        """Append data and return every complete (stream_id, payload) tuple."""
        if data:
            self._buf += data
        out: List[Tuple[int, bytes]] = []
        while True:
            consumed = self._step(out)
            if consumed == 0:
                break
        return out

    def _step(self, out: List[Tuple[int, bytes]]) -> int:
        """Try to advance one packet. Returns bytes consumed (0 = need more)."""
        buf = self._buf
        if len(buf) < 6:
            return 0

        # Resync to a start-code prefix.
        if not (buf[0] == 0 and buf[1] == 0 and buf[2] == 1):
            idx = buf.find(b"\x00\x00\x01")
            if idx < 0:
                # Keep the last 2 bytes in case a start code straddles the
                # boundary; drop the rest.
                drop = len(buf) - 2
                if drop > 0:
                    self.resync_drops += drop
                    del buf[:drop]
                return 0
            self.resync_drops += idx
            del buf[:idx]
            return idx

        stream_id = buf[3]

        # MPEG-PS pack header (0xBA): 14 bytes + (last 3 bits of byte 13) of
        # stuffing bytes.
        if stream_id == 0xBA:
            if len(buf) < 14:
                return 0
            stuffing = buf[13] & 0x07
            total = 14 + stuffing
            if len(buf) < total:
                return 0
            del buf[:total]
            return total

        # MPEG-PS system header (0xBB): 16-bit length follows.
        if stream_id == 0xBB:
            length = (buf[4] << 8) | buf[5]
            total = 6 + length
            if len(buf) < total:
                return 0
            del buf[:total]
            return total

        # Other container markers — consume and ignore.
        if _is_container_marker(stream_id):
            length = (buf[4] << 8) | buf[5]
            total = 6 + length
            if len(buf) < total:
                return 0
            del buf[:total]
            return total

        # MPEG-PS program_end_code (0xB9): exactly 4 bytes total.
        if stream_id == 0xB9:
            del buf[:4]
            return 4

        # Real PES packet.
        pes_length = (buf[4] << 8) | buf[5]

        if pes_length == 0:
            # Unbounded packet (only video). Find the next start code; it
            # marks the end of this packet's payload.
            idx = buf.find(b"\x00\x00\x01", 6)
            if idx < 0:
                # Need more data before we can close the packet.
                return 0
            payload_offset = 6
            if _has_optional_header(stream_id):
                if len(buf) > 6 and (buf[6] >> 4) & 0x03:
                    self.scrambled_packets += 1
                if idx < 9:
                    del buf[:idx]
                    return idx
                header_data_len = buf[8]
                payload_offset = 9 + header_data_len
                if payload_offset > idx:
                    payload_offset = idx
            payload = bytes(buf[payload_offset:idx])
            out.append((stream_id, payload))
            self.packets_yielded += 1
            del buf[:idx]
            return idx

        total = 6 + pes_length
        if len(buf) < total:
            return 0

        payload_offset = 6
        if _has_optional_header(stream_id):
            if len(buf) > 6 and (buf[6] >> 4) & 0x03:
                self.scrambled_packets += 1
            if total < 9:
                del buf[:total]
                return total
            header_data_len = buf[8]
            payload_offset = 9 + header_data_len
            if payload_offset > total:
                payload_offset = total
        payload = bytes(buf[payload_offset:total])
        out.append((stream_id, payload))
        self.packets_yielded += 1
        del buf[:total]
        return total


_PTS_WRAP = 1 << 33
_PTS_HALF = 1 << 32


def _pes_pts(buf: bytearray, off: int, end: int) -> int | None:
    """PTS of the PES packet at ``off`` (start code included), or None."""
    if end - off < 14 or not buf[off + 7] & 0x80:
        return None
    p = buf[off + 9:off + 14]
    return (
        ((p[0] >> 1) & 0x07) << 30 | p[1] << 22 | (p[2] >> 1) << 15
        | p[3] << 7 | p[4] >> 1
    )


class PsReplayFilter:
    """Pass an MPEG-PS bytestream through, dropping replayed packs.

    The old HP7 (CS-HP7-R100) on the port-8000 source does not stream its
    video live: it sends its whole ~1 MiB ring buffer over and over, as
    fast as TCP allows (measured: ~43 Mbit/s, each pack repeated byte for
    byte up to ~60 times, video PTS jumping back ~13 s every ~0.2 s). Fed
    to ffmpeg as is, the picture plays at many times real speed and keeps
    looping. The HP7 Pro sends each frame once, so for it this filter is a
    pass-through.

    Packs (0xBA up to the next 0xBA) are only passed on once complete and
    well formed: every packet must end exactly where the next start code
    begins. Each lap of the ring starts at an arbitrary byte, so the pack
    before the seam is cut short and its last PES length runs into old
    data; such packs are dropped whole, and the stream resyncs at the next
    pack header.

    A complete pack is kept when every PTS in it is newer than the newest
    already sent for its stream (video and audio apart, 33-bit wrap
    handled). A large frame spans several packs with the same PTS, so a
    PTS equal to the newest also counts unless that exact pack was
    already sent. Packs without a PTS follow the pack before them.
    Payloads are never touched, so encrypted streams pass unchanged.
    """

    # A pack is one frame at most; anything this long is not a real pack.
    MAX_PACK = 4 << 20

    def __init__(self) -> None:
        self._buf = bytearray()
        self._last_keep = True
        self._newest: dict[int, int] = {}  # 0xE0 / 0xC0 -> newest PTS sent
        self._sent: dict[int, set[int]] = {}  # kind -> CRCs at newest PTS
        self.dropped_bytes = 0
        self.passed_bytes = 0
        self.bad_packs = 0

    def _scan_pack(
        self, buf: bytearray, start: int
    ) -> tuple[int, list[int]] | None:
        """Parse the pack at ``start``.

        Returns (end, offsets of the PES packets carrying a PTS) for a
        complete, well-formed pack; (-1, []) for a malformed one; None when
        more data is needed.
        """
        n = len(buf)
        if n - start < 14:
            return None
        q = start + 14 + (buf[start + 13] & 0x07)
        with_pts: list[int] = []
        while True:
            if q - start > self.MAX_PACK:
                return -1, []
            if n - q < 4:
                return None
            if buf[q] or buf[q + 1] or buf[q + 2] != 1:
                return -1, []
            sid = buf[q + 3]
            if sid == 0xBA:
                return q, with_pts
            if sid == 0xB9:
                return q + 4, with_pts
            if n - q < 6:
                return None
            length = (buf[q + 4] << 8) | buf[q + 5]
            if length:
                end = q + 6 + length
            else:
                end = buf.find(b"\x00\x00\x01", q + 6)
                if end < 0:
                    return None
            if end > n:
                return None
            if 0xC0 <= sid <= 0xEF and _pes_pts(buf, q, end) is not None:
                with_pts.append(q - start)
            q = end

    def _decide(self, pack: bytes, with_pts: list[int]) -> bool:
        """Keep the pack only if every PTS in it is new for its stream."""
        if not with_pts:
            return self._last_keep
        crc = None
        updates: dict[int, int] = {}
        for off in with_pts:
            kind = pack[off + 3] & 0xF0  # 0xE0 video, 0xC0/0xD0 audio
            pts = _pes_pts(pack, off, len(pack))
            last = updates.get(kind, self._newest.get(kind))
            if last is None or 0 < (pts - last) % _PTS_WRAP < _PTS_HALF:
                updates[kind] = pts
                continue
            if pts == last:
                if crc is None:
                    crc = zlib.crc32(pack)
                if crc not in self._sent.get(kind, ()):
                    continue
            return False
        if crc is None:
            crc = zlib.crc32(pack)
        for kind in {pack[off + 3] & 0xF0 for off in with_pts}:
            pts = updates.get(kind)
            if pts is not None and pts != self._newest.get(kind):
                self._newest[kind] = pts
                self._sent[kind] = {crc}
            else:
                self._sent.setdefault(kind, set()).add(crc)
        return True

    def feed(self, data: bytes) -> bytes:
        """Append ``data``; return the bytes that can be passed on now."""
        buf = self._buf
        buf += data
        out = bytearray()
        pos = 0
        while True:
            if buf[pos:pos + 4] != b"\x00\x00\x01\xba":
                idx = buf.find(b"\x00\x00\x01\xba", pos)
                if idx < 0:
                    idx = max(pos, len(buf) - 3)
                self.dropped_bytes += idx - pos
                pos = idx
                if len(buf) - pos < 4:
                    break
                continue
            scanned = self._scan_pack(buf, pos)
            if scanned is None:
                break
            end, with_pts = scanned
            if end < 0:
                # Cut short at the ring seam (or corrupt): resync.
                self.bad_packs += 1
                self.dropped_bytes += 4
                pos += 4
                continue
            pack = bytes(buf[pos:end])
            keep = self._decide(pack, with_pts)
            self._last_keep = keep
            if keep:
                out += pack
                self.passed_bytes += len(pack)
            else:
                self.dropped_bytes += len(pack)
            pos = end
        del buf[:pos]
        return bytes(out)
