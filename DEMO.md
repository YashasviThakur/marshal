# Demo script (about 3 minutes)

Setup, once: `docker compose -f sandbox/compose.yml up -d`, `python sandbox/bootstrap.py`, then in two terminals:
`python -m marshal_ab serve` (dashboard on :8765) and, when you start recording, `sh sandbox/demo.sh 8 2500` (about 10 minutes, 20,000 users; start it before you begin filming. It ends in a called winner, about +46% overall; `6 1000` is quicker but ends undecided)
(fresh 50/50 experiment, eight rounds of simulated traffic, Marshal acting after each).

No time to record? `python -m marshal_ab serve --replay docs/replay.json` shows the same run (and `docs/index.html` is the same page for a static host).

Record the dashboard on the left and Flagsmith's own experiment page (`http://localhost:8100`, the experiment's results
tab) on the right.

| Time | On screen | Say |
|---|---|---|
| 0:00 | Flagsmith experiment page, 50/50, running | "A/B tests send half your users to the loser until someone remembers to look. Flagsmith Experimentation can run the test. Nothing in it acts on the evidence, or tells you when the evidence is real." |
| 0:20 | Terminal: `marshal status` | "Marshal reads the raw events from the warehouse with its own SQL, and matches Flagsmith's scorecard on all ten numbers to the decimal. So when it disagrees later, it is the maths, not a bug." |
| 0:40 | Dashboard, round 1 | "Every cycle it measures, decides, then moves traffic with Flagsmith's own rollout endpoint. Nothing is forked." |
| 1:00 | Dashboard, rounds 2-5: bar moves 50 -> 80% | "Thompson sampling sends users to whichever variant is probably better. A floor keeps every variant alive and a holdback of 20% stays evenly split so a clean comparison always exists." |
| 1:30 | Evidence table: fixed 95% vs always-valid | "Both intervals now clear zero: +24% to +68% fixed, +16% to +84% always-valid, so Marshal calls a winner. The difference is what the fixed one costs. If you look every day, it is wrong about one A/A test in three: I measured 33%, and 31% in the benchmark. The always-valid interval is wrong under 2% of the time however often you look." Show `python -m marshal_ab bench --runs 100` or the unit test line. |
| 2:00 | Segment panel, callout | "Treatment wins overall, and desktop is called a winner at +87%. Android is down 38%, not yet called. One number hid that. Marshal takes the result apart by any trait, correcting for the number of slices." |
| 2:25 | Guardrails + decision log (optional: run `sh sandbox/demo.sh 6 1500 guardrail` instead and show the log line `hold: guardrail breached by checkout_error on treatment`) | "The treatment wins on purchases but quadruples checkout errors. Marshal holds the split at 50/50 and says why, even at 99% chance of being best, and the feature turns Unhealthy in Flagsmith's own page with that reason (`--health`, on in this run). Add `--notify <webhook>` and a Slack or Discord message says it once. Wrongly halting an A/A test happens 1% of the time however often you look." |
| 2:45 | Flagsmith page shows its own sample-ratio alarm; terminal `marshal status` shows ok | "Flagsmith's alarm fires because it counts cumulatively. Marshal checks only users first seen since the last move: p = 0.36. And if that check ever fails, Marshal stops moving traffic." |
| 3:00 | README | "Self-hosted, no fork, standard library dashboard. github.com/YashasviThakur/marshal." |

Honest lines to keep in (judges notice): the bandit trades inference for regret, so the verdict stays "undecided" for
longer than a fixed split would; `observe` mode is the default and moves nothing; extra conversions are an estimate at
the rates measured so far.

On the benchmark: Flagsmith's 95% interval, read after every round, calls about 20% of no-effect experiments wrong (31% over 40 looks); Marshal calls 1.2% and 0%. The price is power at small lifts (27% of runs called at +20% in 20,000 identities against 76% for Flagsmith's interval): say so.
