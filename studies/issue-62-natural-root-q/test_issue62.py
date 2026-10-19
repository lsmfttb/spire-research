import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run_issue62 as study


class Issue62MetricsTests(unittest.TestCase):
    def test_registered_population_and_root_ordinals(self):
        self.assertEqual(len(study.OWNER_SEEDS), 96)
        self.assertEqual(study.OWNER_SEEDS[0], 6210001)
        self.assertEqual(study.OWNER_SEEDS[-1], 6210096)
        self.assertEqual(study.selected_root_ordinal(6210001), 1)
        self.assertTrue(all(1 <= study.selected_root_ordinal(seed) <= 6 for seed in study.OWNER_SEEDS))

    def test_stream_derivation_is_owner_and_domain_separated(self):
        a = study.derive_seed(study.FUTURE_DOMAIN, 6210001, 0)
        b = study.derive_seed(study.FUTURE_DOMAIN, 6210001, 1)
        c = study.derive_seed(study.FUTURE_DOMAIN, 6210002, 0)
        prefix = study.derive_seed(study.PREFIX_DOMAIN, 6210001)
        self.assertEqual(a, study.derive_seed(study.FUTURE_DOMAIN, 6210001, 0))
        self.assertEqual(len({a, b, c, prefix}), 4)

    def test_owner_weighted_kendall_tau_a(self):
        self.assertEqual(study.tau_a([1.0, 2.0, 3.0], [10.0, 20.0, 30.0]), 1.0)
        self.assertEqual(study.tau_a([3.0, 2.0, 1.0], [10.0, 20.0, 30.0]), -1.0)
        self.assertAlmostEqual(study.tau_a([1.0, 1.0, 2.0], [1.0, 3.0, 4.0]), 2.0 / 3.0)

    def test_linear_percentile_interpolation(self):
        self.assertEqual(study.percentile([2.0, 2.0, 2.0], 0.025), 2.0)
        self.assertEqual(study.percentile([0.0, 10.0], 0.25), 2.5)


if __name__ == "__main__":
    unittest.main()
