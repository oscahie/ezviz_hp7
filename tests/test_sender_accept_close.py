"""A sender whose listener is closed before ffmpeg connects must exit quietly.

The client teardown closes the listeners while the sender threads may still
sit in accept(); that surfaces as OSError [Errno 9] and used to escape the
thread as an "Uncaught thread exception".
"""
from __future__ import annotations

import errno
import queue
import threading
from unittest.mock import MagicMock

from custom_components.ezviz_hp7 import live_camera


def test_listener_closed_during_accept_exits_quietly(monkeypatch):
    errors: list[BaseException] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))

    listener = MagicMock()
    listener.accept.side_effect = OSError(errno.EBADF, "Bad file descriptor")
    t = threading.Thread(
        target=live_camera._sender_thread,
        args=(listener, queue.Queue(), threading.Event(), "video"),
    )
    t.start()
    t.join(timeout=2.0)

    assert not t.is_alive()
    assert errors == []
    listener.close.assert_called()
