"""Unlock log: the latest door and gate openings, with their pictures.

The door station keeps a picture of every unlock in EZVIZ's message list
(``alarmType`` 10242 = lock, 10243 = gate), with who opened it in
``ext.customerType``. This module reads that list, serves the pictures
decrypted through Home Assistant (signed links, never public) and gives the
``ezviz-hp7-unlock-log-card`` its rows over the websocket API.
"""
from __future__ import annotations

import json
import logging
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol
from aiohttp import web

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_ENCRYPTION_KEY, CONF_VERIFICATION_CODE, DOMAIN
from .coordinator import unlock_user

_LOGGER = logging.getLogger(__name__)

ALARM_TYPE_LOCK = 10242
ALARM_TYPE_GATE = 10243
UNLOCK_ALARM_TYPES = {ALARM_TYPE_LOCK: "door", ALARM_TYPE_GATE: "gate"}

LOG_SIZE = 10
DAYS_BACK = 7  # how far back the list is searched for LOG_SIZE unlocks
PAGE_SIZE = 50
MAX_PAGES_PER_DAY = 6

PICTURE_URL = "/api/ezviz_hp7/unlock_picture/{entry_id}/{msg_id}"
PICTURE_LINK_LIFETIME = timedelta(hours=1)
PICTURE_CACHE = 40

CARD_URL = "/ezviz_hp7_static/ezviz-hp7-unlock-log-card.js"
CARD_FILE = Path(__file__).parent / "frontend" / "ezviz-hp7-unlock-log-card.js"

_DATA_SETUP = f"{DOMAIN}_unlock_log_setup"
_DATA_PICTURES = f"{DOMAIN}_unlock_pictures"


def _ext(message: dict[str, Any]) -> dict[str, Any]:
    ext = message.get("ext")
    if isinstance(ext, str):
        try:
            ext = json.loads(ext)
        except ValueError:
            return {}
    return ext if isinstance(ext, dict) else {}


def _alarm_type(ext: dict[str, Any]) -> int | None:
    try:
        return int(ext.get("alarmType"))
    except (TypeError, ValueError):
        return None


def messages_from_answer(answer: Any) -> list[dict[str, Any]]:
    """Entries of a ``get_device_messages_list`` answer."""
    items = answer.get("message") if isinstance(answer, dict) else None
    return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []


def entry_from_message(
    message: dict[str, Any], categorize: Any = None
) -> dict[str, Any] | None:
    """A log row from a message-list entry, or None if it is not an unlock.

    ``categorize(customer_type)`` returns the unlock category
    ("unlock_face", ...) the integration already derives for the unlock
    event; without it the category is left out.
    """
    ext = _ext(message)
    kind = UNLOCK_ALARM_TYPES.get(_alarm_type(ext) or 0)
    msg_id = message.get("msgId")
    if kind is None or not msg_id:
        return None
    who = ext.get("customerType") or ext.get("customType")
    who = str(who) if who else None
    try:
        when = int(message.get("time"))
    except (TypeError, ValueError):
        when = None
    category = categorize(who) if categorize and who else None
    return {
        "id": str(msg_id),
        "time": when,  # epoch milliseconds
        "time_str": message.get("timeStr"),
        "lock": kind,
        "who": who,
        "category": category,
        # Same rule as the unlock event: customerType is the enrolment name
        # for face and palm (less a "[F]"/"[P]" method prefix), and the
        # fixed method name otherwise.
        "user": unlock_user(who, category),
        "title": message.get("title"),
        # Kept server-side only: the card gets a signed link instead.
        "pic": message.get("pic") or None,
        "crypt": str(message.get("picCrypt") or "0") not in ("0", ""),
    }


