"""Event entity for EZVIZ HP7 door unlocks: who opened the door, how and when."""
from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from homeassistant.components.event import EventEntity
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import Hp7Coordinator

_LOGGER = logging.getLogger(__name__)

# Coordinator unlock category -> event type.
EVENT_TYPE_BY_CATEGORY = {
    "unlock_face": "face",
    "unlock_palm": "palm",
    "unlock_rfid": "card",
    "unlock_code": "code",
    "unlock_app": "app",
}
EVENT_TYPES = [*EVENT_TYPE_BY_CATEGORY.values(), "monitor", "other"]
# Logbook wording per event type.
EVENT_LOG_LABELS = {
    "face": "Face",
    "palm": "Palm",
    "card": "Card",
    "code": "Passcode",
    "app": "EZVIZ app",
    "monitor": "Monitor",
    "other": "Unlock",
}


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Set up the unlock event entity."""
    data: dict[str, Any] = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            Hp7UnlockEvent(
                data["coordinator"], data["serial"], data.get("model") or "HP7"
            )
        ]
    )


def unlock_event_type(unlock: dict[str, Any]) -> str:
    """Event type for a coordinator ``unlock`` entry."""
    event_type = EVENT_TYPE_BY_CATEGORY.get(unlock.get("category") or "")
    if event_type:
        return event_type
    record = unlock.get("record") or {}
    if record.get("customerType") == "Monitor":
        return "monitor"
    return "other"


class Hp7UnlockEvent(CoordinatorEntity, EventEntity):
    """Fires once per lock-unlock record, so the history lists every unlock.

    Event type is the method (face, palm, card, code, app, monitor, other),
    or "<Method> - <user>" (e.g. "Palm - Oscar") when a face/palm unlock
    names its enrolment, so the entity's state shows who opened the door.
    Such types are added to ``event_types`` as they first appear; the
    plain method is always in the ``method`` attribute for automations.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "unlock"
    _attr_event_types = EVENT_TYPES
    _attr_icon = "mdi:lock-open-variant"

    def __init__(self, coordinator: "Hp7Coordinator", serial: str, model: str) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._model = model
        self._attr_unique_id = f"{DOMAIN}_{serial}_event_unlock"
        # Prime on the first update: the cloud always reports the last
        # unlock, however old, and it must not replay after a restart.
        self._primed = False
        self._prev_unlock_time: Any = None
        self._attr_event_types = list(EVENT_TYPES)

    @callback
    def _handle_coordinator_update(self) -> None:
        unlock = (self.coordinator.data or {}).get("unlock")
        unlock_time = unlock.get("time") if isinstance(unlock, dict) else None
        if not self._primed:
            self._primed = True
            self._prev_unlock_time = unlock_time
            return
        if unlock_time is None or unlock_time == self._prev_unlock_time:
            return
        self._prev_unlock_time = unlock_time
        # alarmStartTimeStr is local time; the HP7 Pro's epoch alarmStartTime
        # runs an hour ahead of it, so the string is the one to show.
        when = (unlock.get("record") or {}).get("alarmStartTimeStr")
        method = unlock_event_type(unlock)
        user = unlock.get("user")
        label = EVENT_LOG_LABELS.get(method, method)
        event_type = f"{label} - {user}" if user else method
        if event_type not in self._attr_event_types:
            self._attr_event_types.append(event_type)
        self._trigger_event(
            event_type,
            {
                "method": method,
                "user": user,
                "message": unlock.get("message"),
                "unlock_time": when,
            },
        )
        _LOGGER.debug("Unlock event %s (%s) at %s", event_type, user, when)
        self.async_write_ha_state()
        # Event entities only show the type in the UI; a logbook line makes
        # the who/how/when history readable in the Logbook.
        if "logbook" in self.hass.config.components:
            from homeassistant.components.logbook import async_log_entry

            state = self.hass.states.get(self.entity_id)
            async_log_entry(
                self.hass,
                state.name if state else "Unlock",
                f"{label} - {user}" if user else label,
                DOMAIN,
                self.entity_id,
            )

    @property
    def device_info(self):
        """Return device information."""
        from .device_info import make_device_info
        return make_device_info(self._serial, self._model)
