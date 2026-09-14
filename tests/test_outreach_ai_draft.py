"""What must hold even once a model is writing the prose.

outreach_draft.py's whole design is "a person writes the email" -- the
guards (location check, live agency count, do-not-contact) exist regardless
of who or what drafts the words. Handing the writing to a model does not
retire any of them: a do-not-contact row must still never be drafted, a
misresolved location must still hold the row, and a prospect with nothing
readable on their site must be held rather than drafted from thin air.

The model itself is stubbed throughout -- these tests are about what reaches
it and what happens to what it returns, not about prose quality.
"""
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools import outreach_ai_draft as A  # noqa: E402

FIELDS = ["slug", "company", "greeting", "email", "city", "state", "intro",
          "status", "sent_date"]


def row(slug, status="ready", city="Columbia", state="MO"):
    return {"slug": slug, "company": slug.title(), "greeting": "team",
            "email": f"{slug}@example.com", "city": city, "state": state,
            "intro": "", "status": status, "sent_date": ""}


def write_csv(rows):
    path = os.path.join(tempfile.mkdtemp(), "prospects.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    return path


class _Block:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _Response:
    def __init__(self, text):
        self.content = [_Block(text)]


class FakeClient:
    """Records every call instead of reaching the network."""
    def __init__(self, text="Subject: Hello\n\nShort body."):
        self.calls = []
        self._text = text
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _Response(self._text)


class ParseResponseTests(unittest.TestCase):
    def test_splits_subject_and_body(self):
        subject, body = A.parse_response("Subject: Hi there\n\nFirst line.\nSecond.")
        self.assertEqual(subject, "Hi there")
        self.assertEqual(body, "First line.\nSecond.")

    def test_missing_subject_line_is_not_fatal(self):
        subject, body = A.parse_response("Just a body, no subject line.")
        self.assertIn("write one", subject)
        self.assertEqual(body, "Just a body, no subject line.")


class FactsBlockTests(unittest.TestCase):
    def test_only_supplied_facts_appear(self):
        r = row("acme")
        researched = {"founded": 1968, "founded_note": "Founded in 1968",
                      "specialties": ["slip-formed barrier"],
                      "signals": {"scale": ["runs its own trucking fleet"]}}
        block = A.facts_block(r, 51, 12.0, researched)
        self.assertIn("51 bid-posting agencies", block)
        self.assertIn("12.0 miles away", block)
        self.assertIn("1968", block)
        self.assertIn("slip-formed barrier", block)
        self.assertIn("trucking fleet", block)

    def test_absent_facts_are_not_invented(self):
        r = row("acme")
        block = A.facts_block(r, 40, None, {"founded": None, "specialties": [],
                                            "signals": {}})
        self.assertNotIn("founded", block)
        self.assertNotIn("miles away", block)


class DraftEmailTests(unittest.TestCase):
    def test_prompt_carries_the_verified_facts_and_link(self):
        client = FakeClient()
        r = row("acme")
        A.draft_email(client, r, 51, 12.0, {"founded": None, "specialties": [],
                                            "signals": {}})
        prompt = client.calls[0]["messages"][0]["content"]
        self.assertIn("51 bid-posting agencies", prompt)
        self.assertIn("/go/acme", prompt)
        self.assertEqual(client.calls[0]["model"], A.MODEL)

    def test_returns_the_parsed_subject_and_body(self):
        client = FakeClient("Subject: Quick note\n\nBody text here.")
        r = row("acme")
        subject, body = A.draft_email(
            client, r, 51, None, {"founded": None, "specialties": [], "signals": {}})
        self.assertEqual(subject, "Quick note")
        self.assertEqual(body, "Body text here.")


class MainGuardTests(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()
        self._patches = {}
        for name, value in {
            "signature_problem": lambda: "",
            "build_signature": lambda: "Josh\n123 Main St",
            "_client": lambda: FakeClient(),
            "_location_evidence": lambda r: ("ok", ""),
            "coverage": lambda city, state: {"ok": True, "agencies": 51,
                                             "nearest_mi": 5.0,
                                             "nearest": [f"{city}, {state}"]},
            "_looks_like_the_right_town": lambda data, city: True,
            "research": lambda r: {"founded": None, "specialties": [], "signals": {}},
        }.items():
            self._patches[name] = getattr(A, name)
            setattr(A, name, value)

    def tearDown(self):
        for name, original in self._patches.items():
            setattr(A, name, original)

    def run_main(self, argv):
        old_argv = sys.argv
        sys.argv = ["outreach_ai_draft.py"] + argv
        try:
            return A.main()
        finally:
            sys.argv = old_argv

    def test_do_not_contact_is_never_drafted(self):
        A.PROSPECTS = write_csv([row("blocked", status="unsubscribed")])
        called = []
        A.research = lambda r: called.append(r) or {"founded": None,
                                                     "specialties": [], "signals": {}}
        self.run_main(["--dest", self.dest])
        self.assertEqual(called, [])
        self.assertEqual(os.listdir(self.dest), [])

    def test_missing_api_key_stops_before_any_file(self):
        A.PROSPECTS = write_csv([row("acme")])
        A._client = lambda: None
        rc = self.run_main(["--dest", self.dest])
        self.assertEqual(rc, 2)
        self.assertEqual(os.listdir(self.dest), [])

    def test_missing_signature_stops_before_any_file(self):
        A.PROSPECTS = write_csv([row("acme")])
        A.signature_problem = lambda: "not set up"
        rc = self.run_main(["--dest", self.dest])
        self.assertEqual(rc, 2)
        self.assertEqual(os.listdir(self.dest), [])

    def test_a_prospect_with_nothing_readable_is_held_not_drafted(self):
        A.PROSPECTS = write_csv([row("acme")])
        A.research = lambda r: {"problem": "nothing readable at acme.com"}
        self.run_main(["--dest", self.dest])
        self.assertEqual(os.listdir(self.dest), [])

    def test_misresolved_location_is_held_not_drafted(self):
        A.PROSPECTS = write_csv([row("acme")])
        A._looks_like_the_right_town = lambda data, city: False
        self.run_main(["--dest", self.dest])
        self.assertEqual(os.listdir(self.dest), [])

    def test_thin_agency_count_is_held_not_drafted(self):
        A.PROSPECTS = write_csv([row("acme")])
        A.coverage = lambda city, state: {"ok": True, "agencies": 5, "nearest_mi": 1.0}
        self.run_main(["--dest", self.dest])
        self.assertEqual(os.listdir(self.dest), [])

    def test_a_clean_prospect_gets_a_draft_file_with_the_real_signature(self):
        A.PROSPECTS = write_csv([row("acme")])
        rc = self.run_main(["--dest", self.dest])
        self.assertEqual(rc, 0)
        files = os.listdir(self.dest)
        self.assertEqual(files, ["acme.txt"])
        with open(os.path.join(self.dest, "acme.txt"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("acme@example.com", text)
        self.assertIn("Josh\n123 Main St", text)


if __name__ == "__main__":
    unittest.main()
