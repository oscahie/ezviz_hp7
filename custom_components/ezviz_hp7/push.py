"""EZVIZ cloud push -> immediate coordinator refresh.

The coordinator polls every 15 s (60 s while a LAN stream is watched), so a
ring or unlock could take that long to reach HA. EZVIZ pushes a message as
soon as the device reports an event; on a push for this entry's device we
refresh right away. The detailed alarm record (who unlocked, how) shows up in
the cloud alarm list a few seconds after the push, so the refresh is repeated
a couple of times. Polling stays as the fallback when push is down.

Two transports, one at a time:

* channel 99 (preferred) - the long link the mobile app uses. Carries
  doorbell calls as well as unlocks and detections.
* legacy MQTT push - unlocks and detections only, never calls. Used when
  channel 99 can't start (no stored password, MFA demanded for the app
  profile, registration refused) or stops for good; a repair issue says so.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import DOMAIN

if TYPE_CHECKING:
    from .api import Hp7Api
    from .coordinator import Hp7Coordinator

_LOGGER = logging.getLogger(__name__)

# Seconds after a push at which to refresh. The push itself lags the event
# by ~3 s and the alarm record is normally listed by then; the later ones
# catch a slow cloud.
REFRESH_DELAYS = (0, 4, 10)
RETRY_START_SEC = 300
# How often to check that the channel-99 worker hasn't given up.
HEALTH_CHECK = timedelta(seconds=60)
# After falling back, try channel 99 again this much later.
CHANNEL99_RETRY_SEC = 6 * 3600
_SAVE_TIMEOUT = 30


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
        entry = getattr(coordinator, "config_entry", None)
        entry_id = getattr(entry, "entry_id", None) or serial
        self._store: Store = Store(
            hass, 1, f"{DOMAIN}.{entry_id}.channel99", private=True
        )
        self._issue_id = f"channel99_unavailable_{serial}"
        self._push: Any = None
        self._mode: str | None = None  # "channel99" | "legacy"
        self._retry_unsub: Any = None
        self._health_unsub: Any = None
        self._channel99_retry_unsub: Any = None
        # Pending refresh timers of the latest push; a new push replaces them.
        self._refresh_unsubs: list[Any] = []
        self._stopped = False

    @property
    def mode(self) -> str | None:
        return self._mode

    # -- lifecycle ---------------------------------------------------------
    async def async_start(self, _now: Any = None) -> None:
        """Connect channel 99, else the legacy push, else poll and retry."""
        self._retry_unsub = None
        if self._stopped:
            return
        if await self._async_start_channel99():
            return
        await self._async_start_legacy()

    async def _async_start_channel99(self) -> bool:
        state = await self._store.async_load() or {}
        if not isinstance(state, dict):
            state = {}
        try:
            push = await self._hass.async_add_executor_job(
                self._api.start_channel99, state, self._save_state, self._on_message
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning(
                "EZVIZ channel-99 push unavailable for %s (%s); doorbell calls "
                "fall back to polling",
                self._coordinator.serial, exc,
            )
            self._raise_issue(str(exc))
            return False
        if self._stopped:  # unloaded while connecting
            await self._hass.async_add_executor_job(push.stop)
            return True
        self._push, self._mode = push, "channel99"
        ir.async_delete_issue(self._hass, DOMAIN, self._issue_id)
        self._health_unsub = async_track_time_interval(
            self._hass, self._async_check_channel99, HEALTH_CHECK
        )
        _LOGGER.info("EZVIZ channel-99 push connected for %s", self._coordinator.serial)
        return True

    async def _async_start_legacy(self, _now: Any = None) -> None:
        self._retry_unsub = None
        if self._stopped:
            return
        try:
            push = await self._hass.async_add_executor_job(
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
        if self._stopped:
            await self._hass.async_add_executor_job(push.stop)
            return
        self._push, self._mode = push, "legacy"
        # Channel 99 may come back (e.g. after the user clears an MFA prompt
        # in the app); try it again now and then.
        self._channel99_retry_unsub = async_call_later(
            self._hass, CHANNEL99_RETRY_SEC, self._async_retry_channel99
        )
        _LOGGER.debug("EZVIZ legacy push connected for %s", self._coordinator.serial)

    async def _async_check_channel99(self, _now: Any = None) -> None:
        push = self._push
        if self._stopped or self._mode != "channel99" or push is None:
            return
        if not push.failed:
            return
        _LOGGER.warning(
            "EZVIZ channel-99 push for %s stopped for good (%s); switching to "
            "the legacy push",
            self._coordinator.serial, push.diagnostics().get("fatal_error_type"),
        )
        self._raise_issue("the long link stopped")
        await self.async_stop_push()
        await self._async_start_legacy()

    async def _async_retry_channel99(self, _now: Any = None) -> None:
        self._channel99_retry_unsub = None
        if self._stopped or self._mode != "legacy":
            return
        legacy = self._push
        if await self._async_start_channel99():
            if legacy is not None:
                try:
                    await self._hass.async_add_executor_job(legacy.stop)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("EZVIZ legacy push stop ignored: %s", exc)
            return
        self._channel99_retry_unsub = async_call_later(
            self._hass, CHANNEL99_RETRY_SEC, self._async_retry_channel99
        )

    async def async_stop(self) -> None:
        self._stopped = True
        for name in ("_retry_unsub", "_health_unsub", "_channel99_retry_unsub"):
            unsub = getattr(self, name)
            if unsub is not None:
                unsub()
                setattr(self, name, None)
        self._cancel_refreshes()
        await self.async_stop_push()

    async def async_stop_push(self) -> None:
        if self._health_unsub is not None:
            self._health_unsub()
            self._health_unsub = None
        if self._push is not None:
            push, self._push, self._mode = self._push, None, None
            try:
                await self._hass.async_add_executor_job(push.stop)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("EZVIZ push stop ignored: %s", exc)

    @callback
    def _raise_issue(self, reason: str) -> None:
        ir.async_create_issue(
            self._hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="channel99_unavailable",
            translation_placeholders={
                "serial": self._coordinator.serial,
                "reason": reason,
            },
        )

    def _save_state(self, snapshot: dict[str, Any]) -> None:
        """Worker thread: persist the long-link keys before returning."""
        asyncio.run_coroutine_threadsafe(
            self._store.async_save(snapshot), self._hass.loop
        ).result(_SAVE_TIMEOUT)

    # -- messages ----------------------------------------------------------
    def _on_message(self, message: dict[str, Any]) -> None:
        """Push thread: hand matching pushes to the event loop."""
        ext = message.get("ext")
        serial = ext.get("device_serial") if isinstance(ext, dict) else None
        if serial not in self._serials:
            _LOGGER.debug(
                "EZVIZ push (%s) for another device (%s), ignored", self._mode, serial
            )
            return
        _LOGGER.debug(
            "EZVIZ push (%s) for %s (pushType %s, alert %s), refreshing",
            self._mode, serial, message.get("pushType"), ext.get("alert_type_code"),
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
