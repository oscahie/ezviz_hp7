"""Channel-99 push: app-profile login, message decoding, and fallback to the legacy push."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.ezviz_hp7 import channel99, push
from custom_components.ezviz_hp7.channel99 import Channel99Error, Channel99Push
from custom_components.ezviz_hp7.const import DOMAIN

SERIAL = "BW0000000-BW0000001"
LOGIN_OK = {
    "meta": {"code": 200},
    "loginSession": {"sessionId": "session-1"},
    "loginUser": {"userId": "user-1"},
}
SYSTEM_INFO = {"systemConfigInfo": {"pushDasDomain": "das.example", "pushDasPort": 8777}}
REGISTERED = {"meta": {"code": 200}}


def _client(replies, on_message=None, state=None):
    c = Channel99Push(
        "someone@example.com", "secret", "api.example", state if state is not None else {},
        MagicMock(), on_message or MagicMock(),
    )
    c._http.request = MagicMock(
        side_effect=[SimpleNamespace(json=lambda r=r: r, status_code=200) for r in replies]
    )
    return c


def test_login_uses_the_android_profile_and_registers_channel_99():
    c = _client([LOGIN_OK, SYSTEM_INFO, REGISTERED])
    c._login()
    calls = c._http.request.call_args_list
    assert calls[0].args == ("POST", "https://api.example/v3/users/login/v5")
    data = calls[0].kwargs["data"]
    assert data["pushRegisterJson"] == '[{"channel":99}]'
    assert data["featureCode"] == channel99.feature_code("someone@example.com")
    assert c._http.headers["appId"] == "ys7"
    assert c._http.headers["clientNo"] == "google"
    assert calls[2].args == ("PUT", "https://api.example/v3/push/token")
    assert calls[2].kwargs["params"]["pushRegisterJson"] == '[{"channel":99}]'
    assert c._endpoint == ("das.example", 8777)
    assert (c._session_id, c._user_id) == ("session-1", "user-1")


def test_login_follows_the_region_redirect():
    redirect = {"meta": {"code": 1100}, "loginArea": {"apiDomain": "apiieu.example"}}
    c = _client([redirect, LOGIN_OK, SYSTEM_INFO, REGISTERED])
    c._login()
    assert c._http.request.call_args_list[1].args[1] == "https://apiieu.example/v3/users/login/v5"


@pytest.mark.parametrize(
    "replies",
    [
        [{"meta": {"code": 6002}}],  # MFA for the new terminal
        [{"meta": {"code": 1013}}],  # wrong password
        [LOGIN_OK, SYSTEM_INFO, {"meta": {"code": 403}}],  # registration refused
        [LOGIN_OK, {"systemConfigInfo": {}}],  # no LBS host
    ],
)
def test_login_problems_raise_channel99_error(replies):
    with pytest.raises(Channel99Error):
        _client(replies)._login()


def test_feature_code_differs_from_the_main_client_per_account():
    assert channel99.feature_code("a@x") != channel99.feature_code("b@x")
    assert len(channel99.feature_code("a@x")) == 32


def test_receive_decodes_json_and_a_string_ext():
    on_message = MagicMock()
    c = _client([], on_message=on_message)
    c._receive(b'{"pushType": 2, "ext": {"device_serial": "X", "calling_id": "1"}}')
    c._receive(b'{"pushType": 1, "ext": "{\\"device_serial\\": \\"Y\\"}"}')
    c._receive(b"not json")
    c._receive(b"[1, 2]")
    assert [m.args[0]["ext"]["device_serial"] for m in on_message.call_args_list] == ["X", "Y"]


def test_receive_decodes_the_csv_ext_of_live_pushes():
    """Live channel-99 pushes carry ext as the app's comma-separated form."""
    on_message = MagicMock()
    c = _client([], on_message=on_message)
    call = "14,2026-11-23+21:30:45,%s,1,0,20261123213045910-BK0-0-00032,1,,0,,0,,,%s,," % (
        SERIAL, SERIAL)
    unlock = "1,2026-11-23 21:25:32,%s,1,10242,,,,Oscar (F),0,f,1,,0,,m,,d,,2401" % SERIAL
    c._receive(json.dumps({"pushType": 2, "soundType": 2701, "ext": call}).encode())
    c._receive(json.dumps({"pushType": 1, "ext": unlock}).encode())
    first, second = (m.args[0]["ext"] for m in on_message.call_args_list)
    assert first["device_serial"] == SERIAL
    assert first["calling_status"] == 1 and first["time"] == "2026-11-23 21:30:45"
    assert second["device_serial"] == SERIAL
    assert second["alert_type_code"] == 10242 and second["customerType"] == "Oscar (F)"
    assert second["subType"] == 2401


