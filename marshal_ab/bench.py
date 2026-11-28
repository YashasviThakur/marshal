"""marshal bench: Marshal against the usual way of running an A/B test, on simulated experiments, no Flagsmith needed.

Each simulated experiment has a known true conversion rate for both arms and runs in rounds, like the demo. After every
round the experiment is read the way each policy reads it:
  fixed split + Flagsmith 95% interval   what "ship when Flagsmith is 95% sure" does, looked at every round
  fixed split + always-valid interval    the same traffic, read with Marshal's confidence sequence
  marshal                                Thompson sampling (same floor, holdback and step limit as `marshal run`),
                                         read with the confidence sequence
Reported per scenario: conversions per experiment, how often the treatment was called a winner (by the end, and at any
look: the false-winner rate when the true lift is 0 or negative), how often it was called a loser, and the average
round of the first winner call.
"""
import random
from statistics import mean

from .decide import Verdict, allocate, confidence_sequence, win_probabilities
from .stats import CONTROL, VariantStats, compare_to_control

TREATMENT = "treatment"
RULES = ("valid", "p95")  # always-valid confidence sequence, Flagsmith's fixed-sample 95% interval


def one_run(policy: str, base: float, lift: float, rounds: int, per_round: int, rng: random.Random) -> dict:
    rates = {CONTROL: base, TREATMENT: base * (1 + lift)}
    split = {CONTROL: 0.5, TREATMENT: 0.5}
    n, conv = dict.fromkeys(rates, 0), dict.fromkeys(rates, 0)
    first = dict.fromkeys(RULES)
    ever = {r: {"winner": False, "loser": False} for r in RULES}
    end = dict.fromkeys(RULES)
    for rnd in range(1, rounds + 1):
        users = rng.binomialvariate(per_round, split[TREATMENT])
        for arm, k in ((TREATMENT, users), (CONTROL, per_round - users)):
            n[arm] += k
            conv[arm] += rng.binomialvariate(k, rates[arm])
        arms = {a: VariantStats(n[a], conv[a], conv[a]) for a in rates}  # 0/1 outcomes: sum == sum of squares
        p = compare_to_control(arms[CONTROL], arms[TREATMENT])
        verdicts = {"valid": confidence_sequence(arms[CONTROL], arms[TREATMENT]),
                    "p95": p and Verdict(p.lift, p.ci_low, p.ci_high)}
        for rule, v in verdicts.items():
            end[rule] = v.state if v else None
            if v and v.state != "undecided":
                ever[rule][v.state] = True
                if v.state == "winner" and first[rule] is None:
                    first[rule] = rnd
        if policy == "marshal":
            p = win_probabilities(arms, occurrence=True, draws=1000, rng=rng)
            split = allocate(p, split)
    return {"conversions": sum(conv.values()), "ever": ever["valid"]["winner"], "end": end["valid"] == "winner",
            "first": first["valid"], "rules": {r: {"end": end[r] == "winner", "winner": ever[r]["winner"],
                                                   "loser": ever[r]["loser"], "first": first[r]} for r in RULES}}


def bench(args):
    rng = random.Random(args.seed)
    scenarios = [("A/A (no real effect)", 0.0)] + [(f"treatment {x:+.0%}", x) for x in args.lift]
    print(f"{args.runs} simulated experiments per row: base rate {args.base:.1%}, {args.rounds} rounds of "
          f"{args.per_round} users, every policy reads the experiment after every round\n")
    print("| scenario | policy | conversions / experiment | called winner at the end | called winner at any look | "
          "called loser at any look | first winner call (round) |")
    print("|---|---|---|---|---|---|---|")
    for label, lift in scenarios:
        res = {p: [one_run(p, args.base, lift, args.rounds, args.per_round, rng) for _ in range(args.runs)]
               for p in ("fixed", "marshal")}
        for name, policy, rule in (("fixed split, Flagsmith 95% interval", "fixed", "p95"),
                                   ("fixed split, always-valid interval", "fixed", "valid"),
                                   ("marshal", "marshal", "valid")):
            rs = res[policy]
            firsts = [r["rules"][rule]["first"] for r in rs if r["rules"][rule]["first"]]
            print(f"| {label} | {name} | {mean(r['conversions'] for r in rs):.1f} | "
                  f"{mean(r['rules'][rule]['end'] for r in rs):.1%} | {mean(r['rules'][rule]['winner'] for r in rs):.1%} | "
                  f"{mean(r['rules'][rule]['loser'] for r in rs):.1%} | {(f'{mean(firsts):.1f}' if firsts else '-')} |")
        gain = mean(r["conversions"] for r in res["marshal"]) - mean(r["conversions"] for r in res["fixed"])
        print(f"| | marshal minus fixed split | {gain:+.1f} | | | | |")
