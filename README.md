# Marshal

Adaptive traffic allocation for **self-hosted Flagsmith Experimentation**. Marshal watches a running experiment,
decides with statistics that stay valid however often you look, and moves traffic toward the better variant through
Flagsmith's own `PATCH .../experiments/{id}/rollout/` endpoint. Nothing in Flagsmith is patched or forked.

Built for Toggle Wars, Track 03.

## Why this gap

Flagsmith's comparison post credits competitors with "automated rollout features for gradually shifting traffic toward
a winning variant" and "sequential testing". Flagsmith has neither, its glossary lists the frequentist engine as
deferred, and its Release Pipelines have no metric-based triggers. Marshal supplies both, on top of the public API.

## What it does

| | |
|---|---|
| **measure** | Its own SQL on the warehouse events table (`--sql` prints it), so it is not bound by the 5-minute results refresh. Attribution is by each identity's *first* exposure, because reallocation puts identities on two variants and Flagsmith quarantines those. |
| **decide** | An always-valid confidence sequence on the relative lift (mixture sequential probability ratio test). Look every minute and the error rate is still 5%. |
| **act** | Thompson sampling: each variant gets the probability that it is the best. Guard rails: every variant keeps at least 10%, 20% of traffic stays evenly split as a holdback, and no share moves more than 10 points per cycle. |
| **prove** | A/A runs with identical variants. Marshal and Flagsmith's own scorecard are checked against each other to the decimal. |

`observe` is the default mode: it prints and logs what it would do (`.marshal/decisions.jsonl`) and moves nothing.
`--mode act` writes the new split.

## Numbers

* `marshal status` reproduces **all 10 numbers** on Flagsmith's scorecard (counts, sums, sums of squares, lift,
  interval, chance to win) with a largest difference of **0.0**, using Flagsmith's own attribution rule.
* A/A, 40 looks per run, 400 runs (`tests/test_decide.py`): the confidence sequence calls **3.0%** of runs a winner;
  a 95% fixed-sample interval checked at every look calls **33%**.
* Day-0 check (`sandbox/day0_check.py`): on self-hosted 2.295.2 the variant weights of a *running* experiment can be
  changed with `PATCH rollout` (HTTP 200), and bucketing is monotone: no identity moves from treatment back to control.

## Run it

```powershell
docker compose -f sandbox/compose.yml up -d     # Flagsmith 2.295.2 with EXPERIMENTATION_ENABLED, :8100
python sandbox/bootstrap.py                      # admin, organisation, API key -> sandbox/.env
python sandbox/seed_experiment.py                # project, flag, metric, running 50/50 experiment
uv run simulator/sim.py --users 3000             # simulated traffic with known true conversion rates
python -m marshal_ab status                      # Marshal vs Flagsmith's scorecard
python -m marshal_ab run                         # observe: what would I do?
python -m marshal_ab run --mode act --every 15   # act every 15 minutes (each write creates a feature version)
python -m unittest discover tests
```

No ClickHouse or Kafka is needed: Marshal uses Flagsmith's "Flagsmith database" warehouse, which stores events in the
same Postgres.

## Honest limits

* **Flagsmith's sample-ratio check will fire during a bandit run.** Cumulative counts no longer match the split being
  served. That is expected, not a bug. `status` compares against the split currently served.
* Flagsmith's own sample-ratio check is empty for experiments (it reads the environment default split, which has
  0% treatment): a bug in its `services.py`, worth an upstream issue.
* One primary metric steers traffic (the experiment's first). A bandit optimises for it; check guardrail metrics in
  Flagsmith's dashboard.
* Variant shares are written as whole percentages.
* Thompson sampling is a heuristic for exploiting a likely winner; for a clean estimate of the effect size keep the
  holdback and read the confidence sequence, not the allocation.

## Layout

```
marshal_ab/stats.py     Flagsmith's statistics, ported so numbers can be checked against the scorecard
marshal_ab/measure.py   the warehouse SQL (quarantine = Flagsmith's rule, first = what Marshal decides on)
marshal_ab/decide.py    confidence sequence, Thompson sampling, allocation with floor / holdback / step limit
marshal_ab/cli.py       status, run
sandbox/                self-hosted Flagsmith, bootstrap, seeded experiment, day-0 check, convergence demo
simulator/              traffic with known ground truth (ab, aa, srm, flat modes)
```

`stats.py` is derived from Flagsmith's `api/experimentation/stats.py` (BSD-3-Clause, Bullet Train Ltd).
