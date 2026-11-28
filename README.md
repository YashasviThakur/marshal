# Marshal

Adaptive traffic allocation for **self-hosted Flagsmith Experimentation**. Marshal watches a running experiment,
decides with statistics that stay valid however often you look, and moves traffic toward the better variant through
Flagsmith's own `PATCH .../experiments/{id}/rollout/` endpoint. Nothing in Flagsmith is patched or forked.

Built for Toggle Wars, Track 03.

## For judges (Track 03: Build on Open Source Flagsmith)

**One sentence:** Marshal makes a self-hosted Flagsmith experiment steer itself: it measures the experiment from the warehouse, decides with statistics that stay valid however often you look, and moves traffic toward the better variant through Flagsmith's own rollout API.

**Two minutes, nothing to install** (Python 3.12):

```
python -m marshal_ab serve --replay docs/replay.json    # the dashboard on a recorded real run, http://localhost:8765
python -m marshal_ab bench                               # 500 simulated experiments per row: Marshal vs a fixed 50/50 split
```

**The track's four bullets**

| Bullet | Where it shows |
|---|---|
| Self-hosted Flagsmith | Built and run on self-hosted 2.295.2 with `EXPERIMENTATION_ENABLED`, using the "Flagsmith database" warehouse, so no ClickHouse or Kafka. `sandbox/compose.yml` brings it up. |
| Integrations and plugins | Reads the Admin API and the events table; writes only `PATCH .../experiments/{id}/rollout/`. Nothing in Flagsmith is forked. Read-only dashboard, plus a container (`compose.marshal.yml`) that sits beside your own Flagsmith. |
| Developer tools | `status`, `run`, `segments`, `serve`, `snapshot`, `bench` in one stdlib CLI (psycopg only for the warehouse). `observe` is the default mode, so nothing moves by accident. CI runs the tests against a real Postgres. |
| Easy for others to reuse | `Dockerfile`, `requirements.txt`, environment-variable settings, MIT licence, a replay file that needs no setup, and a benchmark that needs no Flagsmith. |

**The scorecard**

| Criterion | Evidence |
|---|---|
| Use of Flagsmith | Flagsmith is the whole substrate: its events, its experiment and metric objects, its results (matched to **all 10 numbers** of its scorecard, largest difference 0.0), and its rollout endpoint as the actuator. |
| Impact | A fixed split wastes users on the worse arm and makes you wait. Marshal earned about 87 more conversions in a 20,000-identity run, and the benchmark shows +3% to +9% conversions with no false winners (0 of 500 A/A runs). |
| Creativity | No other entry I found does adaptive allocation, always-valid inference or per-segment analysis on Flagsmith. Flagsmith's own blog credits competitors with exactly these. |
| Demo video | See DEMO.md for the 3-minute script. |

**Check it yourself**

| Claim | Command |
|---|---|
| Matches Flagsmith's scorecard to the decimal | `python -m marshal_ab status` (after `sh sandbox/demo.sh`) |
| Always-valid check: 3% vs 33% false winners | `python -m unittest tests.test_decide -v` |
| More conversions, no invented winners | `python -m marshal_ab bench` |
| It really moves a running experiment | `python sandbox/day0_check.py` |

