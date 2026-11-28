"""`marshal run` end to end against a stub Flagsmith: the one write Marshal makes (PATCH rollout), when it holds
instead, and that a person is told once. No Flagsmith, Postgres or psycopg needed."""
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1]))
from marshal_ab import cli  # noqa: E402
from marshal_ab.stats import VariantStats  # noqa: E402

EXP = {"name": "Checkout", "status": "running", "started_at": "2026-11-28T10:00:00Z",
       "feature": {"name": "checkout_button", "multivariate_options": [{"id": 1, "key": "treatment"}]},
       "metrics": [{"metric": 10, "metric_name": "purchase"}, {"metric": 11, "metric_name": "checkout_error"}],
       "experiment_rollout": {"enabled": True, "rollout_percentage": 100, "audience": {"segments": []},
                              "multivariate_feature_state_values": [
                                  {"multivariate_feature_option": 1, "percentage_allocation": 50}]}}
METRICS = {10: {"id": 10, "definition": {"event": "purchase"}, "aggregation": "occurrence", "direction": "up"},
           11: {"id": 11, "definition": {"event": "checkout_error"}, "aggregation": "occurrence", "direction": "down"}}


def stats(errors=(20, 20)):
    """purchases 4.2% -> 6.3% (a clear winner); checkout errors as given."""
    return ({"control": 6000, "treatment": 6000},
            {10: {"control": VariantStats(6000, 252, 252), "treatment": VariantStats(6000, 378, 378)},
             11: {"control": VariantStats(6000, errors[0], errors[0]), "treatment": VariantStats(6000, errors[1], errors[1])}})


class Stub:
    patches: list = []

    def __init__(self, *a):
        pass

    def experiment(self, env_key, exp_id):
        return EXP

    def metric(self, env_key, metric_id):
        return METRICS[metric_id]

    def patch(self, path, body):
        Stub.patches.append((path, body))

    def mark_health(self, env_key, feature, healthy, text):
        Stub.health.append((feature, healthy, text))


class Run(unittest.TestCase):
    def setUp(self):
        Stub.patches, Stub.health = [], []
        self.cwd, self.tmp = os.getcwd(), tempfile.TemporaryDirectory()
        os.chdir(self.tmp.name)
        self.sent = []
        env = {"FLAGSMITH_API_URL": "http://flagsmith", "EXPERIMENT_ENV_KEY": "env", "EXPERIMENT_ID": "7",
               "MARSHAL_WAREHOUSE_DSN": "postgresql://stub"}
        for p in (mock.patch.dict(sys.modules, {"psycopg": mock.MagicMock()}),
                  mock.patch.object(cli, "settings", lambda e: (env, "key")), mock.patch.object(cli, "Admin", Stub),
                  mock.patch.object(cli, "notify", lambda url, text: self.sent.append(text)),
                  mock.patch.object(cli, "sample_ratio", lambda *a: ({}, "the experiment started", 0.5))):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(os.chdir, self.cwd)  # runs first: a directory in use cannot be deleted on Windows

    def cycle(self, data, mode="act", health=False):
        args = Namespace(env=None, mode=mode, every=0, floor=0.10, holdback=0.20, max_step=0.10, seed=1,
                         notify="http://hook", health=health)
        with mock.patch.object(cli, "read_stats", lambda *a, **k: data):
            cli.run(args)
        return [json.loads(l) for l in Path(".marshal/decisions.jsonl").read_text().splitlines()][-1]

    def test_act_moves_traffic_with_one_patch_and_says_so_once(self):
        rec = self.cycle(stats())
        self.assertEqual(rec["decision"], "reallocated")
        (path, body), = Stub.patches
        self.assertEqual(path, "/environments/env/experiments/7/rollout/")
        self.assertNotIn("audience", body)  # locked once the experiment is running
        self.assertEqual(body["multivariate_feature_state_values"],
                         [{"multivariate_feature_option": 1, "percentage_allocation": 60}])  # one 10-point step
        self.assertEqual(len(self.sent), 1)
        self.assertIn("treatment is a winner", self.sent[0])
        self.cycle(stats())
        self.assertEqual(len(self.sent), 1)  # same news, no second message

    def test_observe_moves_nothing(self):
        rec = self.cycle(stats(), mode="observe")
        self.assertEqual(rec["decision"], "reallocate")
        self.assertEqual(Stub.patches, [])

    def test_guardrail_holds(self):
        rec = self.cycle(stats(errors=(20, 80)))
        self.assertEqual(rec["decision"], "hold: guardrail breached by checkout_error on treatment")
        self.assertEqual(Stub.patches, [])
        self.assertTrue(any("guardrail breached" in m for m in self.sent))

    def test_health_marks_a_hold_unhealthy_once_and_healthy_when_it_clears(self):
        self.cycle(stats(errors=(20, 80)), health=True)
        self.cycle(stats(errors=(20, 80)), health=True)  # still held: not sent again
        self.assertEqual(len(Stub.health), 1)
        feature, healthy, text = Stub.health[0]
        self.assertEqual((feature, healthy), ("checkout_button", False))
        self.assertIn("guardrail breached by checkout_error", text)
        self.cycle(stats(), health=True)
        self.assertEqual([h[:2] for h in Stub.health], [("checkout_button", False), ("checkout_button", True)])

    def test_health_is_opt_in(self):
        self.cycle(stats(errors=(20, 80)))
        self.assertEqual(Stub.health, [])

    def test_sample_ratio_mismatch_holds(self):
        with mock.patch.object(cli, "sample_ratio", lambda *a: ({}, "the last reallocation", 0.0001)):
            rec = self.cycle(stats())
        self.assertTrue(rec["decision"].startswith("hold: sample ratio mismatch"))
        self.assertEqual(Stub.patches, [])
        self.assertTrue(any("sample ratio mismatch" in m for m in self.sent))

    def test_stalled_events_are_reported(self):
        flat = ({"control": 100, "treatment": 100}, {k: {v: VariantStats(100, 4, 4) for v in ("control", "treatment")}
                                                     for k in (10, 11)})
        self.cycle(flat, mode="observe")
        self.assertEqual(self.sent, [])
        self.cycle(flat, mode="observe")  # identical counts: nothing new arrived
        self.assertEqual(len(self.sent), 1)
        self.assertIn("no new identities", self.sent[0])


if __name__ == "__main__":
    unittest.main()
