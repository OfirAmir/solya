"""RAY slot: a pluggable performance score; disabled by default."""
from __future__ import annotations

import importlib
from datetime import date
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from solya.models import Site

RayBand = Literal["green", "orange", "yellow", "red"]


class RayCalculator(Protocol):
    def compute(self, site: Site, local_date: date, session: Session | None) -> float | None: ...


class NullRay:
    def compute(self, site: Site, local_date: date, session: Session | None) -> float | None:
        return None


def load_ray(path: str | None) -> RayCalculator:
    """Load a calculator from ``module:Class``; ``None`` or empty disables RAY."""
    if not path:
        return NullRay()
    module_name, _, class_name = path.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"RAY implementation must look like 'module:Class', got {path!r}")
    cls = getattr(importlib.import_module(module_name), class_name)
    return cls()


def ray_band(score: float) -> RayBand:
    if score >= 90:
        return "green"
    if score >= 80:
        return "orange"
    if score >= 60:
        return "yellow"
    return "red"


def weighted_ray(items: list[tuple[float | None, float | None]]) -> float | None:
    """kWp-weighted mean of (score, kwp) pairs; None if any score is None or no kWp."""
    if not items:
        return None
    total_kwp = 0.0
    acc = 0.0
    for score, kwp in items:
        if score is None or kwp is None:
            return None
        acc += score * kwp
        total_kwp += kwp
    if total_kwp <= 0:
        return None
    return acc / total_kwp
