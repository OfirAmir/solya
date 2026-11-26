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
    assert weighted_ray([(95, 90.72), (None, 26.55)]) == 95
    assert weighted_ray([(95, None)]) is None
    assert weighted_ray([]) is None


def test_load_ray_path():
    ray = load_ray(f"{__name__}:FixedRay")
    assert ray.compute(None, None, None) == 42.0


class SettingsRay:
    def __init__(self, settings):
        self.settings = settings

    def compute(self, site, local_date, session, actual_wh=None):
        return actual_wh


def test_load_ray_passes_settings_when_accepted():
    marker = object()
    ray = load_ray(f"{__name__}:SettingsRay", marker)
    assert ray.settings is marker
    assert ray.compute(None, None, None, actual_wh=7.0) == 7.0
    assert isinstance(load_ray(f"{__name__}:FixedRay", marker), FixedRay)


def test_null_ray_ignores_actual_wh():
    assert NullRay().compute(None, None, None, actual_wh=5.0) is None


@pytest.mark.parametrize("score,shown", [(89.5, 90), (90.5, 91), (79.5, 80), (90.15, 90), (89.49, 89), (0.5, 1)])
def test_display_ray_rounds_half_up(score, shown):
    from solya.rules.ray import display_ray

    assert display_ray(score) == shown


@pytest.mark.parametrize("score,band", [(89.5, "green"), (79.5, "orange"), (59.5, "yellow")])
def test_band_follows_displayed_integer(score, band):
    from solya.rules.ray import display_ray

    assert ray_band(display_ray(score)) == band
