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
    def test_the_budget_is_a_runaway_guard_not_a_trimmer(self):
        """Under the client's 150s so a scan normally completes in band, but
        generous enough that a normal one never loses a stage. 95s was the
        first value and it was too aggressive: with the client now collecting
        an overrunning scan from cache, cutting early costs bids for no
        benefit."""
        self.assertLess(ls.SCAN_BUDGET_SEC, 150)
        self.assertGreaterEqual(ls.SCAN_BUDGET_SEC, 120)

    def test_enrichment_is_the_stage_sacrificed_first(self):
        """It adds no bids -- it fills contacts and deadlines on bids already
        found. So a slow scan should lose phone numbers before it loses
        listings, which the stage ORDER is what guarantees."""
        import inspect
        src = inspect.getsource(ls._perform_scan)
        order = [src.index('_stage(drop_stats, "%s"' % n)
                 for n in ("state", "federal", "agency", "enrich")]
        self.assertEqual(order, sorted(order),
                         "bid-producing stages must run before enrichment")

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


class PlanHolderBudgetTests(unittest.TestCase):
    """Plan holders are a per-job extra, not a bid.

    A Branson scan spent 19 of its 83 seconds in the state stage: 2 on the
    MoDOT letting page itself and the rest on twelve plan-holder fetches at
    four workers. A scan should not spend a fifth of itself on a detail that
    hangs off bids it has already found.

    Bounded by time rather than by raising the worker count, because the
    faster clock would come from leaning harder on one agency's server --
    the wrong trade to make with somebody else's infrastructure.
    """

    def setUp(self):
        import bid_sources
        self.bs = bid_sources
        self._orig = (bid_sources.plan_holder_index,
                      bid_sources.plan_holder_url_for_call,
                      bid_sources.parse_plan_holders, ls._fetch_page,
                      ls.PLAN_HOLDER_BUDGET_SEC)
        bid_sources.plan_holder_index = lambda h, u: "https://x/index"
        bid_sources.plan_holder_url_for_call = lambda i, c: "https://x/%s" % c
        bid_sources.parse_plan_holders = lambda p: [{"name": "Acme"}]
        self.calls = []

        def slow(url, timeout=None):
            self.calls.append(url)
            time.sleep(0.2)
            return ("<html></html>", "ok")
        ls._fetch_page = slow

    def tearDown(self):
        (self.bs.plan_holder_index, self.bs.plan_holder_url_for_call,
         self.bs.parse_plan_holders, ls._fetch_page,
         ls.PLAN_HOLDER_BUDGET_SEC) = self._orig

    def _run(self, budget):
        ls.PLAN_HOLDER_BUDGET_SEC = budget
        self.calls.clear()
        bids = [{"call": "C%d" % i} for i in range(12)]
        stats = {}
        ls._attach_plan_holders(bids, "<html/>", "https://x/letting", stats)
        return len(self.calls), sum(1 for b in bids if b.get("plan_holders")), stats

    def test_a_generous_budget_fetches_everything(self):
        n, got, stats = self._run(10.0)
        self.assertEqual(n, 12)
        self.assertEqual(got, 12)
        self.assertIsNone(stats.get("plan_holder_budget_spent"))

    def test_a_tight_budget_stops_partway_and_says_so(self):
        n, got, stats = self._run(0.25)
        self.assertLess(n, 12)
        self.assertGreater(n, 0)
        self.assertEqual(stats.get("plan_holder_budget_spent"), 1)

    def test_an_exhausted_budget_costs_nothing(self):
        n, got, stats = self._run(0.0)
        self.assertEqual(n, 0)
        self.assertEqual(stats.get("plan_holder_budget_spent"), 1)

    def test_running_out_never_costs_a_bid(self):
        """It degrades by dropping holder lists, not listings -- the bids are
        placed before this runs and are untouched by it."""
        ls.PLAN_HOLDER_BUDGET_SEC = 0.0
        bids = [{"call": "C%d" % i, "title": "job %d" % i} for i in range(12)]
        ls._attach_plan_holders(bids, "<html/>", "https://x/letting", {})
        self.assertEqual(len(bids), 12)
        self.assertTrue(all(b.get("title") for b in bids))


class BidOriginTests(unittest.TestCase):
    """Which half of the pipeline paid for each bid.

    Search became the most expensive stage in a scan -- 23.7 of 39.4 seconds
    on a Grant County run -- and nothing recorded whether it earned that.
    "kept" alone cannot answer it, so whether to keep spending twenty seconds
    on search queries was unanswerable from the data.
    """

    def test_place_bid_records_where_a_bid_came_from(self):
        import inspect
        src = inspect.getsource(ls._place_bid)
        self.assertIn("origin", inspect.signature(ls._place_bid).parameters)
        self.assertIn('_count("kept_from_" + origin)', src)

    def test_both_halves_are_tagged(self):
        """Tagging only one side would make the ratio meaningless."""
        import inspect
        src = inspect.getsource(ls)
        self.assertIn('origin="search"', src)
        self.assertIn('origin="portal"', src)

    def test_an_untagged_call_still_works(self):
        """Federal and agency bids do not carry an origin, and must not
        break or be miscounted because of it."""
        import inspect
        sig = inspect.signature(ls._place_bid)
        self.assertIsNone(sig.parameters["origin"].default)