def fetch_unlock_log(
    client: Any, serial: str, categorize: Any = None, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Blocking: the ``LOG_SIZE`` newest unlocks, newest first.

    The list is read day by day (it is filed by date) and paged with the
    ``time`` of the last entry of the previous page; detections fill most of
    it, so a busy day takes a few pages.
    """
    now = now or datetime.now()
    found: dict[str, dict[str, Any]] = {}
    for back in range(DAYS_BACK):
        day = (now - timedelta(days=back)).strftime("%Y%m%d")
        end_time: str = ""
        for _ in range(MAX_PAGES_PER_DAY):
            answer = client.get_device_messages_list(
                serial, limit=PAGE_SIZE, date=day, end_time=end_time
            )
            page = messages_from_answer(answer)
            for message in page:
                row = entry_from_message(message, categorize)
                if row is not None:
                    found.setdefault(row["id"], row)
            if len(page) < PAGE_SIZE:
                break
            last = page[-1].get("time")
            if not last or str(last) == end_time:
                break
            end_time = str(last)
        if len(found) >= LOG_SIZE:
            break
    rows = sorted(found.values(), key=lambda r: r["time"] or 0, reverse=True)
    return rows[:LOG_SIZE]


def _entry_data(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    return data if isinstance(data, dict) else None


def _log_of(data: dict[str, Any]) -> list[dict[str, Any]]:
    coordinator = data.get("coordinator")
    log = (getattr(coordinator, "data", None) or {}).get("unlock_log")
    return log if isinstance(log, list) else []


def _key_of(hass: HomeAssistant, entry_id: str) -> str:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return ""
    merged = {**entry.data, **entry.options}
    return str(
        merged.get(CONF_ENCRYPTION_KEY) or merged.get(CONF_VERIFICATION_CODE) or ""
    ).strip()


class UnlockPictureView(HomeAssistantView):
    """The decrypted picture of one unlock (signed link from the websocket)."""

    url = "/api/ezviz_hp7/unlock_picture/{entry_id}/{msg_id}"
    name = "api:ezviz_hp7:unlock_picture"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, msg_id: str) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        cache: OrderedDict[str, bytes] = hass.data.setdefault(_DATA_PICTURES, OrderedDict())
        cache_key = f"{entry_id}/{msg_id}"
        if cache_key in cache:
            cache.move_to_end(cache_key)
            return web.Response(body=cache[cache_key], content_type="image/jpeg")
        data = _entry_data(hass, entry_id)
        row = next((r for r in _log_of(data or {}) if r.get("id") == msg_id), None)
        if row is None or not row.get("pic"):
            return web.Response(status=404)
        try:
            session = async_get_clientsession(hass)
            async with session.get(row["pic"], timeout=15) as resp:
                if resp.status != 200:
                    return web.Response(status=502)
                body = await resp.read()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Unlock picture download failed: %s", exc)
            return web.Response(status=502)
        from .pylocalapi.constants import HIK_ENCRYPTION_HEADER
        from .pylocalapi.utils import decrypt_image

        if HIK_ENCRYPTION_HEADER in body:
            key = _key_of(hass, entry_id)
            if not key:
                return web.Response(status=409)
            try:
                body = await hass.async_add_executor_job(decrypt_image, body, key)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Unlock picture decrypt failed: %s", exc)
                return web.Response(status=502)
        cache[cache_key] = body
        while len(cache) > PICTURE_CACHE:
            cache.popitem(last=False)
        return web.Response(body=body, content_type="image/jpeg")


@websocket_api.websocket_command(
    {
        vol.Required("type"): "ezviz_hp7/unlock_log",
        vol.Optional("serial"): str,
    }
)
@callback
def ws_unlock_log(hass: HomeAssistant, connection: Any, msg: dict[str, Any]) -> None:
    """Rows of every (or one) door station, newest first, with signed picture links."""
    wanted = msg.get("serial")
    rows: list[dict[str, Any]] = []
    for entry_id, data in (hass.data.get(DOMAIN) or {}).items():
        if not isinstance(data, dict):
            continue
        serial = data.get("serial")
        if wanted and wanted not in (serial, str(serial).split("-")[0]):
            continue
        for row in _log_of(data):
            picture = None
            if row.get("pic"):
                picture = async_sign_path(
                    hass,
                    PICTURE_URL.format(entry_id=entry_id, msg_id=row["id"]),
                    PICTURE_LINK_LIFETIME,
                    refresh_token_id=connection.refresh_token_id,
                )
            rows.append(
                {
                    "serial": serial,
                    "time": row.get("time"),
                    "time_str": row.get("time_str"),
                    "lock": row.get("lock"),
                    "who": row.get("who"),
                    "user": row.get("user"),
                    "category": row.get("category"),
                    "title": row.get("title"),
                    "picture": picture,
                }
            )
    rows.sort(key=lambda r: r["time"] or 0, reverse=True)
    connection.send_result(msg["id"], {"unlocks": rows[:LOG_SIZE]})


async def async_setup_unlock_log(hass: HomeAssistant) -> None:
    """Register the view, the websocket command and the card (once)."""
    if hass.data.get(_DATA_SETUP):
        return
    hass.data[_DATA_SETUP] = True
    hass.http.register_view(UnlockPictureView())
    websocket_api.async_register_command(hass, ws_unlock_log)
    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_URL, str(CARD_FILE), False)]
        )
        from homeassistant.components.frontend import add_extra_js_url

        # The file's modification time, so a browser fetches a new version.
        version = int(CARD_FILE.stat().st_mtime) if CARD_FILE.exists() else int(time.time())
        add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("The unlock log card could not be registered: %s", exc)
