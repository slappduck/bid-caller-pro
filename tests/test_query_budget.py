"""Bounds on how many searches one scan may cost.

Search was the runaway line item: 42 queries on a typical scan, 60 worst
case, which burned Tavily's monthly allowance in days. Every provider worth
using is metered, so the query count is a budget, not an implementation
detail -- and it grows silently, one plausible-looking query at a time.

These assert the shape of that budget so a future addition has to be a
deliberate choice rather than an accident.
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "license_server.py"), encoding="utf-8").read()

MAX_ANCHORS = 6  # _nearby_anchor_towns is capped at this


def _count(name):
    m = re.search(name + r"\s*=\s*\[(.*?)\n\s*\]", SRC, re.S)
    assert m, f"{name} not found"
    return len(re.findall(r'^\s*f"', m.group(1), re.M))


class QueryBudgetTests(unittest.TestCase):
    def test_a_typical_scan_stays_within_about_a_dozen_searches(self):
        """Typical = the known-portal read found something, so only the
        'always' queries run. This is the number that has to fit inside a
        free tier."""
        typical = _count("center_queries_always") + \
            MAX_ANCHORS * _count("anchor_queries_always")
        self.assertLessEqual(typical, 14, f"typical scan costs {typical} searches")

    def test_the_worst_case_stays_bounded(self):
        worst = (_count("center_queries_always") + _count("center_queries_generic")
                 + MAX_ANCHORS * (_count("anchor_queries_always")
                                  + _count("anchor_queries_generic")))
        self.assertLessEqual(worst, 24, f"worst-case scan costs {worst} searches")

    def test_anchors_stay_cheap(self):
        """The portal directory grew from ~750 agencies to 4,400+, so most
        anchor towns are now read directly. Anchors multiply by six, so an
        extra query here costs six."""
        self.assertLessEqual(_count("anchor_queries_always"), 2)
        self.assertLessEqual(_count("anchor_queries_generic"), 1)


class AggregatorPackingTests(unittest.TestCase):
    """Eight separate site: searches asked one question eight times. Every
    engine used here supports OR-ed site: filters."""

    def test_every_aggregator_is_still_covered(self):
        packed = " ".join(ls._agg_sites(i) for i in range(len(ls._AGG_SITES)))
        for domain in ("bidnetdirect.com", "demandstar.com", "planetbids.com",
                       "publicpurchase.com", "questcdn.com", "opengov.com",
                       "bonfirehub.com", "bidexpress.com", "bidsearch.com"):
            self.assertIn("site:" + domain, packed,
                          f"{domain} lost in the packing")

    def test_they_fit_in_two_queries(self):
        self.assertEqual(len(ls._AGG_SITES), 2)

    def test_a_state_portal_rides_along_instead_of_costing_a_query(self):
        self.assertIn("site:missouribuys.mo.gov", ls._agg_sites(1, "MO"))

    def test_other_states_do_not_get_missouris_portal(self):
        self.assertNotIn("missouribuys", ls._agg_sites(1, "KS"))
        self.assertNotIn("missouribuys", ls._agg_sites(0, "MO"))

    def test_no_state_is_not_an_error(self):
        self.assertTrue(ls._agg_sites(1).startswith("site:"))

    def test_the_filter_is_or_ed_not_space_separated(self):
        """Space-separated site: terms are an AND in most engines, which
        matches nothing."""
        self.assertIn(" OR ", ls._agg_sites(0))


if __name__ == "__main__":
    unittest.main()
