from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from solya.db import session_scope
from solya.models import FaultRecord, Owner, Site

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def _owner_and_site(s):
    owner = Owner(display_name="Oren", telegram_chat_id=1001, created_at=NOW)
    s.add(owner)
    s.flush()
    site = Site(
        se_site_id=42,
        owner_id=owner.id,
        portal_name="Farm",
        commercial=False,
        tz="Asia/Jerusalem",
        created_at=NOW,
    )
    s.add(site)
    s.flush()
    return owner, site


def test_site_defaults(db):
    with session_scope(db) as s:
        owner, site = _owner_and_site(s)
        assert owner.active is True
        assert owner.locale == "he"
    with session_scope(db) as s:
        site = s.get(Site, 42)
        assert site.status == "active"
        assert site.comm_state == "ok"
        assert site.income_basis == "production"
        assert site.expected_inverters == []
        assert site.card_name is None


def test_fault_key_unique_per_site(db):
    with session_scope(db) as s:
        _owner_and_site(s)
        s.add(FaultRecord(site_id=42, key="k1", source="status", code="X", first_seen=NOW))
    with pytest.raises(IntegrityError):
        with session_scope(db) as s:
            s.add(FaultRecord(site_id=42, key="k1", source="status", code="X", first_seen=NOW))


def test_utc_roundtrip(db):
    with session_scope(db) as s:
        _owner_and_site(s)
        s.add(FaultRecord(site_id=42, key="k2", source="s", code="C", first_seen=NOW))
    with session_scope(db) as s:
        got = s.query(FaultRecord).filter_by(key="k2").one().first_seen
        assert got.tzinfo is not None
        assert got == NOW
        assert got.utcoffset().total_seconds() == 0
