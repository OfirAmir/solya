import pytest

from solya.rules.ray import NullRay, load_ray, ray_band, weighted_ray


class FixedRay:
    def compute(self, site, local_date, session):
        return 42.0


def test_null_ray_default():
    assert NullRay().compute(None, None, None) is None
    assert isinstance(load_ray(None), NullRay)


@pytest.mark.parametrize(
    "score,band",
    [(95, "green"), (85, "orange"), (70, "yellow"), (59.9, "red"), (90, "green"), (80, "orange"), (60, "yellow")],
)
def test_ray_bands(score, band):
    assert ray_band(score) == band


def test_weighted_ray():
    assert round(weighted_ray([(95, 90.72), (97, 26.55)]), 2) == 95.45
    assert weighted_ray([(95, 90.72), (None, 26.55)]) is None
    assert weighted_ray([(95, None)]) is None
    assert weighted_ray([]) is None


def test_load_ray_path():
    ray = load_ray(f"{__name__}:FixedRay")
    assert ray.compute(None, None, None) == 42.0
