"""Stub checkout: records an intent to pay; no provider is called and nothing is charged."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from solya.models import Owner, Payment


def create_stub_checkout(
    session: Session,
    owner_id: int,
    amount_agorot: int | None,
    is_estimate: bool,
    description: str,
    now: datetime,
) -> Payment:
    owner = session.get(Owner, owner_id)
    if owner is None:
        raise ValueError(f"unknown owner {owner_id}")
    payment = Payment(
        owner_id=owner_id,
        amount_agorot=amount_agorot,
        amount_is_estimate=is_estimate,
        provider="stub",
        status="stub_pending",
        description=description,
        created_at=now,
    )
    session.add(payment)
    owner.subscription_status = "pending_payment"
    session.flush()
    return payment
