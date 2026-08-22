"""Structural guards on the frontend that Python can actually check.

There is no JS test runner in this project, so these assert the wiring is
present rather than exercising it. They exist because each one encodes a bug
that already shipped once and would be silent if it came back.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "curbcall_netlify_v4", "app.html")
SW = os.path.join(ROOT, "curbcall_netlify_v4", "sw.js")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class MapSizingTests(unittest.TestCase):
    """The map rendered blank intermittently: Leaflet built its tile grid
    while the container was still zero-height, requested nothing, and raised
    no error -- so the tile-retry path never heard about it. Timers alone are
    a guess about when layout finishes; the observer is the actual event."""

    def setUp(self):
        self.app = _read(APP)

    def test_map_settle_observes_the_container(self):
        body = self.app[self.app.index("function mapSettle("):]
        self.assertIn("observeMapSize", body[:400])

    def test_the_observer_ignores_a_zero_sized_container(self):
        """invalidateSize() on a hidden container just re-caches the same
        useless grid, and would consume the one event that mattered."""
        body = self.app[self.app.index("function observeMapSize("):]
        self.assertIn("if(!w||!h)return;", body[:900])

    def test_the_observer_is_attached_only_once_per_map(self):
        self.assertIn("_mapSizeObserved", self.app)

    def test_a_browser_without_resizeobserver_still_loads(self):
        body = self.app[self.app.index("function observeMapSize("):]
        self.assertIn('typeof ResizeObserver==="undefined"', body[:400])


class CompanyProfileSyncTests(unittest.TestCase):
    """Account fields synced through Supabase but the profile photo did not:
    avatar_url was added to the table and to the UI, and to neither the push
    nor the pull. It lived in localStorage on the device that uploaded it and
    appeared nowhere else. One shared field list is what stops that drifting
    again."""

    def setUp(self):
        self.app = _read(APP)

    def test_push_and_pull_share_one_field_list(self):
        self.assertIn("const COMPANY_FIELDS=", self.app)

    def test_the_avatar_is_in_that_list(self):
        block = self.app[self.app.index("const COMPANY_FIELDS="):]
        self.assertIn("avatar_url", block[:200])

    def _fn(self, name):
        """The source of one function, to the start of the next one.

        Slicing at the first closing brace broke as soon as the function grew
        an early-return guard -- the test should follow the function, not its
        first statement."""
        start = self.app.index(f"async function {name}(")
        after = self.app.find("\nasync function ", start + 1)
        return self.app[start:after if after != -1 else start + 4000]

    def test_the_push_builds_its_row_from_the_list(self):
        self.assertIn("COMPANY_FIELDS", self._fn("pushCompanyProfile"))

    def test_a_failed_push_is_not_swallowed(self):
        """A profile that never reached the server looked identical to one
        that did -- which is what made an empty table so hard to diagnose."""
        body = self._fn("pushCompanyProfile")
        self.assertIn("toast(", body)
        self.assertNotIn("catch(e){}", body)

    def test_the_pull_does_not_gate_the_whole_profile_on_the_company_name(self):
        """Someone with a photo and a contact but no company name had their
        stored row ignored, then overwritten with the empty local copy."""
        body = self._fn("syncPullCompanyProfile")
        self.assertNotIn("data&&data.name", body)
        self.assertIn("COMPANY_FIELDS.some", body)


class DiagnosticsGatingTests(unittest.TestCase):
    """Diagnostics is admin-only. /health is unauthenticated by design, so the
    card must not be the thing that hands a contractor the server's scan
    history -- and its buttons must not exist for them either."""

    def setUp(self):
        self.app = _read(APP)

    def test_the_card_only_renders_for_an_admin(self):
        self.assertIn("${isAdmin?renderDiagnostics():\"\"}", self.app)

    def test_the_health_check_only_runs_for_an_admin(self):
        self.assertIn("if(isAdmin)loadHealth();", self.app)

    def test_its_buttons_are_wired_defensively(self):
        """Unguarded getElementById on an absent card throws, which would
        abort the rest of renderAccount and leave support and sign-out dead."""
        self.assertIn("if(diagRefresh)", self.app)
        self.assertIn("if(diagCopy)", self.app)

    def test_the_admin_token_upgrades_the_health_request(self):
        self.assertIn('"X-Admin-Token":tok', self.app)


class ServiceWorkerTests(unittest.TestCase):
    def setUp(self):
        self.sw = _read(SW)
        self.app_dir = os.path.dirname(APP)

    def test_every_shell_file_actually_exists(self):
        """SHELL_FILES is installed with cache.addAll(), which is atomic: one
        404 throws away the entire shell cache. The call is wrapped in
        .catch(() => {}), so it fails silently and offline mode simply stops
        working. Deleting admin.html without updating this list did exactly
        that."""
        block = re.search(r"const SHELL_FILES = \[(.*?)\];", self.sw, re.S).group(1)
        files = re.findall(r'"([^"]+)"', block)
        self.assertTrue(files, "SHELL_FILES should not be empty")
        for f in files:
            self.assertTrue(os.path.exists(os.path.join(self.app_dir, f)),
                            f"{f} is cached by the service worker but does not exist")

    def test_shell_and_asset_caches_share_a_version(self):
        shell = re.search(r'SHELL_CACHE = "curbcall-shell-(v\d+)"', self.sw).group(1)
        asset = re.search(r'ASSET_CACHE = "curbcall-assets-(v\d+)"', self.sw).group(1)
        self.assertEqual(shell, asset)

    def test_the_deleted_admin_console_is_not_referenced(self):
        self.assertNotIn("admin.html", self.sw)


if __name__ == "__main__":
    unittest.main()