**Upstream:** while building this I found that Flagsmith's docs say a running experiment's variations are locked, yet its rollout endpoint changes the split while running. I filed [Flagsmith/flagsmith#9164](https://github.com/Flagsmith/flagsmith/issues/9164), and the sample-ratio gap I hit is already tracked as #9128.

**Limits, stated first:** Marshal gives up some statistical power to serve users better (at +20% in 20,000 identities it calls a winner in 12% of runs, a fixed split in 28%); one metric steers and the others are guardrails; shares are whole percentages. See "Honest limits" below.

## Why this gap

Flagsmith's comparison post credits competitors with "automated rollout features for gradually shifting traffic toward
a winning variant" and "sequential testing". Flagsmith has neither, its glossary lists the frequentist engine as
deferred, and its Release Pipelines have no metric-based triggers. Marshal supplies both, on top of the public API.

## What it does

| | |
|---|---|
| **measure** | Its own SQL on the warehouse events table (`--sql` prints it), so it is not bound by the 5-minute results refresh. Attribution is by each identity's *first* exposure, because reallocation puts identities on two variants and Flagsmith quarantines those. |
| **decide** | An always-valid confidence sequence on the relative lift (mixture sequential probability ratio test). Look every minute and the error rate is still 5%. |
| **act** | Thompson sampling: each variant gets the probability that it is the best. Guard rails: the experiment's other metrics are guardrails, and if one is credibly worse for a variant (confidence sequence) Marshal stops moving traffic and says why (a test on the difference of means that stays valid at every look; in an A/A run it wrongly halted 1.0% of 400 runs); every variant keeps at least 10%, 20% of traffic stays evenly split as a holdback, and no share moves more than 10 points per cycle. |
| **segments** | `python -m marshal_ab segments device` takes the result apart by any trait the SDK sent. Each segment is judged with the confidence sequence at alpha / k, so slicing does not manufacture winners. |
| **dashboard** | `python -m marshal_ab serve` opens a read-only page on :8765: traffic share over time, the evidence for each variant, any trait taken apart, guardrails, and the decision log. Refreshes every 10 seconds, standard library only. |
| **prove** | A/A runs with identical variants. Marshal and Flagsmith's own scorecard are checked against each other to the decimal. |

`observe` is the default mode: it prints and logs what it would do (`.marshal/decisions.jsonl`) and moves nothing.
`--mode act` writes the new split.

## Numbers

* `marshal status` reproduces **all 10 numbers** on Flagsmith's scorecard (counts, sums, sums of squares, lift,
  interval, chance to win) with a largest difference of **0.0**, using Flagsmith's own attribution rule.
* A/A, 40 looks per run, 400 runs (`tests/test_decide.py`): the confidence sequence calls **3.0%** of runs a winner;
  a 95% fixed-sample interval checked at every look calls **33%**.
* Demo run (`sh sandbox/demo.sh 8 2500`: eight rounds, 20,000 identities, Marshal reallocating after each): the
  treatment's share went 50 -> 60 -> 70 -> 80 -> 82% and held, because the floor and the holdback cap it (control keeps
  0.2/2 + 0.8 x 10% = 18%). Chance the treatment is best was 1.0 from round 2. The measured lift was **+51%**, and the
  confidence sequence called it a winner: [+15%, +88%] (a fixed 95% interval gives [+28%, +74%]). About 94 more
  conversions than a fixed 50/50 split would have earned at the rates measured (an estimate). The size of the lift
  varies between runs (+30% at 12,000 identities, +51% at 20,000); read the interval, not the point estimate.
* Re-run on 2026-11-28 (same script, fresh experiment, recorded in [docs/replay.json](docs/replay.json)): split 50 -> 60 -> 70 -> 80 -> 82 and held; lift **+47.9%**, always-valid [+11.9%, +83.9%] = winner (fixed 95% interval [+25.4%, +70.4%]); about **87 more conversions** than a fixed 50/50 split (an estimate); by device desktop +99% winner, android -35% called a loser ([-70%, -0.4%]), ios undecided. The sample-ratio check against the split being served read p = 0.30, ok.
* The price of a bandit shows at smaller samples: at 12,000 identities the same setup was still undecided,
  [-7%, +66%]. Sending 82% of users to the better arm is what makes the interval slow to close. Use `observe` and a
  fixed split when you need a verdict; use `act` when serving users well matters more.
* Segments, same 20,000 identities (`marshal segments device`, Bonferroni-corrected): desktop +111%, called a winner
  ([+12%, +210%]); android -34%, [-69%, +0.2%], undecided; ios +42%, undecided. The simulator plants a treatment that
  loses on android and wins elsewhere; Marshal sees the sign flip that the overall number hides.
* Guardrail demo (`sh sandbox/demo.sh 6 1500 guardrail`): the simulator's treatment wins on purchases but raises `checkout_error` from 1% to 4%. Marshal holds the split at 50/50 in every round, logging `hold: guardrail breached by checkout_error on treatment`, although the chance the treatment is best is 0.99 or higher.
* Day-0 check (`sandbox/day0_check.py`): on self-hosted 2.295.2 the variant weights of a *running* experiment can be
  changed with `PATCH rollout` (HTTP 200), and bucketing is monotone: no identity moves from treatment back to control.

## Try it with nothing installed

```
python -m marshal_ab bench                                  # simulated experiments, Marshal vs a fixed 50/50 split (stdlib only)
python -m marshal_ab serve --replay docs/replay.json        # the dashboard on a recorded demo run, no Flagsmith, no install
```

The benchmark (500 simulated experiments per row, `python -m marshal_ab bench`):

