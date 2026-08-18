"""Tests for which origins the browser is allowed to call this server from.

This is easy to get wrong in a way that looks like an outage: put the site on
a custom domain, forget the allowlist, and every page loads perfectly while
every API call fails. So the custom-domain path is pinned here.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls


class SiteOriginsTests(unittest.TestCase):
    def _origins(self, value=None):
        env = {} if value is None else {"SITE_ORIGINS": value}
        with patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop("SITE_ORIGINS", None)
            return ls._site_origins()

    @staticmethod
    def _matches(origins, candidate):
        for o in origins:
            if hasattr(o, "match"):
                if o.match(candidate):
                    return True
            elif o == candidate:
                return True
        return False

    def test_the_netlify_site_is_always_allowed(self):
        o = self._origins()
        self.assertTrue(self._matches(o, "https://curbcallpro.netlify.app"))

    def test_deploy_previews_are_allowed(self):
        o = self._origins()
        self.assertTrue(
            self._matches(o, "https://deploy-preview-12--curbcallpro.netlify.app"))

    def test_an_unrelated_origin_is_not_allowed(self):
        o = self._origins()
        self.assertFalse(self._matches(o, "https://evil.example.com"))
        # A lookalike must not slip through the regex either.
        self.assertFalse(
            self._matches(o, "https://curbcallpro.netlify.app.evil.com"))

    def test_a_custom_domain_can_be_added_without_a_deploy(self):
        o = self._origins("https://curbcallpro.com")
        self.assertTrue(self._matches(o, "https://curbcallpro.com"))
        # and the Netlify origin still works during the cutover
        self.assertTrue(self._matches(o, "https://curbcallpro.netlify.app"))

    def test_several_origins_and_stray_whitespace(self):
        o = self._origins(" https://curbcallpro.com , https://www.curbcallpro.com ")
        self.assertTrue(self._matches(o, "https://curbcallpro.com"))
        self.assertTrue(self._matches(o, "https://www.curbcallpro.com"))

    def test_a_trailing_slash_is_tolerated(self):
        """Origins never carry a trailing slash, but people paste URLs."""
        o = self._origins("https://curbcallpro.com/")
        self.assertTrue(self._matches(o, "https://curbcallpro.com"))

    def test_an_empty_setting_changes_nothing(self):
        self.assertEqual(len(self._origins("")), len(self._origins()))
        self.assertEqual(len(self._origins(" , ")), len(self._origins()))


if __name__ == "__main__":
    unittest.main()
