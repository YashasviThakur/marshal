"""marshal status: an experiment's result from Marshal's own SQL, side by side with Flagsmith's scorecard.

  python -m marshal_ab status [--env sandbox/.env] [--attribution first|quarantine] [--sql]

Settings (from --env or the environment): FLAGSMITH_API_URL, an admin key (FLAGSMITH_API_KEY or
FLAGSMITH_API_KEY_<host>_<port>), EXPERIMENT_ENV_KEY, EXPERIMENT_ID, MARSHAL_WAREHOUSE_DSN
(the Postgres holding the events), MARSHAL_EVENTS_TABLE (default: the "Flagsmith database" table).
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import json
import random
import time

from .decide import allocate, confidence_sequence, guardrail_breaches, percentages, win_probabilities
from .flagsmith import Admin
from .measure import FLAGSMITH_DATABASE_TABLE, Metric, counts_since, read_stats, results_sql
from .stats import CONTROL, MIN_IDENTITIES_PER_VARIANT, SRM_MIN_TOTAL_IDENTITIES, infer, srm_p_value

FIELDS = ("lift", "ci_low", "ci_high", "chance_to_win")


def settings(env_file):
    s = dict(os.environ)
    if env_file and Path(env_file).exists():
        s.update(l.split("=", 1) for l in Path(env_file).read_text().splitlines() if "=" in l)
    key = s.get("FLAGSMITH_API_KEY") or next((v for k, v in s.items() if k.startswith("FLAGSMITH_API_KEY_")), None)
    missing = [k for k in ("FLAGSMITH_API_URL", "EXPERIMENT_ENV_KEY", "EXPERIMENT_ID", "MARSHAL_WAREHOUSE_DSN")
               if not s.get(k)] + ([] if key else ["FLAGSMITH_API_KEY"])
    if missing:
        sys.exit("missing settings: " + ", ".join(missing))
    return s, key


def ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def configured_split(exp) -> dict[str, float]:
    """The split the experiment is serving: each variant's rollout share, control takes the rest."""
    keys = {o["id"]: o["key"] for o in exp["feature"]["multivariate_options"]}
    shares = {keys[v["multivariate_feature_option"]]: v["percentage_allocation"] / 100
              for v in exp["experiment_rollout"]["multivariate_feature_state_values"]}
    return {CONTROL: 1 - sum(shares.values()), **shares}


def sample_ratio(s, exp, table, start):
    """Chi-squared p-value of the identities first seen since the last reallocation against the split now served
    (cumulative counts stop matching a split that has moved). -> (split, since-when text, p or None)"""
    import psycopg

    split, since, when = configured_split(exp), start, "the experiment started"
    log = Path(".marshal/decisions.jsonl")
    moves = [json.loads(l) for l in log.read_text().splitlines() if "reallocated" in l] if log.exists() else []
    moves = [m for m in moves if m["decision"] == "reallocated"]
    if moves:  # only identities first seen under the split now being served say anything about that split
        last = moves[-1]
        since = datetime.fromisoformat(last.get("applied_at") or last["at"]).astimezone(start.tzinfo)
        when = f"the last reallocation ({since:%H:%M:%S} UTC)"
    with psycopg.connect(s["MARSHAL_WAREHOUSE_DSN"]) as conn:
        fresh = counts_since(conn, s["EXPERIMENT_ENV_KEY"], exp["feature"]["name"], since, datetime.now(start.tzinfo), table)
    total = sum(fresh.values())
    return split, when, (srm_p_value([fresh.get(v, 0) for v in split], list(split.values()))
                         if total >= SRM_MIN_TOTAL_IDENTITIES else None)


