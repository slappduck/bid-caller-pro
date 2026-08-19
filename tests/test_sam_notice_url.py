"""Tests for _sam_notice_url.

A SAM.gov bid arrived with full detail -- title, scope, deadline, contact --
and no link, because the API's uiLink field was absent. Every notice has a
noticeId and sam.gov's public URL for one is stable, so there is no reason
for the card to be a dead end.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class SamNoticeUrlTests(unittest.TestCase):
    def test_a_notice_id_becomes_its_public_page(self):
        self.assertEqual(ls._sam_notice_url("a1b2c3d4e5f67890"),
                         "https://sam.gov/opp/a1b2c3d4e5f67890/view")

    def test_missing_or_empty_yields_no_url_rather_than_a_broken_one(self):
        for bad in ("", None, "   "):
            self.assertEqual(ls._sam_notice_url(bad), "")

    def test_a_non_hex_value_is_refused(self):
        """Building a URL out of whatever turned up would produce a
        plausible-looking 404, which is worse than no link."""
        self.assertEqual(ls._sam_notice_url("not an id"), "")
        self.assertEqual(ls._sam_notice_url("../../etc/passwd"), "")

    def test_a_short_value_is_refused(self):
        self.assertEqual(ls._sam_notice_url("abc"), "")


class NormalizeOppUsesTheFallbackTests(unittest.TestCase):
    def _opp(self, **kw):
        base = {"title": "Pedestrian And Bicycle Paths", "active": "Yes",
                "noticeId": "0123456789abcdef", "responseDeadLine": "2027-08-19"}
        base.update(kw)
        return base

    def test_ui_link_is_preferred_when_present(self):
        bid, _, _ = ls._normalize_opp(self._opp(uiLink="https://sam.gov/opp/x/view"))
        self.assertEqual(bid["url"], "https://sam.gov/opp/x/view")

    def test_the_notice_id_fills_in_when_ui_link_is_missing(self):
        bid, _, _ = ls._normalize_opp(self._opp())
        self.assertEqual(bid["url"],
                         "https://sam.gov/opp/0123456789abcdef/view")

    def test_no_id_and_no_link_leaves_the_url_empty(self):
        bid, _, _ = ls._normalize_opp(self._opp(noticeId=""))
        self.assertEqual(bid["url"], "")


if __name__ == "__main__":
    unittest.main()
