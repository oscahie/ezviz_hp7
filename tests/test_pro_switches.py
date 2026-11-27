"""HP7 Pro keypad backlight and call-button light (switch type 3)."""
from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.ezviz_hp7.api import Hp7Api

SERIAL = "BK0000000-BK0000001"

# Shape read from a real HP7 Pro door station (values anonymised).
INFO = {
    "FEATURE_INFO": {
        "0": {"DoorLock": {"LightCtrl": {}}},
        "1": {
            "Video": {
                "LightCtrl": {},
                "KeypadMgr": {
                    "KeypadLinkageParam": {
                        "soundPromptParam": {"buttonSound": True},
                        "LEDPromptParam": {"backlightEnabled": False},
                    }
                },
            }
        },
    },
    "SWITCH": [{"type": 3, "enable": True}, {"type": 7, "enable": False}],
}


def _api():
    api = Hp7Api("u", "p")
    api._client = MagicMock()
    api._token = {"session": "x"}
    return api


def test_reads_both_states():
    out = _api()._parse_extra_states(SERIAL, INFO)
    assert out["indicator_light_on"] is True
    assert out["keypad_backlight_on"] is False
    assert out["privacy_on"] is False


def test_models_without_them_report_nothing():
    out = _api()._parse_extra_states(SERIAL, {"SWITCH": [{"type": 7, "enable": True}]})
    assert "indicator_light_on" not in out
    assert "keypad_backlight_on" not in out


def test_backlight_write_keeps_the_button_sound():
    api = _api()
    api._parse_extra_states(SERIAL, INFO)
    assert api.set_keypad_backlight(SERIAL, True)
    kwargs = api._client.set_iot_feature.call_args.kwargs
    assert api._client.set_iot_feature.call_args.args == (SERIAL,)
    assert kwargs["resource_identifier"] == "Video"
    assert kwargs["local_index"] == "1"
    assert kwargs["domain_id"] == "KeypadMgr"
    assert kwargs["action_id"] == "KeypadLinkageParam"
    assert kwargs["value"] == {
        "value": {
            "soundPromptParam": {"buttonSound": True},
            "LEDPromptParam": {"backlightEnabled": True},
        }
    }
    # The value read at status time is not changed in place.
    assert INFO["FEATURE_INFO"]["1"]["Video"]["KeypadMgr"]["KeypadLinkageParam"][
        "LEDPromptParam"
    ]["backlightEnabled"] is False


def test_indicator_write_uses_switch_type_3():
    api = _api()
    assert api.set_indicator_light(SERIAL, False)
    api._client.switch_status.assert_called_once_with(SERIAL, 3, False)
