"""Spanish: the landing page at /es/ built from the English one, with the
same disclosures in Spanish, and the app's Spanish word list."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import build_es_landing as B  # noqa: E402

WEB = os.path.join(ROOT, "curbcall_netlify_v4")


def read(*p):
    with open(os.path.join(*p), encoding="utf-8") as f:
        return f.read()


class SpanishLandingTests(unittest.TestCase):
    def setUp(self):
        self.es = read(WEB, "es", "index.html")

    def test_the_committed_page_is_what_the_english_one_builds(self):
        """Change index.html without updating the Spanish pairs and this fails."""
        self.assertEqual(B.build(), self.es, "es/index.html is stale: run tools/build_es_landing.py")

    def test_it_is_spanish_and_says_so(self):
        self.assertIn('<html lang="es">', self.es)
        self.assertIn('<link rel="canonical" href="https://curbcallpro.com/es/" />', self.es)
        for page in (self.es, read(WEB, "index.html")):
            self.assertIn('hreflang="es" href="https://curbcallpro.com/es/"', page)
            self.assertIn('hreflang="en" href="https://curbcallpro.com/"', page)

    def test_no_english_sentences_are_left_on_the_page(self):
        text = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>|<!--.*?-->", "", self.es)
        text = re.sub(r"<[^>]+>", "\n", text)
        english = [ln.strip() for ln in text.splitlines()
                   if re.search(r"\b(the|your|you|with|and|for)\b", ln) and "lang=" not in ln]
        self.assertEqual(english, [])

    def test_every_subscribe_button_has_the_renewal_terms_in_spanish(self):
        for m in re.finditer(r">Suscribirse [a-z]+</a>", self.es):
            near = self.es[m.end():m.end() + 400]
            self.assertIn("Se renueva automáticamente", near)
            self.assertRegex(near, r"\$\d+/(mes|año)")
            self.assertIn("Cancele", near)
        self.assertEqual(len(re.findall(r">Suscribirse [a-z]+</a>", self.es)), 2)

    def test_the_app_opens_in_spanish_from_here(self):
        self.assertIn('href="/app.html?lang=es"', self.es)
        self.assertNotRegex(self.es, r'href="/app\.html"')

    def test_the_address_works_and_is_in_the_sitemap(self):
        self.assertRegex(read(WEB, "_redirects"), r"(?m)^/es/?\s+/es/index\.html\s+200")
        self.assertIn("https://curbcallpro.com/es/", read(WEB, "sitemap.xml"))
        self.assertIn('href="/es/"', read(WEB, "index.html"))


class AppWordListTests(unittest.TestCase):
    def test_the_menu_and_the_first_steps_are_in_the_word_list(self):
        app = read(WEB, "app.js")
        for word in ("Home", "Find", "Bids", "Account", "Get started", "Price this bid", "Your bid", "Pricing"):
            self.assertRegex(app, r'"%s":"[^"]+"' % re.escape(word), word)

    def test_the_logo_is_never_translated(self):
        html = read(WEB, "app.html")
        self.assertIn('<span class="brand-name" data-no-tr>Curb<span>Call</span></span>', html)


if __name__ == "__main__":
    unittest.main()
