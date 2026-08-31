"""One wall clock for the whole scan.

Every stage had its own budget and they simply added up: 40s of known-town
reads, then unbudgeted search and extraction, then state pages, then 25s of
federal, then enrichment, then plan holders. Nothing capped the sum. A
125-mile scan from Republic, MO ran past the client's 150-second timeout --
it finished and banked 32 bids, the best result any scan had produced, and
the phone had already shown "that took too long".
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class StageGuardTests(unittest.TestCase):
    def test_a_stage_runs_and_is_timed_when_there_is_time(self):
        stats = {}
        out = ls._stage(stats, "demo", time.time() + 60, lambda: "ran")
        self.assertEqual(out, "ran")
        self.assertIn("ms_demo", stats)
        self.assertNotIn("skipped_demo", stats)

    def test_a_stage_is_skipped_once_the_budget_is_gone(self):
        stats = {}
        called = []
        out = ls._stage(stats, "demo", time.time() - 1,
                        lambda: called.append(1))
        self.assertIsNone(out)
        self.assertEqual(called, [])
        self.assertEqual(stats.get("skipped_demo"), 1)

    def test_a_raising_stage_is_still_timed(self):
        """The timing lives in a finally, so a stage that blows up still
        reports what it cost before it did."""
        stats = {}
        with self.assertRaises(ValueError):
            ls._stage(stats, "demo", time.time() + 60,
                      lambda: (_ for _ in ()).throw(ValueError("x")))
        self.assertIn("ms_demo", stats)

    def test_no_deadline_means_never_skip(self):
        stats = {}
        self.assertEqual(ls._stage(stats, "demo", None, lambda: "ran"), "ran")


class BudgetTests(unittest.TestCase):
    def test_the_budget_is_shorter_than_the_client_timeout(self):
        """The app gives up at 150s. A scan that returns 28 bids in 90
        seconds beats one that returns 32 in 160 and is never seen."""
        self.assertLess(ls.SCAN_BUDGET_SEC, 150)

    def test_the_additive_stages_are_the_ones_guarded(self):
        """The core town-and-portal read must always run -- skipping it would
        return an empty scan rather than a shorter one."""
        import inspect
        src = inspect.getsource(ls._perform_scan)
        for stage in ("state", "federal", "agency", "enrich"):
            self.assertIn('_stage(drop_stats, "%s"' % stage, src)
        self.assertNotIn('_stage(drop_stats, "known"', src)

    def test_the_clock_starts_after_the_cache_check(self):
        """A cached hit returns before any of this; it must not start a
        clock or record stage timings."""
        import inspect
        src = inspect.getsource(ls._perform_scan)
        self.assertLess(src.index('"cached": True'),
                        src.index("scan_deadline = time.time()"))


class StateSourceConcurrencyTests(unittest.TestCase):
    def test_the_fetches_are_parallel_but_placement_is_not(self):
        """_place_state_bid mutates grouped and city_coords. The win is
        entirely in the waiting, so only the fetch is threaded -- making the
        mutation concurrent would add a class of bug for no gain."""
        import inspect
        src = inspect.getsource(ls._run_state_sources)
        self.assertIn("ThreadPoolExecutor", src)
        fetch = src.index("ex.map(_load, todo)")
        # The CALL, not the docstring's mention of it.
        place = src.index("kept = _place_state_bid(")
        self.assertLess(fetch, place)
        # placement must sit in a plain loop, not inside the executor block
        self.assertIn("for st, url, page, outcome in fetched:", src)


if __name__ == "__main__":
    unittest.main()
