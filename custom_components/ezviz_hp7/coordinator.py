"""Data update coordinator for EZVIZ HP7."""
from __future__ import annotations

import inspect
import logging
import time
from datetime import timedelta
from typing import Any, TYPE_CHECKING

from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN, UPDATE_INTERVAL_SEC

# alarmType of a door-lock unlock record in the EZVIZ alarm list.
ALARM_TYPE_UNLOCK_LOCK = 10242
# Lock (10242) and gate (10243) openings, both kept in the unlock log.
UNLOCK_LOG_TYPES = (10242, 10243)
UNLOCK_LOG_MAX_AGE = 1800.0
# Repairs notice id prefix for a monitor whose settings (ChimeMusic) fail.
MONITOR_SETTINGS_ISSUE = "monitor_settings_"


# Categories whose customerType is an enrolment name, not a method name.
BIOMETRIC_CATEGORIES = ("unlock_face", "unlock_palm", "unlock_qr")

# Opt-in naming convention (README): an enrolment name that starts with one
# of these prefixes ("[F] Oscar") names its method. EZVIZ reports face and
# palm unlocks only by the enrolment name, so this is the way to tell them
# apart when a person is enrolled by face and palm under the same name.
# "[Q]" (QR code) is untested: no QR unlock record has been seen yet.
METHOD_PREFIXES = {
    "[F]": "unlock_face",
    "[P]": "unlock_palm",
    "[Q]": "unlock_qr",
}

# customerType of the non-biometric unlock methods seen on HP7 / HP7 Pro.
UNLOCK_CATEGORY_BY_CUSTOMER = {
    "Card": "unlock_rfid",
    "EZVIZ app": "unlock_app",
    "Passcode": "unlock_code",
}


def split_method_prefix(name: str) -> tuple[str | None, str]:
    """(category, name without the prefix) for "[F] Oscar"; (None, name) otherwise."""
    for prefix, category in METHOD_PREFIXES.items():
        if name.startswith(prefix):
            rest = name[len(prefix):].strip()
            if rest:
                return category, rest
    return None, name


def unlock_category_from_name(
    who: str | None, biometric_names: dict[str, list[str]] | None
) -> str | None:
    """Unlock category ("unlock_face", "unlock_palm", ...) from a customerType.

    The HP7 Pro puts the enrolment name in ``customerType`` for face and palm
    unlocks ("Oscar unlock the lock"); no field of the alarm record, message
    list or push says "face" or "palm". In order:
      1. a "[F]" / "[P]" name prefix (METHOD_PREFIXES) names the method;
      2. a name in exactly one of the device's FaceInfo/VeinInfo lists
         (a name in both lists gives no category: it cannot be told);
      3. card, app and code unlocks carry a fixed ``customerType``
         ("Card", "EZVIZ app", "Passcode").
    """
    if not isinstance(who, str) or not who:
        return None
    category, _ = split_method_prefix(who)
    if category:
        return category
    if biometric_names:
        hits = [cat for cat, names in biometric_names.items() if who in names]
        if len(hits) == 1:
            return hits[0]
    return UNLOCK_CATEGORY_BY_CUSTOMER.get(who)


def unlock_category_from_detail(
    detail: dict[str, Any] | None, biometric_names: dict[str, list[str]] | None
) -> str | None:
    """Unlock category of a lock-unlock alarm record (see unlock_category_from_name)."""
    if not isinstance(detail, dict) or detail.get("alarmType") != ALARM_TYPE_UNLOCK_LOCK:
        return None
    return unlock_category_from_name(detail.get("customerType"), biometric_names)


def unlock_user(who: str | None, category: str | None) -> str | None:
    """Who unlocked: the enrolment name for face/palm/QR, less a method prefix.

    Other methods name the method, not a person, so they give None.
    """
    if not who or category not in BIOMETRIC_CATEGORIES:
        return None
    return split_method_prefix(who)[1]


if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from .api import Hp7Api

_LOGGER = logging.getLogger(__name__)