def status(args):
    import psycopg

    s, key = settings(args.env)
    api = Admin(s["FLAGSMITH_API_URL"], key)
    env_key, exp_id = s["EXPERIMENT_ENV_KEY"], s["EXPERIMENT_ID"]
    exp = api.experiment(env_key, exp_id)
    metrics = []
    for em in exp["metrics"]:
        m = api.metric(env_key, em["metric"])
        metrics.append(Metric(m["id"], m["definition"]["event"], m["aggregation"], m["direction"] == "down"))
    names = {m["metric"]: m["metric_name"] for m in exp["metrics"]}
    theirs = (api.results(env_key, exp_id) or {}).get("results") or {}
    start = ts(exp["started_at"])
    # Compare over the exact window Flagsmith last computed, or up to now when it has not computed yet.
    end = ts(theirs["as_of"]) if theirs.get("as_of") else datetime.now(start.tzinfo)
    table = s.get("MARSHAL_EVENTS_TABLE", FLAGSMITH_DATABASE_TABLE)

    if args.sql:
        print(results_sql(metrics, table, args.attribution))
        return
    with psycopg.connect(s["MARSHAL_WAREHOUSE_DSN"]) as conn:
        counts, stats = read_stats(conn, env_key, exp["feature"]["name"], start, end, metrics, table, args.attribution)
        if args.attribution != "quarantine":
            q_counts, q_stats = read_stats(conn, env_key, exp["feature"]["name"], start, end, metrics, table, "quarantine")
        else:
            q_counts, q_stats = counts, stats

    print(f"experiment {exp_id} \"{exp['name']}\" {exp['status']} | window {start:%Y-%m-%d %H:%M} -> {end:%H:%M:%S} UTC"
          f" | attribution: {args.attribution}")
    for m in metrics:
        print(f"\nmetric {names[m.id]} ({m.aggregation}, {'lower' if m.lower_is_better else 'higher'} is better)")
        print(f"  {'variant':<10} {'n':>6} {'sum':>8} {'mean':>8} {'lift':>8} {'95% interval':>18} {'chance to win':>14}")
        per = stats[m.id]
        for variant in sorted(per, key=lambda v: (v != CONTROL, v)):
            vs = per[variant]
            line = f"  {variant:<10} {vs.n:>6} {vs.sum:>8g} {vs.mean:>8.4f}"
            if variant != CONTROL:
                inf = infer(per.get(CONTROL), vs, occurrence=m.aggregation == "occurrence",
                            lower_is_better=m.lower_is_better)
                line += (f" {inf.lift:>+8.1%} {f'[{inf.ci_low:+.1%}, {inf.ci_high:+.1%}]':>18} {inf.chance_to_win:>14.1%}"
                         if inf else "   collecting data (below Flagsmith's floors)")
            print(line)

    split, when, srm = sample_ratio(s, exp, table, start)
    print(f"\nsample ratio vs the split being served ({', '.join(f'{k} {v:.0%}' for k, v in split.items())}), "
          f"identities first seen since {when}: "
          + ("too little traffic" if srm is None else f"p = {srm:.3f}" + ("  MISMATCH" if srm < 0.001 else "  ok")))

    if not theirs.get("payload"):
        print("\nFlagsmith has not computed results yet: POST .../results/refresh/ and rerun to compare")
        return
    compare(theirs, q_stats, metrics, names, srm_flagsmith=theirs["payload"].get("srm_p_value"))


def compare(theirs, q_stats, metrics, names, srm_flagsmith):
    """Every number Flagsmith reports, recomputed from the raw events with Flagsmith's own attribution rule."""
    worst, checked = 0.0, 0
    for tm in theirs["payload"]["metrics"]:
        m = next(x for x in metrics if x.id == tm["metric_id"])
        mine = q_stats[m.id]
        for variant, v in tm["variants"].items():
            for field in ("n", "sum", "sum_squares"):
                worst = max(worst, abs(getattr(mine[variant], field) - v[field]))
                checked += 1
        for variant, inf in tm["inference"].items():
            ours = infer(mine.get(CONTROL), mine[variant], occurrence=m.aggregation == "occurrence",
                         lower_is_better=m.lower_is_better)
            if (inf is None) != (ours is None):
                print(f"MISMATCH {names[m.id]} {variant}: Flagsmith {inf} vs Marshal {ours}")
                worst = float("inf")
                continue
            for field in FIELDS if inf else ():
                worst = max(worst, abs(getattr(ours, field) - inf[field]))
                checked += 1
    verdict = "MATCH" if worst < 1e-9 else "DIFFERENT"
    print(f"\nFlagsmith scorecard ({theirs['as_of']}): {verdict} on {checked} numbers, largest difference {worst:.1e}")
    if srm_flagsmith is None:
        print("  Flagsmith's sample-ratio check is empty: it reads the environment default split (0% treatment here),"
              " not the experiment's; Marshal checks against the split actually served")


