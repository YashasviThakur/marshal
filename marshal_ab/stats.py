"""Flagsmith's experiment statistics, reproduced so Marshal's numbers can be checked against Flagsmith's own.

Ported from Flagsmith 2.295.2 `api/experimentation/stats.py` and the floors in `services.py`
(Copyright Bullet Train Ltd, BSD-3-Clause). Same inputs, same formulas: per-variant sufficient statistics
(n, sum, sum of squares), lift by the delta method, a normal credible interval, chance to win, and a
chi-squared sample-ratio check.
"""
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from statistics import NormalDist

_STANDARD_NORMAL = NormalDist()
_Z_95 = 1.959963984540054
# services.py floors: below these a metric shows "collecting data" instead of a result.
MIN_IDENTITIES_PER_VARIANT = 50
MIN_CONVERSIONS_PER_VARIANT = 5
SRM_MIN_TOTAL_IDENTITIES = 100
CONTROL = "control"


@dataclass(frozen=True)
class VariantStats:
    n: int
    sum: float
    sum_squares: float

    @property
    def mean(self) -> float:
        return self.sum / self.n

    @property
    def variance(self) -> float:
        return max(0.0, (self.sum_squares - self.sum**2 / self.n) / (self.n - 1))


@dataclass(frozen=True)
class Inference:
    lift: float
    ci_low: float
    ci_high: float
    chance_to_win: float


def compare_to_control(control: VariantStats, treatment: VariantStats) -> Inference | None:
    if control.n < 2 or treatment.n < 2 or control.mean <= 0:
        return None
    lift = (treatment.mean - control.mean) / control.mean
    variance = treatment.variance / (treatment.n * control.mean**2) + treatment.mean**2 * control.variance / (
        control.n * control.mean**4)
    se = math.sqrt(variance)
    if se == 0:
        return Inference(lift, lift, lift, 0.5 if lift == 0 else float(lift > 0))
    return Inference(lift, lift - _Z_95 * se, lift + _Z_95 * se, _STANDARD_NORMAL.cdf(lift / se))


def infer(control: VariantStats | None, treatment: VariantStats, *, occurrence: bool,
          lower_is_better: bool) -> Inference | None:
    """compare_to_control behind Flagsmith's display floors, flipped for lower-is-better metrics."""
    if control is None or control.n < MIN_IDENTITIES_PER_VARIANT or treatment.n < MIN_IDENTITIES_PER_VARIANT:
        return None
    if occurrence and (control.sum < MIN_CONVERSIONS_PER_VARIANT or treatment.sum < MIN_CONVERSIONS_PER_VARIANT):
        return None
    result = compare_to_control(control, treatment)
    if result is not None and lower_is_better:
        result = replace(result, chance_to_win=1.0 - result.chance_to_win)
    return result


def srm_p_value(observed: Sequence[int], expected_shares: Sequence[float]) -> float | None:
    total = sum(observed)
    if len(observed) < 2 or total == 0 or any(s <= 0 for s in expected_shares):
        return None
    statistic = sum((c - total * s) ** 2 / (total * s) for c, s in zip(observed, expected_shares, strict=True))
    return _chi_squared_survival(statistic, len(observed) - 1)


def _chi_squared_survival(statistic: float, degrees_of_freedom: int) -> float:
    if statistic <= 0:
        return 1.0
    y = statistic / 2.0
    if degrees_of_freedom % 2:
        a, survival = 0.5, math.erfc(math.sqrt(y))
    else:
        a, survival = 1.0, math.exp(-y)
    while a + 1.0 <= degrees_of_freedom / 2.0:
        survival += math.exp(a * math.log(y) - y - math.lgamma(a + 1.0))
        a += 1.0
    return survival
