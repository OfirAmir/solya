"""RAY slot: a pluggable performance score; disabled by default."""
from __future__ import annotations

import importlib
import inspect
from datetime import date
from typing import Literal, Protocol

from sqlalchemy.orm import Session

from solya.models import Site

RayBand = Literal["green", "orange", "yellow", "red"]


class RayCalculator(Protocol):
    def compute(
        self, site: Site, local_date: date, session: Session | None, actual_wh: float | None = None
    ) -> float | None: ...


class NullRay:
    def compute(
        self, site: Site, local_date: date, session: Session | None, actual_wh: float | None = None
    ) -> float | None:
        return None


def _takes_one_argument(cls: type) -> bool:
    try:
        params = list(inspect.signature(cls).parameters.values())
    except (TypeError, ValueError):
        return False
    positional = [
        p for p in params if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
    ]
    return len(positional) >= 1


def load_ray(path: str | None, settings: object | None = None) -> RayCalculator:
    """Load a calculator from ``module:Class``; ``None`` or empty disables RAY.

    A class whose constructor takes one argument gets ``settings``; others get none.
    """
    if not path:
        return NullRay()
    module_name, _, class_name = path.partition(":")
    if not module_name or not class_name:
        raise ValueError(f"RAY implementation must look like 'module:Class', got {path!r}")
    cls = getattr(importlib.import_module(module_name), class_name)
    if _takes_one_argument(cls):
        return cls(settings)
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
    """Mean of (score, weight) pairs weighted by the day's production Wh.

    Items with a None score (or None weight) are skipped; None if nothing remains or the
    total weight is not positive.
    """
    acc = 0.0
    total = 0.0
    for score, weight in items:
        if score is None or weight is None:
            continue
        acc += score * weight
        total += weight
    if total <= 0:
        return None
    return acc / total
