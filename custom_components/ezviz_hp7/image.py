"""Picture the door station took at the latest unlock (face, palm, card, app ...)."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_ENCRYPTION_KEY, CONF_VERIFICATION_CODE, DOMAIN
from .pylocalapi.constants import HIK_ENCRYPTION_HEADER
from .pylocalapi.utils import decrypt_image

_LOGGER = logging.getLogger(__name__)

# Fields of an alarm record that have carried the picture URL.
_PICTURE_FIELDS = ("picUrl", "pic", "picUrlGroup")


def unlock_picture_url(record: Any) -> str | None:
    """The picture URL of an unlock record, if it has one."""
    if not isinstance(record, dict):
        return None
    for field in _PICTURE_FIELDS:
        value = record.get(field)
        if isinstance(value, list):
            value = next((v for v in value if isinstance(v, str) and v), None)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            Hp7UnlockImage(
                hass, data["coordinator"], data["serial"],
                data.get("model") or "HP7", entry,
            )
        ]
    )


class Hp7UnlockImage(CoordinatorEntity, ImageEntity):
    """Updates with each unlock, so an automation can keep the picture
    (``image.snapshot``) together with who opened and how (unlock event)."""

    _attr_has_entity_name = True
    _attr_translation_key = "last_unlock"
    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, coordinator, serial: str, model: str, entry) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._serial = serial
        self._model = model
        self._entry = entry
        self._attr_unique_id = f"{DOMAIN}_{serial}_last_unlock_image"
        self._url: str | None = None
        self._unlock_time: Any = None
        self._cached: bytes | None = None
        self._update_from_data(initial=True)

    @property
    def device_info(self):
        from .device_info import make_device_info

        return make_device_info(self._serial, self._model)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        unlock = (self.coordinator.data or {}).get("unlock") or {}
        record = unlock.get("record") or {}
        return {
            "unlock_time": record.get("alarmStartTimeStr"),
            "user": unlock.get("user"),
            "category": unlock.get("category"),
        }

    def _update_from_data(self, initial: bool = False) -> bool:
        unlock = (self.coordinator.data or {}).get("unlock")
        if not isinstance(unlock, dict):
            return False
        unlock_time = unlock.get("time")
        url = unlock_picture_url(unlock.get("record"))
        if unlock_time == self._unlock_time and url == self._url:
            return False
        self._unlock_time = unlock_time
        self._url = url
        self._cached = None
        record = unlock.get("record") or {}
        # The unlock's own local time, so the state says when the door opened.
        when = dt_util.parse_datetime(str(record.get("alarmStartTimeStr") or ""))
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=dt_util.get_default_time_zone())
        self._attr_image_last_updated = when or (None if initial else dt_util.now())
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_from_data()
        super()._handle_coordinator_update()

    def _key(self) -> str:
        merged = {**self._entry.data, **self._entry.options}
        return str(
            merged.get(CONF_ENCRYPTION_KEY) or merged.get(CONF_VERIFICATION_CODE) or ""
        ).strip()

    async def async_image(self) -> bytes | None:
        if self._cached is not None:
            return self._cached
        url = self._url
        if not url:
            return None
        try:
            session = async_get_clientsession(self.hass)
            # Same request as the Last Snapshot camera.
            headers = {"User-Agent": "EZVIZ/5.0"}
            token = getattr(self.coordinator.api, "token", None) or {}
            if token.get("access_token"):
                headers["Authorization"] = f"Bearer {token['access_token']}"
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Unlock picture: HTTP %s", resp.status)
                    return None
                data = await resp.read()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Unlock picture download failed: %s", exc)
            return None
        if HIK_ENCRYPTION_HEADER in data:
            key = self._key()
            if not key:
                _LOGGER.warning(
                    "The unlock picture of %s is encrypted and no encryption "
                    "key or verification code is configured", self._serial,
                )
                return None
            try:
                data = await self.hass.async_add_executor_job(decrypt_image, data, key)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Could not decrypt the unlock picture: %s", exc)
                return None
        if url == self._url:
            self._cached = data
        return data