class Hp7Coordinator(DataUpdateCoordinator):
    """Manage periodic data updates from EZVIZ HP7 API.
    
    This coordinator handles fetching device status and sensor data
    at regular intervals and distributing updates to all entities.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: Hp7Api,
        serial: str,
        monitor_serial: str | None = None,
        config_entry: ConfigEntry | None = None,
    ) -> None:
        """Initialize coordinator.

        Args:
            hass: Home Assistant instance.
            api: EZVIZ HP7 API instance.
            serial: Camera serial number.
            monitor_serial: Optional indoor monitor serial.
            config_entry: HA config entry. Required by recent HA versions
                (2024.12+) to use async_config_entry_first_refresh; older
                releases accept it as a no-op kwarg too.
        """
        # Newer HA (2024.10+) requires DataUpdateCoordinator subclasses to
        # carry their owning ConfigEntry before async_config_entry_first_refresh
        # can be used. The kwarg name is `config_entry` — pass it through when
        # the base class actually declares it, fall back to plain init on
        # older builds.
        kwargs: dict[str, Any] = {
            "name": "EZVIZ HP7",
            "update_interval": timedelta(seconds=UPDATE_INTERVAL_SEC),
        }
        params = inspect.signature(DataUpdateCoordinator.__init__).parameters
        if "config_entry" in params and config_entry is not None:
            kwargs["config_entry"] = config_entry
        super().__init__(hass, _LOGGER, **kwargs)

        # Some HA builds accept the kwarg but don't set the attribute, or
        # reload paths arrive here without a current_entry context: assign
        # explicitly so async_config_entry_first_refresh's check passes.
        if config_entry is not None and getattr(self, "config_entry", None) is None:
            try:
                self.config_entry = config_entry  # type: ignore[assignment]
            except AttributeError:
                pass

        self.api = api
        self.serial = serial
        self.monitor_serial = monitor_serial
        # The EZVIZ cloud (pagelist) hiccups often — 504 / euauth timeouts /
        # brief rate-limits — usually recovering within a cycle or two. Rather
        # than blanking every cloud-backed entity to "unknown" on the first
        # blip, tolerate a short run of failures by holding the last-good data,
        # and only surface UpdateFailed (entities unavailable) once the outage
        # is sustained. 4 cycles * 15 s ≈ 1 min of grace (#36 andresako).
        self._fail_streak: int = 0
        self._last_alarm_time: str | None = None
        self._last_alarm_detail: dict[str, Any] | None = None
        # Key list cache (RFID cards / face / palm enrolments) — refresh
        # every 5 min to keep KeyMgr API load minimal while still picking up
        # new enrolments / renames within a polling cycle or two.
        self._key_list: list[dict[str, Any]] | None = None
        self._key_list_age: int = 10**6  # force first fetch
        self._biometric_names: dict[str, list[str]] | None = None
        # Unlock log (message list): rows, the newest unlock alarm it has
        # seen, and when it was read (monotonic).
        self._unlock_log: list[dict[str, Any]] | None = None
        self._unlock_log_marker: Any = None
        self._unlock_log_read: float = 0.0
        self._last_unlock: dict[str, Any] | None = None
        # Relay handle, set by the camera platform once it exists. Used to
        # back polling off while someone is watching the LAN stream (#44).
        self.live_relay: Any = None
        self.live_relay_sub: Any = None
        self._slowed_for_stream = False

    def _apply_stream_aware_interval(self) -> None:
        """Poll slower while a local stream has viewers (#44).

        AnthoPakPak's finding: when cloud calls stopped, the LAN stream went
        from severely stuttering to perfectly smooth for hours. Several of
        the endpoints we poll are proxied to the doorbell, and answering them
        competes with its streaming task on the device's small CPU. So while
        someone is actually watching, poll at a quarter of the rate; restore
        the normal cadence as soon as the last viewer leaves.
        """
        watching = any(
            relay is not None
            and getattr(relay, "_active_lan", False)
            and getattr(relay, "_active_clients", 0)
            - getattr(relay, "_snapshot_clients", 0) > 0
            for relay in (self.live_relay, self.live_relay_sub)
        )
        if watching == self._slowed_for_stream:
            return
        self._slowed_for_stream = watching
        self.update_interval = timedelta(
            seconds=UPDATE_INTERVAL_SEC * (4 if watching else 1)
        )
        _LOGGER.debug(
            "EZVIZ HP7: poll interval -> %ss (local stream viewers: %s)",
            self.update_interval.total_seconds(), watching,
        )

    def _sync_monitor_issues(self) -> None:
        """Raise / clear a Repairs notice per monitor whose settings fail."""
        from homeassistant.helpers import issue_registry as ir

        monitors = self.monitor_serial
        if isinstance(monitors, str):
            monitors = [monitors]
        for ms in monitors or []:
            issue_id = f"{MONITOR_SETTINGS_ISSUE}{ms}"
            problem = self.api.chime_problem(ms)
            if problem is None:
                ir.async_delete_issue(self.hass, DOMAIN, issue_id)
                continue
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key=f"monitor_settings_{problem['kind']}",
                translation_placeholders={
                    "serial": ms,
                    "minutes": str(problem.get("minutes", "")),
                    "error": str(problem.get("error") or "")[:200],
                },
            )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch latest device status from API.

        Called periodically to update all coordinator data. When the
        ``last_alarm_time`` field changes between polls we also call the
        EZVIZ alarminfo endpoint once to fetch the detailed alarm record
        (RFID card id, picture id, etc.) and embed it under
        ``latest_alarm_detail``.
        """
        self._apply_stream_aware_interval()
        # A blip is either an exception OR an empty dict (get_status swallows
        # RequestException and returns {}). Treat both the same: hold the last
        # good data for a short grace window before degrading to unavailable.
        try:
            data = await self.hass.async_add_executor_job(
                self.api.get_status, self.serial, self.monitor_serial
            )
            failure: str | None = None if data else "empty status from cloud"
        except Exception as exc:  # noqa: BLE001
            data = None
            failure = str(exc)
        self._sync_monitor_issues()

        if failure is not None:
            self._fail_streak += 1
            # Keep serving the previous cycle's values while the outage is
            # short and we actually have something to serve.
            if self._fail_streak <= 4 and self.data:
                _LOGGER.warning(
                    "EZVIZ cloud fetch failed (%d in a row), keeping last "
                    "known values: %s",
                    self._fail_streak,
                    failure,
                )
                return self.data
            # Right past the grace window, try re-authenticating once: an
            # expired session would otherwise fail every poll until restart,
            # which matches "unknown for hours" reports (#36). Do it a single
            # time so we don't hammer login (and trip rate-limits) during a
            # genuine cloud outage.
            if self._fail_streak == 5:
                _LOGGER.warning(
                    "EZVIZ cloud failing for %d cycles — forcing re-login",
                    self._fail_streak,
                )
                await self.hass.async_add_executor_job(self.api.force_relogin)
            # Sustained outage (or no prior data): mark entities unavailable.
            raise UpdateFailed(f"EZVIZ cloud fetch failed: {failure}")

        # Success — clear the streak and continue with fresh data.
        self._fail_streak = 0

        alarm_time = data.get("last_alarm_time")
        if alarm_time and alarm_time != self._last_alarm_time:
            recent = await self.hass.async_add_executor_job(
                self.api.get_recent_alarms, self.serial
            )
            if recent:
                self._last_alarm_detail = recent[0]
                # Newest lock-unlock record, which a detection alarm right
                # after it would otherwise hide from the sensors.
                self._last_unlock = next(
                    (
                        a for a in recent
                        if a.get("alarmType") == ALARM_TYPE_UNLOCK_LOCK
                    ),
                    self._last_unlock,
                )
            self._last_alarm_time = alarm_time
            if recent:
                newest_unlock = next(
                    (
                        a.get("alarmStartTime") for a in recent
                        if a.get("alarmType") in UNLOCK_LOG_TYPES
                    ),
                    None,
                )
                if newest_unlock is not None and newest_unlock != self._unlock_log_marker:
                    self._unlock_log_marker = newest_unlock
                    self._unlock_log_read = 0.0  # read the log again now
        if self._last_alarm_detail is not None:
            data["latest_alarm_detail"] = self._last_alarm_detail

        # Refresh key list every ~5 min (with UPDATE_INTERVAL_SEC = 15 that's
        # one fetch every 20 polling cycles).
        self._key_list_age += 1
        if self._key_list_age >= 20 or self._key_list is None:
            keys = await self.hass.async_add_executor_job(
                self.api.get_key_list, self.serial
            )
            if keys is not None:
                self._key_list = keys
            bio = await self.hass.async_add_executor_job(
                self.api.get_biometric_key_names, self.serial
            )
            if bio is not None:
                self._biometric_names = bio
            self._key_list_age = 0
        if self._key_list is not None:
            data["keys"] = self._key_list

        # After the key list: the log names face/palm from the enrolments.
        await self._async_refresh_unlock_log()
        if self._unlock_log is not None:
            data["unlock_log"] = self._unlock_log
        if self._last_unlock is not None:
            category = unlock_category_from_detail(
                self._last_unlock, self._biometric_names
            )
            data["unlock"] = {
                "time": self._last_unlock.get("alarmStartTime"),
                "category": category,
                # Face/palm records name the enrolment ("Oscar",
                # "[F] Oscar"); other methods name the method.
                "user": unlock_user(
                    self._last_unlock.get("customerType"), category
                ),
                # customerType exactly as EZVIZ sent it.
                "name": self._last_unlock.get("customerType"),
                # Palm records leave the template unfilled:
                # "{object} unlock the lock".
                "message": str(self._last_unlock.get("alarmMessage") or "").replace(
                    "{object}", str(self._last_unlock.get("customerType") or "")
                ),
                "record": self._last_unlock,
            }

        return data

    async def _async_refresh_unlock_log(self) -> None:
        """Read the unlock log at start, after a new unlock, and twice an hour.

        A gate opening (10243) or an unlock hidden between two polls is
        caught by the half-hourly read at the latest.
        """
        if (
            self._unlock_log is not None
            and self._unlock_log_read
            and time.monotonic() - self._unlock_log_read < UNLOCK_LOG_MAX_AGE
        ):
            return
        self._unlock_log_read = time.monotonic()
        names = self._biometric_names

        def _categorize(who: str) -> str | None:
            return unlock_category_from_detail(
                {"alarmType": ALARM_TYPE_UNLOCK_LOCK, "customerType": who}, names
            )

        log = await self.hass.async_add_executor_job(
            self.api.fetch_unlock_log, self.serial, _categorize
        )
        if log is not None:
            self._unlock_log = log