async def test_csv_ext_push_reaches_the_listener(hass):
    listener = push.Hp7PushListener(hass, MagicMock(), _coordinator())
    listener._on_message(
        {"pushType": 2, "ext": channel99.decode_ext(f"14,t,{SERIAL},1,0,id,1")}
    )
    await hass.async_block_till_done()
    assert len(listener._refresh_unsubs) == len(push.REFRESH_DELAYS)
    await listener.async_stop()


def test_saved_keys_are_wrapped_with_the_user_id():
    c = _client([])
    c._user_id = "user-1"
    c._save({"device_id": "abc"})
    c._save_state.assert_called_once_with({"user_id": "user-1", "keys": {"device_id": "abc"}})


def test_keys_of_another_account_are_dropped_on_start(monkeypatch):
    state = {"user_id": "someone-else", "keys": {"device_id": "old"}}
    c = _client([LOGIN_OK, SYSTEM_INFO, REGISTERED], state=state)
    worker = MagicMock()
    monkeypatch.setattr(channel99, "PushWorker", MagicMock(return_value=worker))
    c.start()
    assert state == {"user_id": "user-1", "keys": {}}
    worker.start.assert_called_once()


# -- listener -----------------------------------------------------------------
def _coordinator():
    coord = MagicMock()
    coord.serial = SERIAL
    coord.monitor_serial = None
    coord.config_entry = SimpleNamespace(entry_id="entry-1")
    return coord


async def test_listener_prefers_channel_99(hass):
    api = MagicMock()
    ch99 = MagicMock(failed=False)
    api.start_channel99.return_value = ch99
    listener = push.Hp7PushListener(hass, api, _coordinator())
    await listener.async_start()
    assert listener.mode == "channel99"
    api.start_push.assert_not_called()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"channel99_unavailable_{SERIAL}") is None
    await listener.async_stop()
    ch99.stop.assert_called_once()


async def test_listener_falls_back_to_legacy_push_and_raises_an_issue(hass):
    api = MagicMock()
    api.start_channel99.side_effect = Channel99Error("login asks for a verification code")
    listener = push.Hp7PushListener(hass, api, _coordinator())
    await listener.async_start()
    assert listener.mode == "legacy"
    api.start_push.assert_called_once()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"channel99_unavailable_{SERIAL}")
    assert issue is not None and issue.translation_key == "channel99_unavailable"
    await listener.async_stop()


async def test_listener_switches_to_legacy_when_channel_99_gives_up(hass):
    api = MagicMock()
    ch99 = MagicMock(failed=False)
    ch99.diagnostics.return_value = {"fatal_error_type": "EzvizPushFatalError"}
    api.start_channel99.return_value = ch99
    listener = push.Hp7PushListener(hass, api, _coordinator())
    await listener.async_start()
    await listener._async_check_channel99()
    assert listener.mode == "channel99"  # healthy: nothing happens
    ch99.failed = True
    await listener._async_check_channel99()
    ch99.stop.assert_called_once()
    assert listener.mode == "legacy"
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"channel99_unavailable_{SERIAL}")
    await listener.async_stop()


async def test_ring_push_for_this_door_schedules_refreshes(hass):
    listener = push.Hp7PushListener(hass, MagicMock(), _coordinator())
    listener._on_message({"pushType": 2, "ext": {"device_serial": SERIAL, "calling_id": "1"}})
    listener._on_message({"pushType": 2, "ext": {"device_serial": "OTHER"}})
    await hass.async_block_till_done()
    assert len(listener._refresh_unsubs) == len(push.REFRESH_DELAYS)
    await listener.async_stop()