500 simulated experiments per row: base rate 4.2%, 8 rounds of 2500 users, always-valid check after every round

| scenario | policy | conversions / experiment | called winner at the end | called winner at any look | first call (round) |
|---|---|---|---|---|---|
| A/A (no real effect) | fixed | 838.0 | 0.2% | 0.2% | 8.0 |
| A/A (no real effect) | marshal | 839.2 | 0.0% | 0.0% | - |
| | marshal minus fixed | +1.2 | | | |
| treatment +20% | fixed | 925.3 | 27.8% | 30.6% | 6.3 |
| treatment +20% | marshal | 956.4 | 12.4% | 13.8% | 6.7 |
| | marshal minus fixed | +31.1 | | | |
| treatment +50% | fixed | 1051.7 | 100.0% | 100.0% | 3.7 |
| treatment +50% | marshal | 1149.7 | 99.8% | 99.8% | 4.1 |
| | marshal minus fixed | +98.0 | | | |

How to read it, without the flattering parts: Marshal earns more conversions because it sends users to the better arm, **+3% at a true +20% lift and +9% at +50%**. It never invented a winner (A/A: 0.0% of 500 runs, against about 33% for a 95% interval checked at every look). The price is power on small effects: at +20% in 20,000 identities Marshal calls a winner in 12% of runs and a fixed split in 28%, because starving the weaker arm slows the evidence. At +50% they are equal. Use `observe` with a fixed split when you need the verdict; use `act` when serving users well matters more.

## Run it

```powershell
docker compose -f sandbox/compose.yml up -d     # Flagsmith 2.295.2 with EXPERIMENTATION_ENABLED, :8100
python sandbox/bootstrap.py                      # admin, organisation, API key -> sandbox/.env
python sandbox/seed_experiment.py                # project, flag, metric, running 50/50 experiment
uv run simulator/sim.py --users 3000             # simulated traffic with known true conversion rates
python -m marshal_ab status                      # Marshal vs Flagsmith's scorecard
python -m marshal_ab serve                       # dashboard on http://localhost:8765
python -m marshal_ab run                         # observe: what would I do?
python -m marshal_ab run --mode act --every 15   # act every 15 minutes (each write creates a feature version)
python -m unittest discover tests
```

As a container beside your own Flagsmith: `docker compose -f docker-compose.yml -f compose.marshal.yml up -d marshal` ([compose.marshal.yml](compose.marshal.yml), read-only dashboard on :8765; `act` mode runs from the CLI). `python -m marshal_ab snapshot` saves the dashboard state for `serve --replay`. CI runs the tests against a Postgres service and the benchmark.

No ClickHouse or Kafka is needed: Marshal uses Flagsmith's "Flagsmith database" warehouse, which stores events in the
same Postgres.

## Honest limits

* **Flagsmith's sample-ratio check will fire during a bandit run.** Its counts are cumulative, so they no longer match the split being served. Marshal's `status` checks only identities first seen since the last reallocation against the split now served: after a 50/50 -> 19/81 move, 800 new users gave p = 0.43 (ok), where the cumulative count gives p < 0.001. Flagsmith's own page will still show its alarm.
* Flagsmith's own sample-ratio check is empty for experiments (it reads the environment default split, which has
  0% treatment): a bug in its `services.py`, worth an upstream issue.
* One primary metric steers traffic (the experiment's first); the rest only act as guardrails that halt it.
* Variant shares are written as whole percentages.
* Thompson sampling is a heuristic for exploiting a likely winner; for a clean estimate of the effect size keep the
  holdback and read the confidence sequence, not the allocation.

## Layout

```
marshal_ab/stats.py     Flagsmith's statistics, ported so numbers can be checked against the scorecard
marshal_ab/measure.py   the warehouse SQL (quarantine = Flagsmith's rule, first = what Marshal decides on)
marshal_ab/decide.py    confidence sequence, Thompson sampling, allocation with floor / holdback / step limit
marshal_ab/cli.py       status, run, segments, serve
marshal_ab/dashboard.py the read-only dashboard (dashboard.html is its page)
sandbox/                self-hosted Flagsmith, bootstrap, seeded experiment, day-0 check, convergence demo
simulator/              traffic with known ground truth (ab, aa, srm, flat modes)
```

`stats.py` is derived from Flagsmith's `api/experimentation/stats.py` (BSD-3-Clause, Bullet Train Ltd).
