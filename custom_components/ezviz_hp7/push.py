"""EZVIZ cloud push (MQTT) -> immediate coordinator refresh.

The coordinator polls every 15 s (60 s while a LAN stream is watched), so a
ring or unlock could take that long to reach HA. EZVIZ pushes a message to
the app's MQTT channel as soon as the device reports an event; on a push for
this entry's device we refresh right away. The detailed alarm record (who
unlocked, how) shows up in the cloud alarm list a few seconds after the
push, so the refresh is repeated a couple of times. Polling stays as the
fallback when push is down.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

if TYPE_CHECKING:
    from .api import Hp7Api
    from .coordinator import Hp7Coordinator

_LOGGER = logging.getLogger(__name__)

# Seconds after a push at which to refresh. The push itself lags the event
# by ~3 s and the alarm record is normally listed by then; the later ones
# catch a slow cloud.
REFRESH_DELAYS = (0, 4, 10)
RETRY_START_SEC = 300


class Hp7PushListener:
    """Keep an EZVIZ push connection and refresh the coordinator on events."""

    def __init__(
        self, hass: HomeAssistant, api: Hp7Api, coordinator: Hp7Coordinator
    ) -> None:
        self._hass = hass
        self._api = api
        self._coordinator = coordinator
        # Serials a push can carry for this entry: the door station's own,
        # its bare monitor part ("BK5833838" of "BK5833838-BK5832013") and
        # the monitor serial.
        serial = coordinator.serial
        monitors = coordinator.monitor_serial or []
        if isinstance(monitors, str):
            monitors = [monitors]
        self._serials = {s for s in (serial, serial.split("-")[0], *monitors) if s}
        self._push: Any = None
        self._retry_unsub: Any = None
        # Pending refresh timers of the latest push; a new push replaces them.
        self._refresh_unsubs: list[Any] = []
        self._stopped = False

    async def async_start(self, _now: Any = None) -> None:
        """Connect; on failure retry later and rely on polling meanwhile."""
        self._retry_unsub = None
        if self._stopped:
            return
        try:
            self._push = await self._hass.async_add_executor_job(
                self._api.start_push, self._on_message
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "EZVIZ push unavailable for %s (%s); polling only, retry in %ss",
                self._coordinator.serial, exc, RETRY_START_SEC,
            )
            self._retry_unsub = async_call_later(
                self._hass, RETRY_START_SEC, self.async_start
            )
            return
        if self._stopped:  # unloaded while connecting
            await self.async_stop_push()
            return
        _LOGGER.debug("EZVIZ push connected for %s", self._coordinator.serial)

    async def async_stop(self) -> None:
        self._stopped = True
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None
        self._cancel_refreshes()
        await self.async_stop_push()

    async def async_stop_push(self) -> None:
        if self._push is not None:
            push, self._push = self._push, None
            try:
                await self._hass.async_add_executor_job(push.stop)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("EZVIZ push stop ignored: %s", exc)

    def _on_message(self, message: dict[str, Any]) -> None:
        """paho thread: hand matching pushes to the event loop."""
        ext = message.get("ext")
        serial = ext.get("device_serial") if isinstance(ext, dict) else None
        if serial not in self._serials:
            return
        _LOGGER.debug(
            "EZVIZ push for %s (alert %s), refreshing",
            serial, ext.get("alert_type_code"),
        )
        self._hass.loop.call_soon_threadsafe(self._schedule_refreshes)

    @callback
    def _schedule_refreshes(self) -> None:
        if self._stopped:
            return
        self._cancel_refreshes()
        self._refresh_unsubs = [
            async_call_later(self._hass, delay, self._refresh)
            for delay in REFRESH_DELAYS
        ]

    def _cancel_refreshes(self) -> None:
        for unsub in self._refresh_unsubs:
            unsub()
        self._refresh_unsubs = []

    async def _refresh(self, _now: Any) -> None:
        if not self._stopped:
            await self._coordinator.async_refresh()