def segments(args):
    """The overall result taken apart by a trait. Each segment is judged with the confidence sequence at alpha / k
    (Bonferroni over the k segments), so slicing does not turn noise into a finding."""
    import psycopg

    s, key = settings(args.env)
    api = Admin(s["FLAGSMITH_API_URL"], key)
    env_key, exp_id = s["EXPERIMENT_ENV_KEY"], s["EXPERIMENT_ID"]
    exp = api.experiment(env_key, exp_id)
    m = api.metric(env_key, exp["metrics"][0]["metric"])
    metric = Metric(m["id"], m["definition"]["event"], m["aggregation"], m["direction"] == "down")
    start = ts(exp["started_at"])
    with psycopg.connect(s["MARSHAL_WAREHOUSE_DSN"]) as conn:
        _, by_segment = read_stats(conn, env_key, exp["feature"]["name"], start, datetime.now(start.tzinfo),
                                   [metric], s.get("MARSHAL_EVENTS_TABLE", FLAGSMITH_DATABASE_TABLE), "first", args.trait)
    groups = {seg: v for seg, v in by_segment[metric.id].items() if CONTROL in v and v[CONTROL].n >= 2}
    alpha = 0.05 / max(1, len(groups))
    print(f"{exp['name']}: metric {metric.event}, split by trait {args.trait!r}, "
          f"{len(groups)} segments, each judged at alpha = {alpha:.4f}")
    print(f"  {'segment':<12} {'variant':<10} {'n':>6} {'mean':>8} {'lift':>8} {'always-valid interval':>24}  verdict")
    for seg, arms in sorted(groups.items()):
        for variant in sorted(v for v in arms if v != CONTROL):
            v = confidence_sequence(arms[CONTROL], arms[variant], alpha)
            print(f"  {seg or '(none)':<12} {variant:<10} {arms[variant].n:>6} {arms[variant].mean:>8.4f}"
                  + (f" {v.lift:>+8.1%} {f'[{v.low:+.1%}, {v.high:+.1%}]':>24}  {v.state}" if v else "   not enough data"))


def alert(record, prev):
    """(key, text) when this cycle has something worth telling a person, else None. Flagsmith sends nothing when a
    result becomes real, a guardrail trips, assignment breaks or events stop: the key lets Marshal say each once."""
    if record["decision"].startswith("hold:"):
        return record["decision"].split(" (")[0], record["decision"]
    called = {v: x for v, x in (record.get("verdicts") or {}).items() if x[0] != "undecided"}
    if called:
        v, (state, lift, low, high) = next(iter(sorted(called.items())))
        return f"{v} is a {state}", f"{v} is a {state}: lift {lift:+.1%}, always-valid interval [{low:+.1%}, {high:+.1%}]"
    if prev and prev.get("identities") and prev["identities"] == record["identities"]:
        return "stalled", "no new identities since the last cycle: is exposure tracking still on?"


