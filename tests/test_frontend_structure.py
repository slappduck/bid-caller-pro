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
