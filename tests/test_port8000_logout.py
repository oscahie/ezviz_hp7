"""Port-8000 streams log their session out when they close."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from custom_components.ezviz_hp7.pylocalapi import local_stream
from custom_components.ezviz_hp7.pylocalapi.hcnetsdk import HcNetSdkLanEndpoint
from custom_components.ezviz_hp7.pylocalapi.local_stream import (
    HCNETSDK_COMMAND_PORT_LOGOUT_COMMAND,
    hcnetsdk_command_port_native_lan_live_view_plan,
    open_hcnetsdk_command_port_generated_multi_socket_stream,
)


def _stream():
    return open_hcnetsdk_command_port_generated_multi_socket_stream(
        HcNetSdkLanEndpoint(serial="BA0", host="10.0.0.2", command_port=8000),
        hcnetsdk_command_port_native_lan_live_view_plan(channel=33),
        password="ABCDEF",
    )


def _session():
    session = MagicMock()
    session.session_id = b"\x00\x00\x00\x07"
    session.auth_seed = 0x1234
    session.challenge = b"k" * 16
    return session


def test_close_sends_logout_once():
    stream = _stream()
    stream.login_session = _session()
    stream._session_local_ip = "10.0.0.3"
    media = MagicMock()
    stream._stream = media
    client = MagicMock()
    client.__enter__.return_value = client
    template = MagicMock()
    template.return_value.to_frame.return_value = b"logout-frame"
    with patch.object(
        local_stream, "HcNetSdkCommandPortClient", return_value=client
    ), patch.object(
        local_stream, "HcNetSdkCommandPortControlTemplate", template
    ):
        stream.close()
        stream.close()
    media.close.assert_called_once()
    assert template.call_args.kwargs["command_id"] == HCNETSDK_COMMAND_PORT_LOGOUT_COMMAND
    client.send_command_frame.assert_called_once_with(b"logout-frame")
    assert stream.login_session is None


def test_logout_failure_is_swallowed():
    stream = _stream()
    stream.login_session = _session()
    stream._session_local_ip = "10.0.0.3"
    with patch.object(
        local_stream, "HcNetSdkCommandPortClient", side_effect=OSError("refused")
    ):
        stream.close()
    assert stream.login_session is None


def test_no_logout_without_login():
    stream = _stream()
    with patch.object(local_stream, "HcNetSdkCommandPortClient") as client:
        stream.close()
    client.assert_not_called()