def notify(url, text):
    """POST the message as JSON ({"text"} for Slack and Mattermost, {"content"} for Discord). Never stops the loop."""
    import urllib.request

    try:
        req = urllib.request.Request(url, json.dumps({"text": text, "content": text}).encode(),
                                     {"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        print(f"notify failed: {e}", file=sys.stderr)


def run(args):
    """One measure -> decide -> act cycle (looped with --every). Observe mode only reports what it would do."""
    import psycopg

    s, key = settings(args.env)
    api = Admin(s["FLAGSMITH_API_URL"], key)
    env_key, exp_id = s["EXPERIMENT_ENV_KEY"], s["EXPERIMENT_ID"]
    table = s.get("MARSHAL_EVENTS_TABLE", FLAGSMITH_DATABASE_TABLE)
    while True:
        exp = api.experiment(env_key, exp_id)
        if exp["status"] != "running":
            return print(f"experiment is {exp['status']}, nothing to do")
        metrics = []
        for em in exp["metrics"]:
            m = api.metric(env_key, em["metric"])
            metrics.append(Metric(m["id"], m["definition"]["event"], m["aggregation"], m["direction"] == "down"))
        metric = metrics[0]  # the first metric steers the traffic; the others are guardrails
        start = ts(exp["started_at"])
        with psycopg.connect(s["MARSHAL_WAREHOUSE_DSN"]) as conn:
            counts, stats = read_stats(conn, env_key, exp["feature"]["name"], start,
                                       datetime.now(start.tzinfo), metrics, table, "first")
        arms = stats[metric.id]
        current = configured_split(exp)
        enough = all(a.n >= MIN_IDENTITIES_PER_VARIANT for a in arms.values()) and set(arms) == set(current)
        record = {"at": datetime.now().isoformat(timespec="seconds"), "mode": args.mode, "current": current,
                  "identities": counts}
        if not enough:
            record["decision"] = "collecting data"
        else:
            wins = win_probabilities(arms, occurrence=metric.aggregation == "occurrence",
                                     lower_is_better=metric.lower_is_better, rng=random.Random(args.seed))
            verdicts = {v: confidence_sequence(arms[CONTROL], arms[v]) for v in arms if v != CONTROL}
            record.update(chance_best=wins, verdicts={v: (x.state, round(x.lift, 4), round(x.low, 4), round(x.high, 4))
                                                      for v, x in verdicts.items() if x})
            breaches = guardrail_breaches(metrics[1:], stats)
            _, when, srm = sample_ratio(s, exp, table, start)
            if srm is not None and srm < 0.001:  # users are not arriving in the split being served: the data is suspect
                record["decision"] = f"hold: sample ratio mismatch (p = {srm:.4f}, identities first seen since {when})"
            elif breaches:  # a guardrail metric is credibly worse for a variant: stop moving traffic toward it
                record["decision"] = "hold: guardrail breached by " + ", ".join(breaches)
            else:
                proposed = allocate(wins, current, floor=args.floor, holdback=args.holdback, max_step=args.max_step)
                record["proposed"] = proposed
                record["decision"] = "reallocate" if percentages(proposed) != percentages(current) else "hold"
                if record["decision"] == "reallocate" and args.mode == "act":
                    options = {o["key"]: o["id"] for o in exp["feature"]["multivariate_options"]}
                    body = {k: v for k, v in exp["experiment_rollout"].items() if k != "audience"}  # audience is locked once running
                    body["multivariate_feature_state_values"] = [
                        {"multivariate_feature_option": options[v], "percentage_allocation": p}
                        for v, p in percentages(proposed).items()]
                    api.patch(f"/environments/{env_key}/experiments/{exp_id}/rollout/", body)
                    record["applied_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                    record["decision"] = "reallocated"
        log = Path(".marshal/decisions.jsonl")
        lines = log.read_text().splitlines() if log.exists() else []
        prev = json.loads(lines[-1]) if lines else {}
        message = alert(record, prev)
        record["alert"] = message and message[0]
        print(json.dumps(record))
        Path(".marshal").mkdir(exist_ok=True)
        with open(log, "a") as f:
            f.write(json.dumps(record) + chr(10))
        if message and message[0] != prev.get("alert") and (args.notify or s.get("MARSHAL_NOTIFY_URL")):
            notify(args.notify or s["MARSHAL_NOTIFY_URL"], f"Marshal, experiment \"{exp['name']}\": {message[1]}")
        held, was_held = record["decision"].startswith("hold:"), prev.get("decision", "").startswith("hold:")
        if args.health and ((held and record["alert"] != prev.get("alert")) or (was_held and not held)):
            try:  # Unhealthy in Flagsmith while traffic is held (with the reason), Healthy again afterwards
                api.mark_health(env_key, exp["feature"]["name"], not held,
                                record["decision"] if held else "Marshal is steering traffic again")
            except Exception as e:
                print(f"feature health failed: {e}", file=sys.stderr)
        if not args.every:
            return
        time.sleep(args.every * 60)


def main():
    ap = argparse.ArgumentParser(prog="marshal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="result from Marshal's SQL, checked against Flagsmith's scorecard")
    st.add_argument("--env", default="sandbox/.env")
    st.add_argument("--attribution", choices=["first", "quarantine"], default="first")
    st.add_argument("--sql", action="store_true", help="print the query and stop")
    sg = sub.add_parser("segments", help="the result split by a trait, with a multiple-comparison correction")
    sg.add_argument("trait")
    sg.add_argument("--env", default="sandbox/.env")
    sv = sub.add_parser("serve", help="read-only dashboard: split, evidence, segments, guardrails, decision log")
    sv.add_argument("--env", default="sandbox/.env")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--trait", default="device", help="trait the segment table starts on")
    sv.add_argument("--host", default="127.0.0.1", help="0.0.0.0 inside a container")
    sv.add_argument("--replay", help="serve a file saved by `snapshot` instead of reading Flagsmith (no setup needed)")
    sp = sub.add_parser("snapshot", help="save the dashboard's current state to a file for `serve --replay`")
    sp.add_argument("--env", default="sandbox/.env")
    sp.add_argument("--out", default="docs/replay.json")
    sp.add_argument("--traits", nargs="+", default=["device"])
    rn = sub.add_parser("run", help="measure, decide and (in act mode) move traffic toward the better variant")
    rn.add_argument("--env", default="sandbox/.env")
    rn.add_argument("--mode", choices=["observe", "act"], default="observe")
    rn.add_argument("--every", type=float, default=0, help="minutes between cycles; 0 runs once")
    rn.add_argument("--floor", type=float, default=0.10, help="smallest share any variant keeps")
    rn.add_argument("--holdback", type=float, default=0.20, help="share of traffic kept evenly split")
    rn.add_argument("--max-step", type=float, default=0.10, help="largest share change per cycle")
    rn.add_argument("--seed", type=int)
    rn.add_argument("--health", action="store_true", help="show a hold on the feature inside Flagsmith: Unhealthy with the "
                    "reason (Feature Health, via a Webhook provider created in the project), Healthy again afterwards")
    rn.add_argument("--notify", help="URL to POST a message to (Slack, Discord, Mattermost) when a variant is called, "
                    "traffic is held, assignment breaks or events stop; or set MARSHAL_NOTIFY_URL")
    bn = sub.add_parser("bench", help="simulated experiments: Marshal vs a fixed 50/50 split (no Flagsmith needed)")
    bn.add_argument("--runs", type=int, default=300)
    bn.add_argument("--rounds", type=int, default=8)
    bn.add_argument("--per-round", type=int, default=2500)
    bn.add_argument("--base", type=float, default=0.042, help="control conversion rate")
    bn.add_argument("--lift", type=float, nargs="+", default=[0.20, 0.50, -0.20], help="true relative lifts to simulate")
    bn.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()
    if args.cmd == "bench":
        from .bench import bench
        return bench(args)
    if args.cmd == "serve":
        from .dashboard import serve
        return serve(args)
    if args.cmd == "snapshot":
        from .dashboard import record
        return record(args)
    {"status": status, "run": run, "segments": segments}[args.cmd](args)


if __name__ == "__main__":
    main()
