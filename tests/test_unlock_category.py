"""Unlock method from the enrolment name in customerType (HP7 Pro)."""
from __future__ import annotations

from custom_components.ezviz_hp7.coordinator import (
    unlock_category_from_detail,
    unlock_category_from_name,
    unlock_user,
)
from custom_components.ezviz_hp7.event import unlock_event_type


def _rec(who):
    return {"alarmType": 10242, "customerType": who}


def test_name_in_one_list():
    bio = {"unlock_face": ["Ana (F)"], "unlock_palm": ["Ana (P)"]}
    assert unlock_category_from_detail(_rec("Ana (F)"), bio) == "unlock_face"
    assert unlock_category_from_detail(_rec("Ana (P)"), bio) == "unlock_palm"
    # No prefix: the name is shown as EZVIZ sends it.
    assert unlock_user("Ana (F)", "unlock_face") == "Ana (F)"


def test_same_name_for_face_and_palm():
    bio = {"unlock_face": ["Ana", "Luis"], "unlock_palm": ["Ana", "Luis"]}
    # Cannot be told: no category, so the event says "other".
    assert unlock_category_from_detail(_rec("Ana"), bio) is None
    assert unlock_event_type({"category": None, "record": _rec("Ana")}) == "other"


def test_method_prefix():
    bio = {"unlock_face": ["[F] Ana"], "unlock_palm": ["[P] Ana"]}
    assert unlock_category_from_name("[F] Ana", bio) == "unlock_face"
    assert unlock_category_from_name("[P] Ana", bio) == "unlock_palm"
    # The prefix names the method even before the enrolment lists are read.
    assert unlock_category_from_name("[F]Ana", None) == "unlock_face"
    assert unlock_user("[F] Ana", "unlock_face") == "Ana"
    assert unlock_user("[P]Ana", "unlock_palm") == "Ana"
    assert unlock_category_from_name("[Q] Ana", None) == "unlock_qr"
    assert unlock_user("[Q] Ana", "unlock_qr") == "Ana"
    unlock = {"category": "unlock_qr", "record": _rec("[Q] Ana")}
    assert unlock_event_type(unlock) == "qr"
    # Only the exact prefixes at the start count.
    assert unlock_category_from_name("Ana [F]", None) is None
    assert unlock_category_from_name("[f] Ana", None) is None
    assert unlock_category_from_name("[F]", None) is None


def test_fixed_method_names():
    bio = {"unlock_face": ["Ana"], "unlock_palm": ["Ana"]}
    assert unlock_category_from_detail(_rec("Card"), bio) == "unlock_rfid"
    assert unlock_category_from_detail(_rec("EZVIZ app"), bio) == "unlock_app"
    assert unlock_category_from_detail(_rec("Someone"), bio) is None
    assert unlock_user("Card", "unlock_rfid") is None
