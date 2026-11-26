# Traffic simulator

Drives a running Flagsmith experiment with simulated identities whose **true** conversion rates are
known, so you can check what a tool reports against what is actually there. Being able to say "the
real lift was +20% and it found +19.4%" is worth more than any chart.

Built before the hackathon as day-0 prep. It moves into Marshal's repo on Nov 26.

## Prerequisites

- `uv` (already installed). Dependencies are declared inline in `sim.py`, so `uv run` fetches them.
- A self-hosted Flagsmith with `EXPERIMENTATION_ENABLED=true`, a connected warehouse, and a running
  experiment on a multivariate flag whose variations each have a **key**. A variation without a key
  records no exposures.

## Setup

Edit `sim.toml`:

- `environment_key` — client-side or server-side key for the environment
- `feature` — the experiment's flag name
- `variants` — the variation keys as created in Flagsmith; `control` is reserved for the flag's own value
- `conversion_event` — must match the metric's event name **exactly, including case**. Unmatched
  events are silently ignored.
- `api_url` — ends in `/api/v1/`
- `events_url` — your Flagsmith API URL ending in `/api/` for the Flagsmith-database warehouse, or
  your event ingestion service's base URL otherwise. The SDK and this script both append `v1/events`.

Everything under `[truth]` is ground truth the tool under test is never told.

## Transports

| | What it does | Use it for |
|---|---|---|
| `--transport sdk` | Real Flagsmith Python SDK. **Flagsmith** picks the variant, so real bucketing and real event delivery are exercised. One round trip per identity. | Correctness |
| `--transport raw` | Posts event batches straight to the events API, with timestamps spread over `--span-hours`. The script picks the variant itself, so bucketing is **not** exercised. | Volume, and charts that need a time axis |

Raw is how four days of traffic arrives in four minutes on camera. SDK is how you prove the numbers
mean something.

## Modes

| Mode | What it sends | What a correct tool should do |
|---|---|---|
| `ab` | Variants differ per `[truth]` | Find the winner, eventually |
| `aa` | Every variant gets control's rate | Declare no winner. Flagsmith's own A/A post is explicit that one run proves nothing — it is the pattern across many that matters |
| `srm` | Drops `--srm-drop` of non-control exposures, as a one-variant crash would | Raise sample ratio mismatch and refuse to call a winner |
| `flat` | Conversions but no exposures, as broken instrumentation does | Show enrolment flat while the app looks healthy |

`srm` needs `--transport raw`: over the SDK the exposure is recorded by the act of reading the flag,
so it cannot be withheld.

## Examples

```bash
uv run sim.py --dry-run --users 400                      # generate and report, send nothing
```

```bash
uv run sim.py --mode ab --users 500 --transport sdk      # real bucketing, real delivery
```

```bash
uv run sim.py --mode raw --help                          # see every flag
```

```bash
uv run sim.py --mode ab --users 20000 --transport raw --span-hours 72 --prefix run2_
```

```bash
uv run sim.py --mode aa --users 20000 --transport raw --span-hours 72 --prefix aa1_
```

```bash
uv run sim.py --mode srm --users 5000 --transport raw --srm-drop 0.35 --prefix srm1_
```

## Things that will bite you

- **Use a fresh `--prefix` for every run.** Exposures are deduplicated per identity, and an identity
  recorded against more than one variant is *quarantined* — dropped from every count. Re-running with
  the same prefix after a weight change is the fastest way to poison your own dataset.
- **Timestamps** older than 10 days or more than 24 hours ahead are rejected, hence the 240-hour cap on
  `--span-hours`. A batch over 2 MiB is rejected whole, so the script sends 500 events per request.
- **Conversions only count after an identity's first exposure**, so the script always places a
  conversion after its exposure.
- **Identities must be identified** for exposures to count, and the same identifier must carry both the
  exposure and the conversion. The script uses one identifier per simulated user throughout.
- The event name is **case-sensitive** and must match the metric exactly, or conversions vanish without
  an error.

## Deliberately not here

No batch A/A statistics. Running hundreds of A/A experiments to check a false-declaration rate is a
test of the *analysis engine*, not of traffic, so it belongs in Marshal's own test suite where it needs
no network at all.
