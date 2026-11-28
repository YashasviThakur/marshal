import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from marshal_ab.bench import one_run  # noqa: E402


class Bench(unittest.TestCase):
    def test_marshal_earns_more_and_never_invents_a_winner(self):
        rng = random.Random(3)
        runs = {p: [one_run(p, 0.042, 0.5, 8, 2500, rng) for _ in range(60)] for p in ("fixed", "marshal")}
        mean = lambda p: sum(r["conversions"] for r in runs[p]) / len(runs[p])
        self.assertGreater(mean("marshal"), mean("fixed"))  # a real +50% effect: more users on the winner
        aa = [one_run("marshal", 0.042, 0.0, 8, 2500, rng) for _ in range(150)]
        self.assertLess(sum(r["ever"] for r in aa) / len(aa), 0.05)  # no effect: at most 5% called a winner at any look


if __name__ == "__main__":
    unittest.main()
