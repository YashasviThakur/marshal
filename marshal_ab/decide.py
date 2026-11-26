"""Decide: when is a result real, and where should traffic go next.

Flagsmith's interval is a fixed-sample one: look at it every day and it will call roughly one A/A test in
four a winner. Marshal's verdict uses an always-valid confidence sequence instead (a mixture sequential
probability ratio test, normal prior on the lift), so checking continuously is legitimate. The allocation
is Thompson sampling: each variant gets the share of traffic equal to the probability that it is the best.
"""
import math
import random
from dataclasses import dataclass

from .stats import VariantStats, lift_se

DEFAULT_TAU = 0.2  # prior spread of the relative lift: effects of +-20% are plausible, +-200% are not


@dataclass(frozen=True)
class Verdict:
    lift: float
    low: float
    high: float

    @property
    def state(self) -> str:
        return "winner" if self.low > 0 else "loser" if self.high < 0 else "undecided"


def confidence_sequence(control: VariantStats, treatment: VariantStats, alpha: float = 0.05,
                        tau: float = DEFAULT_TAU) -> Verdict | None:
    """Interval for the relative lift that holds at every sample size at once, with error rate alpha."""
    r = lift_se(control, treatment)
    if r is None or r[1] == 0:
        return None
    lift, se = r
    v, t2 = se * se, tau * tau
    radius = math.sqrt(v * (v + t2) / t2 * (2 * math.log(1 / alpha) + math.log((v + t2) / v)))
    return Verdict(lift, lift - radius, lift + radius)


def win_probabilities(arms: dict[str, VariantStats], *, occurrence: bool, lower_is_better: bool = False,
                      draws: int = 20000, rng: random.Random | None = None) -> dict[str, float]:
    """P(variant is the best), by sampling each variant's posterior mean (Beta for conversions, normal else)."""
    rng = rng or random.Random()
    sign = -1 if lower_is_better else 1

    def draw(a: VariantStats) -> float:
        if occurrence:
            return rng.betavariate(1 + a.sum, 1 + a.n - a.sum)
        return rng.gauss(a.mean, math.sqrt(a.variance / a.n))

    wins = dict.fromkeys(arms, 0)
    for _ in range(draws):
        wins[max(arms, key=lambda v: sign * draw(arms[v]))] += 1
    return {v: c / draws for v, c in wins.items()}


def allocate(wins: dict[str, float], current: dict[str, float], *, floor: float = 0.10, holdback: float = 0.20,
             max_step: float = 0.10) -> dict[str, float]:
    """Next traffic split. Every variant keeps at least `floor`; `holdback` of traffic stays evenly split so a
    clean comparison always exists; no variant moves more than `max_step` in one update."""
    k = len(wins)
    target = {v: holdback / k + (1 - holdback) * (floor + (1 - k * floor) * w) for v, w in wins.items()}
    delta = {v: target[v] - current[v] for v in wins}
    scale = min(1.0, max_step / max(abs(d) for d in delta.values())) if any(delta.values()) else 1.0
    return {v: current[v] + scale * delta[v] for v in wins}


def percentages(split: dict[str, float], control: str = "control") -> dict[str, int]:
    """Whole-number treatment percentages; control takes whatever is left, as in Flagsmith."""
    return {v: round(s * 100) for v, s in split.items() if v != control}


def guardrail_breaches(guardrails, stats: dict, control: str = "control") -> list[str]:
    """Guardrail metrics (anything after the steering metric) on which a variant is credibly worse than control."""
    return [f"{g.event} on {v}" for g in guardrails for v in stats[g.id] if v != control
            and (cs := confidence_sequence(stats[g.id][control], stats[g.id][v]))
            and cs.state == ("winner" if g.lower_is_better else "loser")]
