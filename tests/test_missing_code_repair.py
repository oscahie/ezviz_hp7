"""Repairs issue + fix flow for local_sdk without a verification code."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from homeassistant.helpers import issue_registry as ir

from custom_components.ezviz_hp7 import repairs
from custom_components.ezviz_hp7.const import CONF_VERIFICATION_CODE, DOMAIN
from custom_components.ezviz_hp7.live_camera import _sync_missing_code_issue

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
        with patch.object(repairs, "_port_8000_login_works", return_value=False):
            res = await flow.async_step_confirm({"code": "ABCDEF"})
        assert res["type"] == "form"
        assert res["errors"] == {"code": "cannot_login"}
        hass.config_entries.async_update_entry.assert_not_called()

        res = await flow.async_step_confirm({"code": "  "})
        assert res["errors"] == {"code": "empty_code"}

        with patch.object(repairs, "_port_8000_login_works", return_value=True):
            res = await flow.async_step_confirm({"code": " ABCDEF "})
    assert res["type"] == "create_entry"
    hass.config_entries.async_update_entry.assert_called_once_with(
        entry,
        options={"stream_source": "local_sdk", CONF_VERIFICATION_CODE: "ABCDEF"},
    )


def test_login_check_closes_the_session():
    src = MagicMock()
    with patch(
        "custom_components.ezviz_hp7.live_camera.NetSdkLanSource",
        return_value=src,
    ):
        assert repairs._port_8000_login_works(MagicMock(), SERIAL, "ABCDEF")
        src.close.assert_called_once()
        src.start.side_effect = RuntimeError("login failed")
        assert not repairs._port_8000_login_works(MagicMock(), SERIAL, "ABCDEF")
        assert src.close.call_count == 2
