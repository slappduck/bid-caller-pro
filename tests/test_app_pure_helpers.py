"""Tests for app.py's pure, GUI-free helper functions.

app.py is the desktop (tkinter) app and had zero test coverage -- nothing
here caught the Est. Value bug fixed alongside this file (a manually-edited
value was written into self.saved but every display read the untouched raw
bid dict instead, so an edit looked like it silently failed everywhere
except the Active Bids tab). That bug lived in saved_field()'s logic,
inlined three different ways across _bid_card, _show_bid_detail, and the
Est. Value sort key -- now a single shared function so it only has to be
right, and be tested, once.

A real Tk widget tree needs a display this environment doesn't have, and
app.py imports tkinter unconditionally at module level (plus regional_printer,
which pulls in bs4/pypdf that aren't part of this project's own
requirements.txt). None of that is needed to exercise plain string/dict
logic like bid_id, parse_value, days_until, and saved_field, so tkinter and
regional_printer are stubbed out purely so the module can be imported --
nothing here calls into a widget.
"""
import datetime
import os
import sys
import types
import unittest


def _stub_module(name):
    return types.ModuleType(name)


def _install_stubs():
    if "tkinter" not in sys.modules:
        tk_mod = _stub_module("tkinter")
        ttk_mod = _stub_module("tkinter.ttk")
        messagebox_mod = _stub_module("tkinter.messagebox")
        tk_mod.ttk = ttk_mod
        tk_mod.messagebox = messagebox_mod
        sys.modules["tkinter"] = tk_mod
        sys.modules["tkinter.ttk"] = ttk_mod
        sys.modules["tkinter.messagebox"] = messagebox_mod
    if "regional_printer" not in sys.modules:
        sys.modules["regional_printer"] = _stub_module("regional_printer")


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_install_stubs()
import app  # noqa: E402 - must follow the stub installation above


class BidIdTests(unittest.TestCase):
    def test_same_city_title_scope_yields_the_same_id(self):
        a = app.bid_id("Springfield", {"title": "Sidewalk Repair", "scope": "ADA ramps"})
        b = app.bid_id("Springfield", {"title": "Sidewalk Repair", "scope": "ADA ramps"})
        self.assertEqual(a, b)

    def test_a_different_city_yields_a_different_id(self):
        bid = {"title": "Sidewalk Repair", "scope": "ADA ramps"}
        self.assertNotEqual(app.bid_id("Springfield", bid), app.bid_id("Aurora", bid))

    def test_missing_title_or_scope_does_not_raise(self):
        app.bid_id("Springfield", {})  # no title/scope keys at all


class ParseValueTests(unittest.TestCase):
    def test_millions(self):
        self.assertEqual(app.parse_value("$1.2M"), 1_200_000.0)

    def test_thousands(self):
        self.assertEqual(app.parse_value("$85k"), 85_000.0)

    def test_plain_number_with_commas(self):
        self.assertEqual(app.parse_value("$45,000"), 45_000.0)

    def test_blank_is_unparseable(self):
        self.assertEqual(app.parse_value(""), -1.0)

    def test_none_is_unparseable(self):
        self.assertEqual(app.parse_value(None), -1.0)

    def test_no_digits_is_unparseable(self):
        self.assertEqual(app.parse_value("call for pricing"), -1.0)


class DaysUntilTests(unittest.TestCase):
    def test_a_future_iso_date_counts_forward(self):
        future = (datetime.datetime.now().date() + datetime.timedelta(days=5)).isoformat()
        self.assertEqual(app.days_until({"deadline": future}), 5)

    def test_a_past_date_is_negative(self):
        past = (datetime.datetime.now().date() - datetime.timedelta(days=3)).isoformat()
        self.assertEqual(app.days_until({"deadline": past}), -3)

    def test_no_deadline_is_none(self):
        self.assertIsNone(app.days_until({}))

    def test_unparseable_deadline_is_none(self):
        self.assertIsNone(app.days_until({"deadline": "sometime next month"}))


class SavedFieldTests(unittest.TestCase):
    """Regression coverage for the Est. Value bug: a manual edit lives only
    in `saved`, never in the raw bid dict, so every reader has to check
    `saved` first or the edit is invisible everywhere it wasn't made."""

    def test_no_saved_record_falls_back_to_the_raw_bid(self):
        got = app.saved_field({}, "k1", "value", {"value": "$50k"})
        self.assertEqual(got, "$50k")

    def test_a_saved_override_wins_over_the_raw_bid(self):
        saved = {"k1": {"value": "$90k"}}
        got = app.saved_field(saved, "k1", "value", {"value": "$50k"})
        self.assertEqual(got, "$90k")

    def test_a_saved_record_with_no_value_still_falls_back(self):
        saved = {"k1": {"note": "call Tuesday"}}  # saved, but never edited value
        got = app.saved_field(saved, "k1", "value", {"value": "$50k"})
        self.assertEqual(got, "$50k")

    def test_neither_source_having_the_field_yields_empty(self):
        got = app.saved_field({"k1": {}}, "k1", "value", {})
        self.assertEqual(got, "")

    def test_matches_the_key_bid_id_would_produce(self):
        # _bid_card/_show_bid_detail key their saved lookups off bid_id() --
        # this pins that saved_field is actually keyed the same way callers
        # use it, not just correct in isolation.
        bid = {"title": "Curb Repair", "scope": "downtown"}
        key = app.bid_id("Ozark", bid)
        saved = {key: {"value": "$12,000"}}
        self.assertEqual(app.saved_field(saved, key, "value", bid), "$12,000")


if __name__ == "__main__":
    unittest.main()
