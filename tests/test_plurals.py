"""Counting things in words a person would use.

"3 bid(s)" is how a form letter counts. The app always knows the number, so
it can pick the word -- and the landing page's hero counter, which ticks
0,1,2..5 on a loop under a hard-coded "bids", announced "1 bids" to every
visitor on every pass.

plural() is run here for real under node rather than eyeballed, because the
interesting cases are the ones nobody looks at: zero, and a negative.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, os.pardir, "curbcall_netlify_v4", "app.html")
INDEX = os.path.join(HERE, os.pardir, "curbcall_netlify_v4", "index.html")


class PluralHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not available")
        with open(APP, encoding="utf-8") as f:
            src = f.read()
        i = src.index("function plural(n,word,plural_){")
        cls.fn = src[i:src.index("}", src.index("return", i)) + 1]

    def _call(self, *args):
        js = "%s\nconsole.log(JSON.stringify(plural(%s)));" % (
            self.fn, ",".join(json.dumps(a) for a in args))
        out = subprocess.run([shutil.which("node"), "-e", js],
                             capture_output=True, text=True, timeout=60)
        if out.returncode:
            self.fail(out.stderr[-800:])
        return json.loads(out.stdout.strip())

    def test_one_is_singular(self):
        self.assertEqual(self._call(1, "bid"), "1 bid")

    def test_many_is_plural(self):
        self.assertEqual(self._call(3, "bid"), "3 bids")

    def test_zero_is_plural(self):
        """"0 bid" is wrong in English."""
        self.assertEqual(self._call(0, "bid"), "0 bids")

    def test_an_irregular_plural_can_be_given(self):
        self.assertEqual(self._call(2, "city", "cities"), "2 cities")
        self.assertEqual(self._call(1, "city", "cities"), "1 city")

    def test_minus_one_is_singular_too(self):
        self.assertEqual(self._call(-1, "bid"), "-1 bid")


class NoFormLetterCountsTests(unittest.TestCase):
    def test_no_user_facing_s_parentheses_remain(self):
        with open(APP, encoding="utf-8") as f:
            src = re.sub(r"//.*$", "", f.read(), flags=re.M)
        leftovers = re.findall(r"\b(?:bid|project|town|lead|recipient)\(s\)", src)
        self.assertEqual(leftovers, [], f"still counting like a form: {leftovers}")

    def test_the_landing_page_counter_picks_its_own_word(self):
        """It ticks 0..5 on a loop, so a fixed word is wrong 1 time in 6."""
        with open(INDEX, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('id="mnw"', src)
        self.assertIn('nWord.textContent=(v===1?"bid":"bids")', src)


if __name__ == "__main__":
    unittest.main()


class ExportFilenameTests(unittest.TestCase):
    """A file the customer finds in Downloads next week.

    Date.now() produced curbcall_bids_1788409740206.csv, which sorts oddly
    next to its siblings and says nothing about which day the list is from.
    """

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest("node not available")
        with open(APP, encoding="utf-8") as f:
            cls.src = f.read()

    def test_the_name_carries_a_readable_date(self):
        i = self.src.index("function stampedName(base,ext){")
        # Brace-match rather than "find the next }". The body contains a
        # template literal, so the first closing brace belongs to
        # ${d.getFullYear()} and slicing there yields an unterminated string.
        depth, j = 0, self.src.index("{", i)
        for j in range(j, len(self.src)):
            if self.src[j] == "{":
                depth += 1
            elif self.src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
        fn = self.src[i:j + 1]
        js = fn + '\nconsole.log(stampedName("curbcall-bids","csv"));'
        out = subprocess.run([shutil.which("node"), "-e", js],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr[-500:])
        name = out.stdout.strip()
        self.assertRegex(name, r"^curbcall-bids-\d{4}-\d{2}-\d{2}\.csv$")

    def test_no_export_is_named_with_a_raw_timestamp(self):
        self.assertNotIn('+Date.now()+".csv"', self.src)
