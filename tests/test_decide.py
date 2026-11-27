"""The claim Marshal rests on: continuous monitoring with the confidence sequence keeps the false-winner rate
under alpha on A/A data, where peeking at a fixed-sample 95% interval does not."""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from marshal_ab.decide import allocate, confidence_sequence, credibly_worse, guardrail_breaches, percentages, win_probabilities  # noqa: E402
from marshal_ab.measure import Metric  # noqa: E402
from marshal_ab.stats import VariantStats, compare_to_control  # noqa: E402


def aa_run(rng, peeks=40, batch=250, rate=0.05):
    """One A/A experiment looked at after every batch. -> (confidence sequence ever called it, fixed interval ever did)"""
    c = t = 0
    n = 0
    seq = fixed = False
    for _ in range(peeks):
        n += batch
        c += sum(rng.random() < rate for _ in range(batch))
        t += sum(rng.random() < rate for _ in range(batch))
        control, treatment = VariantStats(n, c, c), VariantStats(n, t, t)
        v = confidence_sequence(control, treatment)
        i = compare_to_control(control, treatment)
        seq |= bool(v) and v.state != "undecided"
        fixed |= bool(i) and (i.ci_low > 0 or i.ci_high < 0)
    return seq, fixed


class Decide(unittest.TestCase):
    def test_peeking_at_aa(self):
        rng = random.Random(7)
        runs = [aa_run(rng) for _ in range(400)]
        seq, fixed = (sum(r[i] for r in runs) / len(runs) for i in (0, 1))
        print(f"\nA/A, 40 peeks, 400 runs: confidence sequence {seq:.1%} false winners, fixed 95% interval {fixed:.1%}")
        self.assertLessEqual(seq, 0.05)
        self.assertGreater(fixed, 0.15)

    def test_guardrail_false_halts_at_aa(self):
        rng, halts = random.Random(11), 0
        for _ in range(400):
            c = t = n = 0
            for _ in range(40):
                n += 250
                c += sum(rng.random() < 0.02 for _ in range(250))
                t += sum(rng.random() < 0.02 for _ in range(250))
                if credibly_worse(VariantStats(n, c, c), VariantStats(n, t, t), True):
                    halts += 1
                    break
        print(f"\nguardrail A/A, 40 looks, 400 runs: {halts / 400:.1%} false halts")
        self.assertLessEqual(halts / 400, 0.05)

    def test_real_effect_is_called(self):
        v = confidence_sequence(VariantStats(20000, 840, 840), VariantStats(20000, 1000, 1000))
        self.assertEqual(v.state, "winner")

    def test_guardrail(self):
        refunds_up = {"control": VariantStats(20000, 400, 400), "t": VariantStats(20000, 600, 600)}
        refunds, signups = Metric(2, "refund", "occurrence", lower_is_better=True), Metric(3, "signup", "occurrence")
        ok = {"control": VariantStats(20000, 800, 800), "t": VariantStats(20000, 800, 800)}
        self.assertEqual(guardrail_breaches([refunds, signups], {2: refunds_up, 3: ok}), ["refund on t"])
        self.assertEqual(guardrail_breaches([refunds, signups], {2: ok, 3: ok}), [])

    def test_guardrail_catches_rare_event_spike(self):
        errors = Metric(2, "checkout_error", "occurrence", lower_is_better=True)
        spike = {"control": VariantStats(3000, 30, 30), "t": VariantStats(6000, 240, 240)}  # 1% -> 4%
        self.assertEqual(guardrail_breaches([errors], {2: spike}), ["checkout_error on t"])

    def test_win_probabilities(self):
        w = win_probabilities({"control": VariantStats(2000, 84, 84), "t": VariantStats(2000, 120, 120)},
                              occurrence=True, rng=random.Random(1))
        self.assertGreater(w["t"], 0.95)
        self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_allocation_floor_holdback_step(self):
        cur = {"control": 0.5, "t": 0.5}
        nxt = allocate({"control": 0.0, "t": 1.0}, cur)
        self.assertAlmostEqual(nxt["t"] - cur["t"], 0.10)  # capped by max_step
        self.assertAlmostEqual(sum(nxt.values()), 1.0)
        far = {"control": 0.1, "t": 0.9}
        self.assertGreaterEqual(allocate({"control": 0.0, "t": 1.0}, far)["control"], 0.1 * 0.8 + 0.1 - 1e-9 - 0.1)
        self.assertEqual(percentages({"control": 0.4, "t": 0.6}), {"t": 60})
        stable = allocate({"control": 0.5, "t": 0.5}, cur)  # no evidence, nothing moves
        self.assertAlmostEqual(stable["t"], 0.5)


if __name__ == "__main__":
    unittest.main()
