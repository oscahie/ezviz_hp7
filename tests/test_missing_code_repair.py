"""Repairs issue + fix flow for local_sdk without a verification code."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.helpers import issue_registry as ir

from custom_components.ezviz_hp7 import repairs
from custom_components.ezviz_hp7.const import CONF_VERIFICATION_CODE, DOMAIN
from custom_components.ezviz_hp7.live_camera import (
    Hp7StreamRelay,
    NetSdkLanSource,
    _sync_missing_code_issue,
)
from custom_components.ezviz_hp7.pylocalapi.hcnetsdk import (
    HcNetSdkLoginRejected,
    HcNetSdkTcpFrame,
    HcNetSdkTcpFrameHeader,
    parse_hcnetsdk_command_port_login_session,
)

SERIAL = "BA0000000-BA0000001"
ISSUE = f"verification_code_missing_{SERIAL}"


def _issue(hass):
    return ir.async_get(hass).async_get_issue(DOMAIN, ISSUE)


async def test_issue_only_for_local_sdk_without_code(hass):
    for source in ("cloud", "local", "auto"):
        _sync_missing_code_issue(hass, SERIAL, source, needs_code=True)
        assert _issue(hass) is None
    _sync_missing_code_issue(hass, SERIAL, "local_sdk", needs_code=False)
    assert _issue(hass) is None

    _sync_missing_code_issue(hass, SERIAL, "local_sdk", needs_code=True)
    issue = _issue(hass)
    assert issue is not None and issue.is_fixable

    # Cleared as soon as a code is configured or the source changes.
    _sync_missing_code_issue(hass, SERIAL, "local_sdk", needs_code=False)
    assert _issue(hass) is None
    _sync_missing_code_issue(hass, SERIAL, "local_sdk", needs_code=True)
    _sync_missing_code_issue(hass, SERIAL, "local", needs_code=True)
    assert _issue(hass) is None


async def test_fix_flow_for_issue_id():
    flow = await repairs.async_create_fix_flow(None, ISSUE, {"serial": SERIAL})
    assert isinstance(flow, repairs.VerificationCodeRepairFlow)
    flow = await repairs.async_create_fix_flow(None, ISSUE, None)
    assert flow._serial == SERIAL
    flow = await repairs.async_create_fix_flow(
        None, f"encryption_otp_{SERIAL}", None
    )
    assert isinstance(flow, repairs.EncryptionOtpRepairFlow)


def _flow(hass, entry):
    flow = repairs.VerificationCodeRepairFlow(SERIAL)
    flow.hass = hass
    flow.flow_id = "x"
    flow.handler = DOMAIN
    return flow


async def test_fix_flow_stores_code_only_when_login_works(hass):
    entry = MagicMock(options={"stream_source": "local_sdk"})
    hass.config_entries.async_update_entry = MagicMock()
    flow = _flow(hass, entry)
    with patch.object(repairs, "_resolve_entry", return_value=(entry, object())):
        with patch.object(repairs, "_port_8000_login", return_value="cannot_login"):
            res = await flow.async_step_confirm({"code": "ABCDEF"})
        assert res["type"] == "form"
        assert res["errors"] == {"code": "cannot_login"}
        hass.config_entries.async_update_entry.assert_not_called()

        res = await flow.async_step_confirm({"code": "  "})
        assert res["errors"] == {"code": "empty_code"}

        with patch.object(repairs, "_port_8000_login", return_value="invalid_code"):
            res = await flow.async_step_confirm({"code": "ZZZZZZ"})
        assert res["errors"] == {"code": "invalid_code"}

        with patch.object(repairs, "_port_8000_login", return_value="ok"):
            res = await flow.async_step_confirm({"code": " ABCDEF "})
    assert res["type"] == "create_entry"
    hass.config_entries.async_update_entry.assert_called_once_with(
        entry,
        options={"stream_source": "local_sdk", CONF_VERIFICATION_CODE: "ABCDEF"},
    )


def test_login_check_tells_refused_from_other_failures():
    src = MagicMock()
    with patch(
        "custom_components.ezviz_hp7.live_camera.NetSdkLanSource",
        return_value=src,
    ):
        assert repairs._port_8000_login(MagicMock(), SERIAL, "ABCDEF") == "ok"
        src.close.assert_called_once()
        src.start.side_effect = HcNetSdkLoginRejected("login failed")
        assert repairs._port_8000_login(MagicMock(), SERIAL, "Z") == "invalid_code"
        src.start.side_effect = OSError("timed out")
        assert repairs._port_8000_login(MagicMock(), SERIAL, "Z") == "cannot_login"
        assert src.close.call_count == 3


async def test_fix_flow_for_rejected_issue():
    flow = await repairs.async_create_fix_flow(
        None, f"verification_code_rejected_{SERIAL}", None
    )
    assert isinstance(flow, repairs.VerificationCodeRepairFlow)
    assert flow._serial == SERIAL


def _frame(body):
    header = HcNetSdkTcpFrameHeader(16 + len(body), 0, 1, 0)
    return HcNetSdkTcpFrame(header, body)


def test_empty_session_raises_login_rejected():
    with pytest.raises(HcNetSdkLoginRejected):
        parse_hcnetsdk_command_port_login_session(
            _frame(b""), _frame(b"\x00" * 48)
        )
    session = parse_hcnetsdk_command_port_login_session(
        _frame(b""), _frame(b"\x00\x00\x00\x07" + b"\x00" * 44)
    )
    assert session.session_id == b"\x00\x00\x00\x07"


def test_rejected_login_stops_retries():
    relay = Hp7StreamRelay(
        api=MagicMock(), serial=SERIAL, channel=1, stream_source="local_sdk",
        verification_code="ZZZZZZ",
    )
    relay._note_login_rejected(HcNetSdkLoginRejected("x"))
    assert relay._login_rejected

    async def _go():
        loop = asyncio.get_running_loop()
        await relay._open_source(loop, MagicMock())

    with pytest.raises(RuntimeError, match="not retrying"):
        asyncio.run(_go())


def test_source_reports_rejection_to_the_relay():
    cb = MagicMock()
    src = NetSdkLanSource(MagicMock(), SERIAL, "ZZZZZZ", on_login_rejected=cb)
    stream = MagicMock()
    stream.start.side_effect = HcNetSdkLoginRejected("x")
    with patch(
        "custom_components.ezviz_hp7.pylocalapi.local_stream."
        "open_hcnetsdk_command_port_generated_multi_socket_stream",
        return_value=stream,
    ):
        src._api.get_local_ip.return_value = "10.0.0.1"
        with pytest.raises(HcNetSdkLoginRejected):
            src.start()
    cb.assert_called_once()
    stream.close.assert_called_once()
