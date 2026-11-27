"""Start without the EZVIZ cloud: the port-8000 live video keeps working."""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.ezviz_hp7 import CLOUD_RETRY_INTERVAL
from custom_components.ezviz_hp7.api import Hp7Api
from custom_components.ezviz_hp7.const import DOMAIN

SERIAL = "BK0000000-BK0000001"


@pytest.fixture(autouse=True)
def _custom(hass, enable_custom_integrations):
    """Load the integration from custom_components; its media dependencies
    (ffmpeg, stream) need native libraries the tests do not install."""
    hass.config.components.update({"ffmpeg", "stream"})


def _entry(source="local_sdk", code="ABCDEF"):
    return MockConfigEntry(
        domain=DOMAIN,
        title="Door",
        data={"username": "u", "password": "p", "region": "eu", "serial": SERIAL},
        options={"stream_source": source, "verification_code": code},
    )


def _store(hass_storage, entry, data):
    hass_storage[f"{DOMAIN}.local.{entry.entry_id}"] = {
        "version": 1,
        "minor_version": 1,
        "key": f"{DOMAIN}.local.{entry.entry_id}",
        "data": data,
    }


def _issue(hass, entry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"cloud_offline_{entry.entry_id}")


async def _setup(hass, entry, login_error=True):
    live = AsyncMock()
    login = patch.object(
        Hp7Api, "login", side_effect=RuntimeError("no internet") if login_error else None
    )
    with login, patch(
        "custom_components.ezviz_hp7.live_camera.async_setup_live_entities", live
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return live


async def test_starts_cameras_only_with_stored_ip(hass, hass_storage):
    entry = _entry()
    entry.add_to_hass(hass)
    _store(hass_storage, entry, {"ip": "10.0.0.50", "camera_key": "KEY123", "model": "HP7 Pro"})

    live = await _setup(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    live.assert_awaited_once()
    data = hass.data[DOMAIN][entry.entry_id]
    assert data["offline"] and data["coordinator"] is None
    assert data["model"] == "HP7 Pro"
    api: Hp7Api = data["api"]
    with patch.object(Hp7Api, "ensure_client", side_effect=AssertionError("no cloud")):
        assert api.get_local_ip(SERIAL) == "10.0.0.50"
        assert api.get_camera_encryption_key(SERIAL) == "KEY123"
    assert _issue(hass, entry) is not None
    # Only the camera platform was set up, and only it is unloaded.
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_reloads_when_the_cloud_answers(hass, hass_storage):
    entry = _entry()
    entry.add_to_hass(hass)
    _store(hass_storage, entry, {"ip": "10.0.0.50"})
    await _setup(hass, entry)

    with patch.object(Hp7Api, "login", return_value=True), patch(
        "homeassistant.config_entries.ConfigEntries.async_schedule_reload"
    ) as reload:
        async_fire_time_changed(hass, dt_util.utcnow() + CLOUD_RETRY_INTERVAL + timedelta(seconds=1))
        await hass.async_block_till_done(wait_background_tasks=True)
    reload.assert_called_once_with(entry.entry_id)
    await hass.config_entries.async_unload(entry.entry_id)


async def test_not_ready_without_stored_ip_or_other_source(hass, hass_storage):
    entry = _entry()
    entry.add_to_hass(hass)
    await _setup(hass, entry)
    assert entry.state is ConfigEntryState.SETUP_RETRY

    cloud = _entry(source="cloud")
    cloud.add_to_hass(hass)
    _store(hass_storage, cloud, {"ip": "10.0.0.50"})
    await _setup(hass, cloud)
    assert cloud.state is ConfigEntryState.SETUP_RETRY

    no_code = _entry(code="")
    no_code.add_to_hass(hass)
    _store(hass_storage, no_code, {"ip": "10.0.0.50"})
    await _setup(hass, no_code)
    assert no_code.state is ConfigEntryState.SETUP_RETRY


def test_remembered_local_round_trip():
    api = Hp7Api("u", "p")
    assert api.remembered_local(SERIAL) == {}
    api.seed_local(SERIAL, "10.0.0.50", "KEY")
    assert api.remembered_local(SERIAL) == {"ip": "10.0.0.50", "camera_key": "KEY"}
    # Stored per bare serial, so the door station's own serial finds it too.
    assert api.remembered_local(SERIAL.split("-")[0])["ip"] == "10.0.0.50"
