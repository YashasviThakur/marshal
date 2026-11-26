# /// script
# requires-python = ">=3.11"
# dependencies = ["flagsmith>=6.3.0", "httpx>=0.28"]
# ///
"""Traffic simulator for Flagsmith Experimentation.

Drives a running experiment with simulated identities whose true conversion rates
are known, so you can check what a tool reports against what is actually there.

Two transports:

  sdk   Uses the real Flagsmith Python SDK. Flagsmith decides the variant, so this
        exercises real bucketing and real event delivery. Use it to verify
        correctness. Slower: one round trip per identity.

  raw   Posts event batches straight to the events API with timestamps spread over
        a window, so hours or days of traffic arrive in seconds. The simulator
        assigns the variant itself, so this does NOT exercise Flagsmith's
        bucketing. Use it for volume and for charts that need a time axis.

Modes:

  ab    Variants differ, per [truth] in the config.
  aa    Every variant gets the control's rate. A correct tool finds no winner.
  srm   Drops a share of one variant's exposures, as a one-variant crash would.
        Sample ratio mismatch should fire; results should not be trusted.
  flat  Sends conversions but no exposures, as broken instrumentation does.
        Enrolment stays flat while the app looks healthy.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

EXPOSURE_EVENT = "$flag_exposure"
CONTROL = "control"
BATCH_SIZE = 500


@dataclass
class Identity:
    identifier: str
    traits: dict[str, str]
    variant: str | None = None
    exposed: bool = False
    converted: bool = False


@dataclass
class Tally:
    exposures: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    conversions: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    by_device: dict[tuple[str, str], list[int]] = field(
        default_factory=lambda: defaultdict(lambda: [0, 0])
    )
    sent: int = 0
    accepted: int = 0
    rejected: list[str] = field(default_factory=list)


def load_config(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def pick(weights: dict[str, float], rng: random.Random) -> str:
    keys = list(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def true_rate(cfg: dict, mode: str, variant: str, device: str) -> float:
    truth = cfg["truth"]
    if mode == "aa":
        return float(truth[CONTROL])
    base = float(truth.get(variant, truth[CONTROL]))
    if variant == CONTROL:
        return base
    multiplier = float(truth.get("device_multiplier", {}).get(device, 1.0))
    return min(1.0, base * multiplier)


def make_identities(cfg: dict, count: int, prefix: str, rng: random.Random) -> list[Identity]:
    traits_cfg = cfg["traits"]
    return [
        Identity(
            identifier=f"{prefix}{i}",
            traits={name: pick(weights, rng) for name, weights in traits_cfg.items()},
        )
        for i in range(count)
    ]


def resolve_variant(flag) -> str:
    """The variant an SDK flag was served. Control has no multivariate option, so
    its variant comes back empty."""
    return getattr(flag, "variant", None) or CONTROL


# --------------------------------------------------------------------------- sdk


def run_sdk(cfg: dict, args: argparse.Namespace, ids: list[Identity], tally: Tally) -> None:
    from flagsmith import EventProcessorConfig, Flagsmith

    fsc = cfg["flagsmith"]
    client = Flagsmith(
        environment_key=fsc["environment_key"],
        api_url=fsc["api_url"],
        enable_events=True,
        # max_buffer_items is a cap, not a flush size: when full the SDK drops the oldest events.
        # Keep it above the run's event count; flush_events() at the end sends the rest.
        event_processor_config=EventProcessorConfig(
            events_api_url=fsc["events_url"], max_buffer_items=100_000
        ),
    )
    feature = fsc["feature"]
    event = cfg["flagsmith"]["conversion_event"]
    rng = random.Random(args.seed + 1)
    guard = cfg["truth"].get("guardrail") if args.guardrail else None  # a second event only the treatment inflates

    for identity in ids:
        if args.mode == "flat":
            # Evaluate without recording an exposure: the app works, the data does not.
            flags = client.get_identity_flags(identity.identifier, traits=identity.traits)
            flag = flags.get_flag(feature)
            identity.variant = resolve_variant(flag)
        else:
            flag = client.get_experiment_flag(
                feature, identifier=identity.identifier, traits=identity.traits
            )
            identity.variant = resolve_variant(flag)
            if args.mode == "srm" and identity.variant != CONTROL:
                if rng.random() < args.srm_drop:
                    # The exposure was already recorded by get_experiment_flag, so a
                    # dropped exposure can only be simulated over the raw transport.
                    pass
            identity.exposed = True

        rate = true_rate(cfg, args.mode, identity.variant, identity.traits.get("device", ""))
        if rng.random() < rate:
            identity.converted = True
            client.track_event(
                event,
                identifier=identity.identifier,
                value=1,
                traits=identity.traits,
            )
        if guard and rng.random() < float(guard.get(identity.variant, guard[CONTROL])):
            client.track_event(guard["event"], identifier=identity.identifier, value=1, traits=identity.traits)
        record(tally, identity)

    client.flush_events()
    client.close()
    tally.sent = sum(tally.exposures.values()) + sum(tally.conversions.values())
    tally.accepted = tally.sent


# --------------------------------------------------------------------------- raw


def assign_variants(args: argparse.Namespace, ids: list[Identity], variants: list[str]) -> None:
    rng = random.Random(args.seed + 2)
    treatments = [v for v in variants if v != CONTROL]
    for identity in ids:
        identity.variant = (
            rng.choice(treatments) if treatments and rng.random() * 100 < args.split else CONTROL
        )


def build_events(cfg: dict, args: argparse.Namespace, ids: list[Identity], tally: Tally) -> list[dict]:
    fsc = cfg["flagsmith"]
    feature, event = fsc["feature"], fsc["conversion_event"]
    rng = random.Random(args.seed + 3)
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=args.span_hours)
    events: list[dict] = []

    for identity in ids:
        exposed_at = start + (now - start) * rng.random()
        if args.mode != "flat":
            drop = (
                args.mode == "srm"
                and identity.variant != CONTROL
                and rng.random() < args.srm_drop
            )
            if not drop:
                identity.exposed = True
                events.append(
                    {
                        "event": EXPOSURE_EVENT,
                        "feature_name": feature,
                        "identifier": identity.identifier,
                        "value": identity.variant,
                        "timestamp": int(exposed_at.timestamp() * 1000),
                        "traits": identity.traits,
                    }
                )

        rate = true_rate(cfg, args.mode, identity.variant, identity.traits.get("device", ""))
        if rng.random() < rate:
            identity.converted = True
            # After the exposure, or it is not attributed to it.
            converted_at = exposed_at + timedelta(seconds=rng.randint(5, 3600))
            events.append(
                {
                    "event": event,
                    "identifier": identity.identifier,
                    "value": 1,
                    "timestamp": int(min(converted_at, now).timestamp() * 1000),
                    "traits": identity.traits,
                }
            )
        record(tally, identity)

    rng.shuffle(events)
    return events


def post_events(cfg: dict, events: list[dict], tally: Tally) -> None:
    import httpx

    fsc = cfg["flagsmith"]
    url = fsc["events_url"].rstrip("/") + "/v1/events"
    headers = {
        "X-Environment-Key": fsc["environment_key"],
        "Content-Type": "application/json",
    }
    tally.sent = len(events)
    with httpx.Client(timeout=30.0) as http:
        for start in range(0, len(events), BATCH_SIZE):
            batch = events[start : start + BATCH_SIZE]
            response = http.post(url, headers=headers, json={"events": batch})
            if response.status_code in (401, 403):
                sys.exit(f"{response.status_code} from {url}: check environment_key")
            if response.status_code >= 400 and response.status_code != 400:
                sys.exit(f"{response.status_code} from {url}: {response.text[:300]}")
            body = response.json()
            tally.accepted += body.get("accepted", 0)
            for item in body.get("rejected", []):
                if len(tally.rejected) < 5:
                    tally.rejected.append(item.get("error", json.dumps(item)))


# ----------------------------------------------------------------------- report


def record(tally: Tally, identity: Identity) -> None:
    variant = identity.variant or CONTROL
    if identity.exposed:
        tally.exposures[variant] += 1
    if identity.converted:
        tally.conversions[variant] += 1
    device = identity.traits.get("device", "-")
    cell = tally.by_device[(device, variant)]
    cell[0] += 1 if identity.exposed else 0
    cell[1] += 1 if identity.converted else 0


def report(cfg: dict, args: argparse.Namespace, tally: Tally) -> None:
    transport = "dry-run" if args.dry_run else args.transport
    print(f"\nmode={args.mode} transport={transport} users={args.users} seed={args.seed}")
    if args.dry_run:
        print(f"generated={tally.sent} (nothing posted)")
    else:
        print(f"sent={tally.sent} accepted={tally.accepted}")
    for problem in tally.rejected:
        print(f"  rejected: {problem}")

    print("\nper variant (observed → true):")
    for variant in sorted(set(tally.exposures) | set(tally.conversions)):
        n = tally.exposures[variant]
        c = tally.conversions[variant]
        observed = f"{c / n:.4f}" if n else "n/a"
        truth = true_rate(cfg, args.mode, variant, "")
        print(
            f"  {variant:<12} exposures={n:<6} conversions={c:<6} "
            f"observed={observed}  true(base)={truth:.4f}"
        )

    print("\nper device and variant (the segment story):")
    for (device, variant), (n, c) in sorted(tally.by_device.items()):
        observed = f"{c / n:.4f}" if n else "n/a"
        truth = true_rate(cfg, args.mode, variant, device)
        print(
            f"  {device:<9} {variant:<12} n={n:<6} conv={c:<6} "
            f"observed={observed}  true={truth:.4f}"
        )

    if args.mode == "srm":
        print(f"\nDropped ~{args.srm_drop:.0%} of non-control exposures: SRM should fire.")
    if args.mode == "flat":
        print("\nNo exposures sent: enrolment stays flat while conversions arrive.")
    if args.mode == "aa":
        print("\nIdentical rates: a correct tool declares no winner.")


# -------------------------------------------------------------------------- cli


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("sim.toml"))
    parser.add_argument("--mode", choices=["ab", "aa", "srm", "flat"], default="ab")
    parser.add_argument("--transport", choices=["sdk", "raw"], default="sdk")
    parser.add_argument("--users", type=int, default=500)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--prefix", default="sim_", help="identifier prefix; change it for a fresh cohort")
    parser.add_argument("--span-hours", type=int, default=0, help="raw only: spread timestamps over this window (max 240)")
    parser.add_argument("--split", type=float, default=50.0, help="raw only: percent of identities on a non-control variant")
    parser.add_argument("--srm-drop", type=float, default=0.3, help="srm mode: share of non-control exposures to drop")
    parser.add_argument("--guardrail", action="store_true", help="sdk only: also emit the [truth.guardrail] event")
    parser.add_argument("--dry-run", action="store_true", help="generate and report, send nothing")
    args = parser.parse_args()

    if not args.config.exists():
        sys.exit(f"no config at {args.config}")
    cfg = load_config(args.config)
    if args.span_hours > 240:
        sys.exit("--span-hours cannot exceed 240: events older than 10 days are rejected")
    if args.transport == "sdk" and args.mode == "srm" and not args.dry_run:
        print("note: over the SDK transport an exposure is recorded as the flag is read,")
        print("      so a dropped exposure cannot be simulated. Use --transport raw.")

    rng = random.Random(args.seed)
    ids = make_identities(cfg, args.users, args.prefix, rng)
    tally = Tally()

    if args.transport == "raw" or args.dry_run:
        assign_variants(args, ids, cfg["flagsmith"]["variants"])
        events = build_events(cfg, args, ids, tally)
        if args.dry_run:
            tally.sent = len(events)
            print(f"dry run: {len(events)} events, first two:")
            for item in events[:2]:
                print("  " + json.dumps(item))
        else:
            post_events(cfg, events, tally)
    else:
        if cfg["flagsmith"]["environment_key"] == "CHANGE_ME":
            sys.exit("set flagsmith.environment_key in the config first")
        run_sdk(cfg, args, ids, tally)

    report(cfg, args, tally)


if __name__ == "__main__":
    main()
