"""Guards on the one failure mode the license_server/ split can't catch itself.

license_server/__init__.py execs 13 files into one shared namespace instead of
importing them as real submodules, on purpose -- see that file's own
docstring for why (55 test files patch.object(ls, "_private_helper"), which
only redirects every call site correctly if there is exactly one shared
namespace, the way the original single file always had).

That design has exactly one gap nothing else notices: a new file added under
license_server/ that never gets listed in __init__.py's _FILES tuple loads
silently. No error, no warning -- the names it defines simply never reach
`license_server`, the same way an unregistered route or helper would vanish.
Conversely, if the split ever reintroduces two files defining the same
top-level name, one silently shadows the other with no error either.

Neither of these tests exercises what the package DOES -- that's the other
1,724 tests. These only check that everything on disk is actually wired in.
"""
import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import license_server as ls  # noqa: E402
from license_server import _FILES  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(_ROOT, "license_server")


def _top_level_names(path):
    """Every name a file binds at module scope: defs, classes, assignments,
    and imports -- anything that would land in the shared namespace."""
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
    return names


class EveryFileOnDiskIsRegisteredTests(unittest.TestCase):
    def test_files_tuple_matches_whats_actually_on_disk(self):
        on_disk = {f for f in os.listdir(PKG_DIR)
                   if f.endswith(".py") and f != "__init__.py"}
        self.assertEqual(
            on_disk, set(_FILES),
            "license_server/__init__.py's _FILES tuple and the .py files "
            "actually present under license_server/ have drifted apart -- "
            "a file was added or removed without updating the other.")


class AssemblyProducesTheExpectedNamespaceTests(unittest.TestCase):
    def test_the_flask_app_is_reachable(self):
        self.assertTrue(hasattr(ls, "app"))
        self.assertEqual(type(ls.app).__name__, "Flask")

    def test_every_name_each_file_defines_reaches_the_package(self):
        for fname in _FILES:
            for name in _top_level_names(os.path.join(PKG_DIR, fname)):
                self.assertTrue(
                    hasattr(ls, name),
                    f"{name!r} is defined in license_server/{fname} but "
                    "does not appear on license_server -- check _FILES and "
                    "the exec order in __init__.py")

    def test_no_two_files_define_the_same_top_level_name(self):
        """As of this split: zero. A future duplicate -- even an innocent
        one, like two files both defining a helper called `_fetch` -- would
        silently let the later file win, with no error anywhere."""
        owner = {}
        collisions = []
        for fname in _FILES:
            for name in _top_level_names(os.path.join(PKG_DIR, fname)):
                if name in owner and owner[name] != fname:
                    collisions.append((name, owner[name], fname))
                owner[name] = fname
        self.assertEqual(
            collisions, [],
            "these names are defined in more than one license_server/ file; "
            "whichever loads last silently wins")


class TheRootFileIsGeneratedNotEditedTests(unittest.TestCase):
    """The repo-root license_server.py is a build artifact, not source.

    __init__.py rewrites it on every import as the concatenation of the
    package files, so an edit made to it directly is destroyed the next time
    anything imports license_server -- silently, with no error, and with the
    working tree looking clean immediately afterwards because the rewrite
    lands before anyone looks. It is 8,635 lines and reads exactly like the
    source it used to be, so editing it is the obvious mistake to make; it
    was very nearly made during the SAM.gov endpoint fix, where every change
    happened to be applied to both copies by hand and so survived by luck.

    Checked against git rather than the working tree, for the same reason
    test_service_worker_freshness.py checks its cache version that way: by
    the time a test runs it has already imported the package, so the file on
    disk has already been repaired. Only what was COMMITTED can still be
    wrong.
    """

    def test_the_committed_root_file_matches_the_package(self):
        import subprocess
        try:
            committed = subprocess.run(
                ["git", "show", "HEAD:license_server.py"], cwd=_ROOT,
                capture_output=True, text=True, encoding="utf-8",
                timeout=30)
        except Exception as ex:                       # no git, shallow CI
            self.skipTest(f"git unavailable: {ex}")
        if committed.returncode != 0:
            self.skipTest("license_server.py not in HEAD")

        expected = "\n\n".join(
            open(os.path.join(PKG_DIR, f), encoding="utf-8").read()
            for f in _FILES)
        self.assertEqual(
            committed.stdout, expected,
            "the committed license_server.py is not the concatenation of the "
            "license_server/ files. It is GENERATED -- edit the package file "
            "that owns the code, never the root file, then re-import to "
            "regenerate it. A root-only edit is discarded on the next import.")


if __name__ == "__main__":
    unittest.main()
