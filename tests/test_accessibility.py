"""The accessibility failures an axe-core run actually found, pinned.

Four real ones, none of which produced any visible symptom:

  * The whole app disabled pinch-zoom (maximum-scale=1.0, user-scalable=no).
    axe rates that critical. It fails WCAG 1.4.4 and it is a genuine barrier
    for anyone who enlarges text to read. The usual justification -- iOS
    zooming in when a form field is focused -- only applies below 16px, and
    the app's inputs are 16px, so the lock was buying nothing at all.

  * The coverage radius control on the marketing page was a <select> with no
    accessible name. A screen reader announced it as an unlabelled combo box
    on the one screen a prospect uses before paying.

  * --text3 measured 2.71:1 against the pricing panel and 3.28:1 against the
    page background, against a 4.5:1 requirement. It is used for the footer,
    the plan tags and the "Last updated" line on both policies.

  * The support address in the footer was a link distinguished from body text
    only by colour.

The full audit needs a browser and a downloaded axe-core (tools/a11y_audit.js).
These are the cheap deterministic version: they compute real contrast ratios
rather than matching strings, and they run everywhere.
"""
import glob
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, os.pardir, "curbcall_netlify_v4")


def pages():
    return sorted(glob.glob(os.path.join(WEB, "*.html"))
                  + glob.glob(os.path.join(WEB, "legal", "*.html")))


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _luminance(hex_colour):
    h = hex_colour.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    parts = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    parts = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
             for c in parts]
    return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]


def contrast(a, b):
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


class ZoomIsNotDisabledTests(unittest.TestCase):
    """WCAG 1.4.4. The critical one, and it was on every screen of the app."""

    def test_no_page_locks_the_viewport(self):
        for path in pages():
            with self.subTest(page=os.path.basename(path)):
                for tag in re.findall(r'<meta name="viewport"[^>]*>', read(path)):
                    self.assertNotIn("user-scalable=no", tag)
                    self.assertNotRegex(tag, r"maximum-scale\s*=\s*1")

    def test_form_fields_are_at_least_16px(self):
        """The reason the lock gets added: iOS zooms in on focus below 16px.
        Keeping fields at 16px is what makes removing it free."""
        css = read(os.path.join(WEB, "app.html"))
        m = re.search(r"\.filter-row input,\.filter-row select\{[^}]*"
                      r"font-size:([^;]+);", css)
        self.assertIsNotNone(m, "the filter row's sizing moved")
        self.assertEqual(m.group(1).strip(), "1rem")


class ContrastTests(unittest.TestCase):
    """Computed, not asserted by eye."""

    MIN = 4.5

    def tokens(self, html):
        found = {}
        for name in ("bg", "panel", "text", "text2", "text3"):
            m = re.search(r"--%s:\s*(#[0-9a-fA-F]{3,6})" % name, html)
            if m:
                found[name] = m.group(1)
        return found

    def test_every_text_token_is_readable_on_every_surface(self):
        checked = 0
        for path in pages():
            t = self.tokens(read(path))
            surfaces = [t[k] for k in ("bg", "panel") if k in t]
            for name in ("text", "text2", "text3"):
                if name not in t:
                    continue
                for surface in surfaces:
                    with self.subTest(page=os.path.basename(path),
                                      token=name, on=surface):
                        ratio = contrast(t[name], surface)
                        checked += 1
                        self.assertGreaterEqual(
                            round(ratio, 2), self.MIN,
                            "--%s (%s) on %s is %.2f:1"
                            % (name, t[name], surface, ratio))
        self.assertGreater(checked, 0, "no colour tokens were found to check")

    def test_the_hierarchy_survived_the_fix(self):
        """Lifting --text3 for contrast must not make it match --text2, or
        the three-level type hierarchy collapses into two."""
        for path in pages():
            t = self.tokens(read(path))
            if "text2" in t and "text3" in t:
                with self.subTest(page=os.path.basename(path)):
                    self.assertNotEqual(t["text2"], t["text3"])


class ControlsAreNamedTests(unittest.TestCase):
    def test_every_select_has_an_accessible_name(self):
        for path in pages():
            html = read(path)
            for tag in re.findall(r"<select\b[^>]*>", html):
                ident = re.search(r'id="([^"]+)"', tag)
                labelled = ("aria-label" in tag or "aria-labelledby" in tag
                            or (ident and 'for="%s"' % ident.group(1) in html))
                with self.subTest(page=os.path.basename(path),
                                  select=tag[:70]):
                    self.assertTrue(labelled,
                                    "select has no accessible name: " + tag[:90])


class LinksAreDistinguishableTests(unittest.TestCase):
    """WCAG 1.4.1: colour alone cannot be the only signal."""

    def test_the_support_link_in_body_text_is_underlined(self):
        html = read(os.path.join(WEB, "index.html"))
        m = re.search(r'<a href="mailto:support@curbcallpro\.com"'
                      r'\s+style="([^"]*)"', html)
        self.assertIsNotNone(m, "the inline support link moved")
        self.assertIn("underline", m.group(1))
