from __future__ import annotations

import unittest

from bounded_batch02_sampling import (
    LOCAL_LIMIT,
    TARGET_SEEDS,
    _batch02_seed_schedule,
    _remaining_local_budget,
)


class BoundedBatch02SamplingTests(unittest.TestCase):
    def test_dispatch_slice_is_exactly_the_authorized_original_keys(self) -> None:
        schedule = _batch02_seed_schedule()
        self.assertEqual(schedule[:128], tuple(range(660001, 660129)))
        self.assertEqual(schedule[128:256], TARGET_SEEDS)
        self.assertEqual(TARGET_SEEDS, tuple(range(660243, 660257)))
        self.assertNotIn(660239, schedule[128:256])
        self.assertNotIn(660241, schedule[128:256])

    def test_local_cpu_limit_keeps_setup_and_finalization_reserves_inside_cap(self) -> None:
        remaining = _remaining_local_budget(charged=200.0, setup=25.0, finalization_reserve=30.0)
        self.assertEqual(remaining, LOCAL_LIMIT - 255.0)
        self.assertLessEqual(200.0 + 25.0 + remaining + 30.0, LOCAL_LIMIT)
        self.assertLessEqual(_remaining_local_budget(charged=1950.0, setup=20.0, finalization_reserve=30.0), 0.0)


if __name__ == "__main__":
    unittest.main()
