"""marshal bench: Marshal against a fixed 50/50 split on simulated experiments, no Flagsmith needed.

Each simulated experiment has a known true conversion rate for both arms and runs in rounds, like the demo. After every
round the always-valid confidence sequence is checked (peeking is legitimate for it). Policies:
  fixed   - 50/50 forever
  marshal - Thompson sampling with the same floor, holdback and step limit as `marshal run`
Reported per scenario: conversions per experiment, how often the treatment was called a winner by the end, and how
often at any look (the false-winner rate when the true lift is 0), plus the average round of the first winner call.
"""
import random
from statistics import mean

from .decide import allocate, confidence_sequence, win_probabilities
from .stats import CONTROL, VariantStats

TREATMENT = "treatment"


def one_run(policy: str, base: float, lift: float, rounds: int, per_round: int, rng: random.Random) -> dict:
    rates = {CONTROL: base, TREATMENT: base * (1 + lift)}
    split = {CONTROL: 0.5, TREATMENT: 0.5}
    n, conv = dict.fromkeys(rates, 0), dict.fromkeys(rates, 0)
    first_win, wins_at_end = None, False
    for r in range(1, rounds + 1):
        users = rng.binomialvariate(per_round, split[TREATMENT])
        for arm, k in ((TREATMENT, users), (CONTROL, per_round - users)):
            n[arm] += k
            conv[arm] += rng.binomialvariate(k, rates[arm])
        arms = {a: VariantStats(n[a], conv[a], conv[a]) for a in rates}  # 0/1 outcomes: sum == sum of squares
        v = confidence_sequence(arms[CONTROL], arms[TREATMENT])
        if v and v.state == "winner" and first_win is None:
            first_win = r
        wins_at_end = bool(v and v.state == "winner")
        if policy == "marshal":
            p = win_probabilities(arms, occurrence=True, draws=1000, rng=rng)
            split = allocate(p, split)
    return {"conversions": sum(conv.values()), "ever": first_win is not None, "end": wins_at_end, "first": first_win}


def bench(args):
    rng = random.Random(args.seed)
    scenarios = [("A/A (no real effect)", 0.0)] + [(f"treatment +{x:.0%}", x) for x in args.lift]
    print(f"{args.runs} simulated experiments per row: base rate {args.base:.1%}, {args.rounds} rounds of "
          f"{args.per_round} users, always-valid check after every round\n")
    print(f"| scenario | policy | conversions / experiment | called winner at the end | called winner at any look | "
          f"first call (round) |")
    print("|---|---|---|---|---|---|")
    for label, lift in scenarios:
        rows = {}
        for policy in ("fixed", "marshal"):
            res = [one_run(policy, args.base, lift, args.rounds, args.per_round, rng) for _ in range(args.runs)]
            rows[policy] = res
            firsts = [r["first"] for r in res if r["first"]]
            print(f"| {label} | {policy} | {mean(r['conversions'] for r in res):.1f} | "
                  f"{mean(r['end'] for r in res):.1%} | {mean(r['ever'] for r in res):.1%} | "
                  f"{(f'{mean(firsts):.1f}' if firsts else '-')} |")
        gain = mean(r["conversions"] for r in rows["marshal"]) - mean(r["conversions"] for r in rows["fixed"])
        print(f"| | marshal minus fixed | {gain:+.1f} | | | |")
