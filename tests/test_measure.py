"""Statistics are plain unit tests. The SQL runs against a TEMPORARY table on a real Postgres
(MARSHAL_TEST_DSN, default: the sandbox on :5442) and is skipped when none is reachable."""
import math
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
from marshal_ab.measure import Metric, counts_since, read_stats  # noqa: E402
from marshal_ab.stats import VariantStats, compare_to_control, infer, srm_p_value  # noqa: E402


class Stats(unittest.TestCase):
    def test_lift_interval_and_chance(self):
        control, treatment = VariantStats(1000, 40, 40), VariantStats(1000, 50, 50)
        r = compare_to_control(control, treatment)
        self.assertAlmostEqual(r.lift, 0.25)
        se = (r.ci_high - r.lift) / 1.959963984540054
        # delta method by hand: var(t)/(n_t m_c^2) + m_t^2 var(c)/(n_c m_c^4)
        vt, vc = (50 - 50**2 / 1000) / 999, (40 - 40**2 / 1000) / 999
        self.assertAlmostEqual(se, math.sqrt(vt / (1000 * 0.04**2) + 0.05**2 * vc / (1000 * 0.04**4)))
        self.assertGreater(r.chance_to_win, 0.5)

    def test_floors_and_lower_is_better(self):
        self.assertIsNone(infer(VariantStats(49, 5, 5), VariantStats(60, 6, 6), occurrence=True, lower_is_better=False))
        self.assertIsNone(infer(VariantStats(60, 4, 4), VariantStats(60, 6, 6), occurrence=True, lower_is_better=False))
        up = infer(VariantStats(500, 20, 20), VariantStats(500, 30, 30), occurrence=True, lower_is_better=False)
        down = infer(VariantStats(500, 20, 20), VariantStats(500, 30, 30), occurrence=True, lower_is_better=True)
        self.assertAlmostEqual(up.chance_to_win + down.chance_to_win, 1.0)

    def test_srm(self):
        self.assertGreater(srm_p_value([500, 500], [0.5, 0.5]), 0.99)
        self.assertLess(srm_p_value([600, 400], [0.5, 0.5]), 0.001)
        self.assertIsNone(srm_p_value([500, 500], [1.0, 0.0]))  # what Flagsmith ends up passing for experiments


def connect():
    try:
        import psycopg
        return psycopg.connect(os.environ.get("MARSHAL_TEST_DSN",
                                              "postgresql://postgres:password@localhost:5442/flagsmith"), connect_timeout=3)
    except Exception:
        return None


T0 = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)


class Sql(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = connect()
        if cls.conn is None:
            raise unittest.SkipTest("no Postgres for the SQL tests")
        rows = [  # (identifier, event, feature_name, minutes after T0, value)
            ("a", "$flag_exposure", "f", 1, "control"), ("a", "buy", None, 5, "1"),
            ("b", "$flag_exposure", "f", 1, "treatment"), ("b", "buy", None, 2, "1"), ("b", "buy", None, 3, "1"),
            ("c", "$flag_exposure", "f", 2, "control"),
            # d bought BEFORE being exposed: not attributed
            ("d", "buy", None, 1, "1"), ("d", "$flag_exposure", "f", 4, "treatment"),
            # e was moved control -> treatment mid-run, then bought
            ("e", "$flag_exposure", "f", 1, "control"), ("e", "$flag_exposure", "f", 30, "treatment"),
            ("e", "buy", None, 31, "1"),
            # outside the window, other feature, other environment: all ignored
            ("f", "$flag_exposure", "f", 200, "control"), ("g", "$flag_exposure", "other", 1, "control"),
        ]
        with cls.conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE ev (id bigint generated always as identity, environment_key text, event text,"
                        " feature_name text, timestamp timestamptz, identifier text, value text, traits text)")
            cur.executemany("INSERT INTO ev (environment_key, event, feature_name, timestamp, identifier, value)"
                            " VALUES ('k', %s, %s, %s, %s, %s)",
                            [(e, f, T0 + timedelta(minutes=m), i, v) for i, e, f, m, v in rows])
            cur.execute("INSERT INTO ev (environment_key, event, feature_name, timestamp, identifier, value)"
                        " VALUES ('other-env', '$flag_exposure', 'f', %s, 'h', 'control')", (T0,))
            # traits ride on exposure events; e's first exposure says android, its later one says desktop
            cur.execute("UPDATE ev SET traits = '{\"device\": \"desktop\"}' WHERE event = '$flag_exposure'")
            cur.execute("UPDATE ev SET traits = '{\"device\": \"android\"}' WHERE identifier IN ('a', 'e')"
                        " AND event = '$flag_exposure' AND timestamp < %s", (T0 + timedelta(minutes=10),))

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def stats(self, attribution, aggregation="occurrence"):
        return read_stats(self.conn, "k", "f", T0, T0 + timedelta(hours=1), [Metric(1, "buy", aggregation)],
                          table="ev", attribution=attribution)

    def test_quarantine_matches_flagsmith_rule(self):
        counts, stats = self.stats("quarantine")
        self.assertEqual(counts, {"control": 2, "treatment": 2})  # a, c | b, d ; e quarantined
        self.assertEqual(stats[1]["control"].sum, 1)  # a
        self.assertEqual(stats[1]["treatment"].sum, 1)  # b; d's purchase came before exposure

    def test_first_exposure_keeps_moved_identities(self):
        counts, stats = self.stats("first")
        self.assertEqual(counts, {"control": 3, "treatment": 2})  # e counts for control, where it started
        self.assertEqual(stats[1]["control"].sum, 2)  # a and e

    def test_segments_use_the_trait_at_first_exposure(self):
        counts, stats = read_stats(self.conn, "k", "f", T0, T0 + timedelta(hours=1), [Metric(1, "buy", "occurrence")],
                                   table="ev", attribution="first", trait="device")
        self.assertEqual(counts, {"android": {"control": 2}, "desktop": {"control": 1, "treatment": 2}})  # a, e | c | b, d
        self.assertEqual(stats[1]["android"]["control"].sum, 2)

    def test_counts_since_skips_identities_seen_before(self):
        end = T0 + timedelta(hours=1)
        self.assertEqual(counts_since(self.conn, "k", "f", T0, end, "ev"), {"control": 3, "treatment": 2})  # a c e | b d
        # at +10 min only e's move (first seen +1) is excluded; nobody was first seen later
        self.assertEqual(counts_since(self.conn, "k", "f", T0 + timedelta(minutes=10), end, "ev"), {})

    def test_count_aggregation(self):
        _, stats = self.stats("quarantine", "count")
        self.assertEqual((stats[1]["treatment"].sum, stats[1]["treatment"].sum_squares), (2, 4))  # b bought twice


if __name__ == "__main__":
    unittest.main()
