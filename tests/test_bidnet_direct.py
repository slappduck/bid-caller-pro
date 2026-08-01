"""Offline tests for the BidNet Direct direct-search integration in
license_server.py. Network calls are mocked -- these check the URL-building,
state-code lookup, and href-parsing logic without hitting the real site
(there's a separate live smoke test run manually during development; see
the session notes -- this suite must stay runnable with no network)."""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls

_SAMPLE_HTML = """
<html><body>
<a class="solicitation-link" href="/missouri/solicitations/open-bids/statewide/Sidewalk-Project/444100000001?origin=0&amp;target=view">Sidewalk Project</a>
<a class="solicitation-link" href="/missouri/solicitations/open-bids/statewide/Curb-Ramp-Project/444100000002?origin=0&amp;target=view">Curb Ramp Project</a>
<a href="/some/other/link">Not a solicitation</a>
</body></html>
"""


class BidnetLocationCodeTests(unittest.TestCase):
    def test_known_state_has_a_code(self):
        self.assertIn("MO", ls.BIDNET_LOCATION_CODES)
        self.assertEqual(ls.BIDNET_LOCATION_CODES["MO"], 169)

    def test_unknown_state_returns_no_results_without_any_network_call(self):
        with patch("license_server.urllib.request.urlopen") as mock_open:
            result = ls._bidnet_direct_urls("sidewalk", "ZZ")
            mock_open.assert_not_called()
        self.assertEqual(result, [])


class BidnetDirectUrlsTests(unittest.TestCase):
    def _mock_response(self, html):
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = html.encode("utf-8")
        return cm

    def test_parses_detail_links_from_html(self):
        with patch("license_server.urllib.request.urlopen",
                   return_value=self._mock_response(_SAMPLE_HTML)):
            results = ls._bidnet_direct_urls("sidewalk", "MO")
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r["url"].startswith("https://www.bidnetdirect.com/") for r in results))
        self.assertTrue(all("content" in r for r in results))

    def test_unescapes_ampersand_in_query_string(self):
        with patch("license_server.urllib.request.urlopen",
                   return_value=self._mock_response(_SAMPLE_HTML)):
            results = ls._bidnet_direct_urls("sidewalk", "MO")
        self.assertIn("&target=view", results[0]["url"])
        self.assertNotIn("&amp;", results[0]["url"])

    def test_respects_max_results_cap(self):
        many = "".join(
            f'<a class="solicitation-link" href="/missouri/solicitations/open-bids/statewide/P{i}/{i}?origin=0">P{i}</a>'
            for i in range(20)
        )
        with patch("license_server.urllib.request.urlopen",
                   return_value=self._mock_response(many)):
            results = ls._bidnet_direct_urls("sidewalk", "MO", max_results=3)
        self.assertEqual(len(results), 3)

    def test_network_failure_returns_empty_list_not_an_exception(self):
        with patch("license_server.urllib.request.urlopen", side_effect=OSError("boom")):
            result = ls._bidnet_direct_urls("sidewalk", "MO")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
