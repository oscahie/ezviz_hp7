"""Cloud (TTS) two-way-talk session, used to hang up a ringing doorbell call.

Answering a call in the EZVIZ app opens a talk session on the EZVIZ TTS
server; the moment the doorbell joins that session, the indoor monitor and
every phone stop ringing. Opening the session and closing it once the device
has joined is therefore a "hang up".

Wire format from the app's libezstreamclient.so (EZVIZ app 7.6.1):
``EZStreamClientProxy::getVoiceTalkUrl`` builds the ``tts://`` URL and
``CTalkClnSession::MakeStartMsgEx`` (plain, no ECDH) frames the start request
as four big-endian u32 (1, 4, 0, body length) followed by a JSON-like body.
The server answers with the same header (third field 0 on success) and
``restult: OK;...``; the device's audio follows once it has joined.

Tested on the HP7 Pro: ``dev`` must be the composite ``MONITOR-DOOR`` serial
(the door-station serial alone is refused with code 14). Neither the call's
callingId nor marking the call answered is needed.
"""
from __future__ import annotations

import socket
import struct
import time
import uuid
from typing import Any, Callable

TALK_TYPE_ANSWER = 3
JOIN_TIMEOUT = 5.0
_MAX_REPLY = 4096


class TalkError(Exception):
    """The talk session could not be opened, or the device did not join."""


def talk_url(host: str, port: int, serial: str, token: str) -> str:
    """Return the ``tts://`` URL the app uses to answer a call."""
    return (
        f"tts://{host}:{port}/talk?dev={serial}&chann=0&encodetype=1"
        f"&authssn={token}&cln=3&talktype={TALK_TYPE_ANSWER}"
    )


def start_message(url: str) -> bytes:
    """Frame the talk start request (``MakeStartMsgEx`` without ECDH)."""
    body = (
        '{\r\n"url":"' + url + '&tls=0",\r\n'
        f'"timestamp":"{int(time.time() * 1000)}",\r\n'
        f'"uuid":"{uuid.uuid4()}"\r\n}}'
    ).encode()
    return struct.pack(">IIII", 1, 4, 0, len(body)) + body


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise TalkError(f"server closed the connection after {len(buf)}/{size} bytes")
        buf += chunk
    return buf


def hang_up(
    host: str,
    port: int,
    serial: str,
    token: str,
    timeout: float = JOIN_TIMEOUT,
    connect: Callable[..., Any] = socket.create_connection,
) -> None:
    """Open a talk session, wait until the device joins, then close it.

    Raises:
        TalkError: the server refused the session, or the device did not
            join within ``timeout`` (the call was then not hung up).
        OSError: the TTS server could not be reached.
    """
    with connect((host, port), timeout=timeout) as sock:
        sock.sendall(start_message(talk_url(host, port, serial, token)))
        try:
            _, _, code, size = struct.unpack(">IIII", _recv_exact(sock, 16))
            if size > _MAX_REPLY:
                raise TalkError(f"unexpected reply length {size}")
            body = _recv_exact(sock, size)
        except TimeoutError as exc:
            raise TalkError("no reply from the EZVIZ talk server") from exc
        if code != 0 or b"OK" not in body:
            raise TalkError(f"talk server refused the session (code {code}: {body[:80]!r})")
        # Closing before the device has joined leaves the call ringing: wait
        # for its first audio bytes.
        try:
            joined = sock.recv(_MAX_REPLY)
        except TimeoutError as exc:
            raise TalkError(f"the doorbell did not join the talk session within {timeout:.0f}s") from exc
        if not joined:
            raise TalkError("talk server closed the session before the doorbell joined")
