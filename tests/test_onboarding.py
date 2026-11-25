import re
from datetime import datetime, timedelta, timezone

from solya.models import ConnectRequest, Invite, Owner, Site
from solya.services import onboarding as ob
from solya.solaredge.models import SiteInfo

NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


def _info(site_id, kwp, name="Site"):
    return SiteInfo(site_id, name, kwp, "Asia/Jerusalem", 32.0, 34.8, NOW, True)


def _owner(s, chat=1):
    inv = ob.create_invite(s, "admin", NOW)
    return ob.redeem_invite(s, inv, chat, "דנה", NOW)


def test_invite_token_format(db):
    with db() as s:
        tok = ob.create_invite(s, "admin", NOW)
        assert len(tok) <= 64 and re.fullmatch(r"[A-Za-z0-9_-]+", tok)
        assert s.get(Invite, tok).created_by == "admin"


def test_redeem_once(db):
    with db() as s:
        tok = ob.create_invite(s, "admin", NOW)
        owner = ob.redeem_invite(s, tok, 11, "דנה", NOW)
        assert owner.telegram_chat_id == 11
        assert ob.redeem_invite(s, tok, 22, "אבי", NOW) is None
        assert ob.redeem_invite(s, tok, 11, "דנה", NOW).id == owner.id
        assert ob.redeem_invite(s, "nope", 33, "x", NOW) is None
        assert ob.get_owner_by_chat(s, 33) is None


def test_web_claimed_invite_not_redeemable_in_bot(db):
    with db() as s:
        tok = ob.create_invite(s, "admin", NOW)
        web = ob.signup_web(s, tok, "דנה", "d@e.com", "0501234567", NOW)
        assert ob.redeem_invite(s, tok, 44, "אבי", NOW) is None
        assert ob.get_owner_by_chat(s, 44) is None
        assert s.get(Invite, tok).owner_id == web.id


def test_bot_redeem_claims_invite_conditionally(db):
    with db() as s:
        tok = ob.create_invite(s, "admin", NOW)
        s.get(Invite, tok).used_at = NOW  # claimed elsewhere, e.g. a concurrent request
        s.flush()
        assert ob.redeem_invite(s, tok, 55, "דנה", NOW) is None
        assert ob.get_owner_by_chat(s, 55) is None


def test_connect_request_ttl(db, settings):
    with db() as s:
        owner = _owner(s)
        url = ob.create_connect_request(s, owner.id, NOW, settings)
        assert url.startswith("https://solya.example/connect/")
        tok = url.rsplit("/", 1)[1]
        assert ob.resolve_connect_token(s, tok, NOW + timedelta(minutes=14), 15)
        assert ob.resolve_connect_token(s, tok, NOW + timedelta(minutes=16), 15) is None


def test_resolve_callback_by_external_id_then_state_then_cookie(db, settings):
    with db() as s:
        owner = _owner(s)
        tok = ob.create_connect_request(s, owner.id, NOW, settings).rsplit("/", 1)[1]
        req = s.get(ConnectRequest, tok)
        later = NOW + timedelta(minutes=5)
        assert ob.resolve_callback(s, req.nonce, None, None, later, 15) is req
        assert ob.resolve_callback(s, None, req.nonce, None, later, 15) is req
        assert ob.resolve_callback(s, "bogus", "bogus", tok, later, 15) is req
        assert ob.resolve_callback(s, None, None, None, later, 15) is None
        assert ob.resolve_callback(s, req.nonce, None, tok, NOW + timedelta(minutes=20), 15) is None
        req.used_at = later
        assert ob.resolve_callback(s, req.nonce, req.nonce, tok, later, 15) is None


def _req(s, settings, owner):
    tok = ob.create_connect_request(s, owner.id, NOW, settings).rsplit("/", 1)[1]
    return s.get(ConnectRequest, tok)


def test_register_site_first_flag_and_commercial(db, settings):
    with db() as s:
        owner = _owner(s)
        r1 = _req(s, settings, owner)
        site1, first1 = ob.register_site(s, r1, _info(3033498, 65.52), NOW)
        assert first1 and site1.commercial and site1.status == "active"
        assert r1.used_at == NOW and r1.site_id == 3033498
        r2 = _req(s, settings, owner)
        site2, first2 = ob.register_site(s, r2, _info(4552265, 26.55), NOW)
        assert not first2 and not site2.commercial


def test_reconnect_resets_status(db, settings):
    with db() as s:
        owner = _owner(s)
        site, _ = ob.register_site(s, _req(s, settings, owner), _info(1, 10.0), NOW)
        site.status = "reauth_needed"
        site.label_he = "משק"
        site, first = ob.register_site(s, _req(s, settings, owner), _info(1, 10.0, "New"), NOW)
        assert site.status == "active" and not first
        assert site.label_he == "משק" and site.portal_name == "New"
        assert s.query(Site).count() == 1 and s.query(Owner).count() == 1


def test_register_site_owner_change_is_logged(db, settings, caplog):
    """M4: a site moving to another owner is logged (and the callback tells the admin)."""
    import logging

    with db() as s:
        first, second = _owner(s, chat=1), _owner(s, chat=2)
        ob.register_site(s, _req(s, settings, first), _info(7, 10.0), NOW)
        with caplog.at_level(logging.WARNING, logger="solya"):
            site, _ = ob.register_site(s, _req(s, settings, second), _info(7, 10.0), NOW)
        assert site.owner_id == second.id
    assert f"site 7 reassigned from owner {first.id} to owner {second.id}" in caplog.text


def test_register_site_same_owner_not_logged(db, settings, caplog):
    import logging

    with db() as s:
        owner = _owner(s)
        ob.register_site(s, _req(s, settings, owner), _info(7, 10.0), NOW)
        with caplog.at_level(logging.WARNING, logger="solya"):
            ob.register_site(s, _req(s, settings, owner), _info(7, 10.0), NOW)
    assert "reassigned" not in caplog.text
