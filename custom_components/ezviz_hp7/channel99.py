"""EZVIZ channel-99 push: the long link the mobile app gets its calls on.

The legacy MQTT push (``pylocalapi.mqtt``) delivers unlock and detection
alarms but never doorbell calls. The app receives calls over "channel 99", a
long link registered at login with ``pushRegisterJson=[{"channel":99}]`` and
reached through the LBS host the cloud names in ``pushDasDomain``. Verified on
an HP7 Pro (22 Nov 2026): the same link carries calls (``pushType`` 2,
``soundType`` 2701, ``ext.calling_id``), unlocks (``alert_type_code`` 10242,
``ext.customerType`` "Card" / "<name> (F)") and smart detections (10079), ~3-5 s
after the event. Accounts with alarm do-not-disturb on only get the calls.

Channel 99 needs its own login with the Android app profile, so this keeps a
separate HTTP session (and feature code) from the integration's main client;
the main session is untouched. Blocking code: run it in an executor.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any

import requests

from .longlink._longlink_session import Channel99Session
from .longlink._longlink_worker import PushWorker
from .longlink.exceptions import EzvizPushFatalError

_LOGGER = logging.getLogger(__name__)

# Android-app identity (pyEzvizApi's ANDROID_PROFILE): channel-99 registration
# is refused for the web profile the integration's main client uses.
_HEADERS = {
    "clientType": "3",
    "osVersion": "13",
    "clientVersion": "7.4.1.0421",
    "netType": "WIFI",
    "customno": "1000001",
    "ssid": "",
    "clientNo": "google",
    "appId": "ys7",
    "language": "en_GB",
    "lang": "en",
    "User-Agent": "okhttp/3.12.1",
}
_REGISTRATION = {
    "pushRegisterJson": '[{"channel":99}]',
    "pushExtJson": '{"language":"","protoVer":"2"}',
}
_LOGIN = "/v3/users/login/v5"
_REDIRECT = 1100
_MFA_CODES = (6002, 6003)
# Log in again before the long link reconnects with a session this old.
_SESSION_MAX_AGE = 6 * 3600
_TIMEOUT = 25


class Channel99Error(Exception):
    """Login or registration failed; the caller should fall back."""


def feature_code(account: str) -> str:
    """Per-account terminal id, distinct from the main client's."""
    return hashlib.md5(f"ezviz_hp7:channel99:{account}".encode()).hexdigest()


class Channel99Push:
    """One account's channel-99 link; ``on_message`` gets each push as a dict.

    ``state``/``save_state`` ({"user_id", "keys"}) persist the device keys the LBS allocates on the
    first connect, so a restart reuses them instead of allocating new ones.
    ``save_state`` is called from the worker thread.
    """

    def __init__(
        self,
        account: str,
        password: str,
        api_host: str,
        state: dict[str, Any],
        save_state: Callable[[dict[str, Any]], None],
        on_message: Callable[[dict[str, Any]], None],
    ) -> None:
        self._account = account
        self._password = password
        self._base = f"https://{api_host}"
        self._stored = state
        self._save_state = save_state
        self._on_message = on_message
        self._feature_code = feature_code(account)
        self._http = requests.Session()
        self._http.headers.update({**_HEADERS, "featureCode": self._feature_code})
        self._session_id: str | None = None
        self._user_id: str | None = None
        self._logged_in_at = float("-inf")
        self._endpoint: tuple[str, int] | None = None
        self._worker: PushWorker | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Log in, register channel 99 and start the background worker."""
        self._login()
        serial = f"MOBILE:ys7:{self._user_id}:{self._feature_code}".encode("ascii")
        if self._stored.get("user_id") != self._user_id:
            # Keys belong to another account's terminal (or none yet).
            self._stored.clear()
            self._stored.update(user_id=self._user_id, keys={})
        keys = self._stored.setdefault("keys", {})
        endpoint = self._endpoint
        self._worker = PushWorker(
            lambda: Channel99Session(
                endpoint,
                serial,
                lambda: self._session_id,
                keys,
                self._save,
                self._receive,
                prepare=self._prepare,
            )
        )
        self._worker.start()

    def stop(self) -> None:
        if self._worker is not None:
            worker, self._worker = self._worker, None
            worker.stop()
        self._http.close()

    @property
    def failed(self) -> bool:
        """True once the worker hit an error it won't retry (needs fallback)."""
        return self._worker is not None and self._worker.failure is not None

    def diagnostics(self) -> dict[str, Any]:
        return self._worker.diagnostics() if self._worker else {"state": "idle"}

    # -- HTTP --------------------------------------------------------------
    def _login(self) -> None:
        data = {
            "account": self._account,
            "password": hashlib.md5(self._password.encode("utf-8")).hexdigest(),
            "featureCode": self._feature_code,
            "msgType": "0",
            "bizType": "",
            "cuName": base64.b64encode(b"Hassio").decode(),
            **_REGISTRATION,
        }
        j = self._request("POST", _LOGIN, data=data)
        code = (j.get("meta") or {}).get("code")
        if code == _REDIRECT and isinstance(j.get("loginArea"), dict):
            self._base = f"https://{j['loginArea']['apiDomain']}"
            j = self._request("POST", _LOGIN, data=data)
            code = (j.get("meta") or {}).get("code")
        if code in _MFA_CODES:
            raise Channel99Error(
                "login asks for a verification code (MFA) for the app profile"
            )
        if "loginSession" not in j or "loginUser" not in j:
            raise Channel99Error(f"login failed (code {code})")
        self._session_id = str(j["loginSession"]["sessionId"])
        self._user_id = str(j["loginUser"]["userId"])
        self._http.headers["sessionId"] = self._session_id
        self._logged_in_at = time.monotonic()
        info = self._request("GET", "/v3/configurations/system/info")
        info = info.get("systemConfigInfo") or {}
        if not info.get("pushDasDomain"):
            raise Channel99Error("cloud did not name a channel-99 host")
        self._endpoint = (str(info["pushDasDomain"]), int(info.get("pushDasPort") or 8666))
        reg = self._request("PUT", "/v3/push/token", params=_REGISTRATION)
        if (reg.get("meta") or {}).get("code") != 200:
            raise Channel99Error(
                f"channel-99 registration rejected (code {(reg.get('meta') or {}).get('code')})"
            )

    def _prepare(self) -> None:
        """Before each (re)connect: refresh an old session by logging in again."""
        if time.monotonic() - self._logged_in_at <= _SESSION_MAX_AGE:
            return
        try:
            self._login()
        except Channel99Error as exc:
            # A password change or new MFA demand won't fix itself.
            raise EzvizPushFatalError(str(exc)) from exc

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        r = self._http.request(method, f"{self._base}{path}", timeout=_TIMEOUT, **kwargs)
        try:
            payload = r.json()
        except ValueError as exc:
            raise Channel99Error(f"{method} {path}: non-JSON reply ({r.status_code})") from exc
        return payload if isinstance(payload, dict) else {}

    # -- worker callbacks (worker thread) ---------------------------------
    def _save(self, snapshot: dict[str, Any]) -> None:
        """Persist before returning: the handshake relies on it."""
        self._save_state({"user_id": self._user_id, "keys": dict(snapshot)})

    def _receive(self, payload: bytes) -> None:
        try:
            message = json.loads(payload)
        except ValueError:
            return
        if not isinstance(message, dict):
            return
        ext = message.get("ext")
        if isinstance(ext, str):
            try:
                message["ext"] = json.loads(ext)
            except ValueError:
                pass
        try:
            self._on_message(message)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("EZVIZ channel-99 message handler failed")
