"""Pure RAY v1 arithmetic. All functions take and return floats."""

HIST_WEIGHTS = {"last30": 2 / 3, "year1": 1 / 3, "year2": 1 / 6}
MIN_BASELINE_DAYS = 10
MIN_LAST30_DAYS = 10
MIN_SAME_DAYS = 5
MIN_CV_DAYS = 10
WINDOW_HOURS = range(7, 17)

CLOUD_PENALTY = 0.3
HEALTH_CV_FULL = 0.6
HEALTH_MAX_PENALTY = 20.0
ENV_CLOUD_WEIGHT = 40.0
ENV_TEMP_BASE_C = 25.0
ENV_TEMP_SLOPE = 0.4
ENV_TEMP_MAX = 20.0
ENV_PM10_FULL = 150.0
ENV_PM10_MAX = 15.0
PERF_RATIO_WEIGHT = 0.7
PERF_HIST_WEIGHT = 0.3
COMBINE_WEIGHTS = (0.40, 0.35, 0.25)
COMBINE_CAP_MARGIN = 25.0


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def perf_ratio(actual_wh: float, expected_wh: float, cloud_pct: float) -> float:
    raw = (actual_wh / expected_wh * 100) / (1 - CLOUD_PENALTY * cloud_pct / 100)
    return max(0.0, min(raw, 100.0))


def hist_sub(actual_wh: float, refs: list[tuple[float | None, float]]) -> float | None:
    total = 0.0
    weights = 0.0
    for ref_wh, weight in refs:
        if ref_wh is None:
            continue
        total += weight * min(actual_wh / ref_wh * 100, 100.0)
        weights += weight
    if weights == 0:
        return None
    return total / weights


def health(cv: float | None) -> float:
    if cv is None:
        return 100.0
    return 100.0 - min(HEALTH_MAX_PENALTY, cv / HEALTH_CV_FULL * HEALTH_MAX_PENALTY)


def env(cloud_pct: float, tmax_c: float, pm10: float) -> float:
    return clamp(
        100
        - ENV_CLOUD_WEIGHT * cloud_pct / 100
        - min(ENV_TEMP_MAX, ENV_TEMP_SLOPE * max(0.0, tmax_c - ENV_TEMP_BASE_C))
        - ENV_PM10_MAX * min(pm10 / ENV_PM10_FULL, 1.0)
    )


def perf(perf_ratio: float, hist_sub: float | None) -> float:
    h = hist_sub if hist_sub is not None else perf_ratio
    return PERF_RATIO_WEIGHT * perf_ratio + PERF_HIST_WEIGHT * h


def combine(perf: float, health: float, env: float) -> float:
    wp, wh, we = COMBINE_WEIGHTS
    return clamp(min(wp * perf + wh * health + we * env, perf + COMBINE_CAP_MARGIN))
