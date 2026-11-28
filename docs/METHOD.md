# Method and assumptions

What Marshal computes, and what is checked versus assumed. Code: `marshal_ab/measure.py`, `stats.py`, `decide.py`.

## 1. Measure

* **Unit and attribution.** One identity is one unit. It counts for the variant it was *first* exposed to (intention to
  treat). Flagsmith's own rule drops any identity seen on more than one variant; reallocating traffic creates exactly
  those identities, and dropping them biases the result toward whichever arm was never moved. Bucketing is monotone
  (checked on 2.295.2 by `sandbox/day0_check.py`: raising a share only adds identities), so a moved identity is
  always one that went control to treatment, and counting it as control makes the estimated effect smaller, not larger.
* **Outcome.** The metric over the identity's events after its first exposure (occurrence, count, sum or mean), reduced to
  `(n, sum, sum of squares)` per variant, the same sufficient statistics Flagsmith uses. With Flagsmith's own attribution
  rule these reproduce all 10 numbers on its scorecard (`marshal status`).

## 2. Decide

* **Interval.** A confidence sequence from a mixture sequential probability ratio test on the log of the rate ratio,
  with a normal mixture of spread tau = 0.2 (effects of +-20% are plausible, +-200% are not). With `v` the variance of the
  log ratio, the radius is `sqrt(v (v + tau^2) / tau^2 * (2 ln(1/alpha) + ln((v + tau^2) / v)))`, and the interval for the
  lift is `exp(log ratio -+ radius) - 1`. Ville's inequality bounds the chance that it *ever* excludes the truth by
  alpha, however often it is read.
* **Why the log ratio.** On the lift itself the delta-method error is proportional to the estimated ratio, which made the
  interval lopsided (4.8% false losers and no false winners in 400 A/A runs, 40 looks). On the log scale it is symmetric.
* **Segments.** Each trait value is judged on its own data with alpha divided by the number of segments (Bonferroni), using
  the trait the identity had at its first exposure.
* **Guardrails.** Any metric after the first. A variant is credibly worse when an always-valid test on the plain difference
  of means (prior spread 0.25 control standard deviations) excludes zero in the bad direction. The relative lift is not
  used: for a rare event with 18 errors in control it cannot see a 6x jump.
* **Sample ratio.** Chi-squared test of the identities first seen since the last reallocation against the split being
  served. Below p = 0.001, `run` holds traffic.

## 3. Act

Thompson sampling: each variant's chance of being best is estimated from its posterior (Beta with a uniform prior for
occurrence metrics, normal otherwise, 20,000 draws). The next split is
`holdback / k + (1 - holdback) * (floor + (1 - k * floor) * P(best))`, defaults floor 10%, holdback 20%, and no share moves
more than 10 points per cycle. Shares are written as whole percentages. The first metric steers; it is the only
one the split follows.

## What is checked, and what is assumed

| Claim | Status |
|---|---|
| Numbers match Flagsmith's scorecard | **Checked** to the decimal on a live experiment, `marshal status`. |
| The interval keeps its error rate under repeated looks | **Proved** for Gaussian data with known variance (Ville). With the variance estimated from the data, and with traffic that moves between looks, it is **checked by simulation, not proved**: A/A, 40 looks, 400 runs, 0.2% wrong calls on a fixed split (`tests/test_decide.py`); 1.2% under Marshal's own reallocation, 500 runs, 8 looks (`marshal bench`). |
| Reallocation earns more conversions | **Simulated** (`marshal bench`) and **estimated** on the demo run against a fixed even split at the rates measured so far. Not measured against a real control group. |
| Reallocation costs power | **Stated**: at a true +20% in 20,000 identities Marshal calls a winner at some look in 32% of runs, a fixed split with the same rule in 45%. |

Not handled: several guardrails are each tested at alpha, with no correction across them; a trend or novelty effect that
changes the lift over time is averaged into one number; very few conversions per arm make the plug-in variance noisy
(Flagsmith shows no result below 50 identities and 5 conversions per variant; Marshal's interval has no such floor, but
`run` does not move traffic below 50 identities per arm); monotone bucketing was verified on Flagsmith 2.295.2 only.
