"""A shared account borrows the LAN key from the owner's entry."""
from __future__ import annotations

import pytest

from custom_components.ezviz_hp7.api import Hp7Api

SERIAL = "BK0000000-BK0000001"
KEY = b"0123456789abcdef"


def _api(user: str, key: bytes | None) -> Hp7Api:
    api = Hp7Api(username=user, password="x", token={"username": user})
    calls = []

    def fetch(bare: str) -> bytes:
        calls.append(bare)
        if key is None:
            raise RuntimeError("invalid LAN AES key from CAS (Result=1052175, key='')")
        return key

    api._fetch_lan_aes_key_from_cas = fetch  # type: ignore[method-assign]
    api.calls = calls  # type: ignore[attr-defined]
    return api


def test_own_key_used_first() -> None:
    owner = _api("owner", KEY)
    shared = _api("shared", KEY)
    shared.lan_key_fallbacks = lambda: [owner, shared]
    assert shared.fetch_lan_aes_key(SERIAL) == KEY
    assert owner.calls == []


def test_shared_account_borrows_owner_key_and_caches_it() -> None:
    owner = _api("owner", KEY)
    shared = _api("shared", None)
    shared.lan_key_fallbacks = lambda: [shared, owner]
    assert shared.fetch_lan_aes_key(SERIAL) == KEY
    assert owner.calls == shared.calls
    assert shared.fetch_lan_aes_key(SERIAL) == KEY
    assert len(shared.calls) == 1


def test_no_other_account_raises_original_error() -> None:
    shared = _api("shared", None)
    shared.lan_key_fallbacks = lambda: [shared, _api("shared", KEY)]
    with pytest.raises(RuntimeError, match="1052175"):
        shared.fetch_lan_aes_key(SERIAL)
