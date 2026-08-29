import os
import tempfile
import unittest
from pathlib import Path

from idea_migrate.completion import PathCompleter, path_candidates


class CompletionTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        (self.base / "WebstormProjects").mkdir()
        (self.base / "WebstormProjectsArchive").mkdir()
        (self.base / "IdeaProjects").mkdir()
        (self.base / "notes.txt").write_text("x", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()


class TestPathCandidates(CompletionTestCase):
    def test_completes_a_partial_final_component(self):
        found = path_candidates(f"{self.base}/Webst")
        self.assertEqual(
            sorted(found),
            [
                f"{self.base}/WebstormProjects/",
                f"{self.base}/WebstormProjectsArchive/",
            ],
        )

    def test_completions_end_with_a_separator(self):
        for candidate in path_candidates(f"{self.base}/Idea"):
            self.assertTrue(candidate.endswith("/"))

    def test_files_are_excluded_when_only_dirs(self):
        found = path_candidates(f"{self.base}/not", only_dirs=True)
        self.assertEqual(found, [])

    def test_files_are_included_when_not_only_dirs(self):
        found = path_candidates(f"{self.base}/not", only_dirs=False)
        self.assertEqual(found, [f"{self.base}/notes.txt"])

    def test_matching_is_case_insensitive(self):
        found = path_candidates(f"{self.base}/webst")
        self.assertEqual(len(found), 2)

    def test_empty_component_lists_directory_contents(self):
        found = path_candidates(f"{self.base}/")
        self.assertEqual(len(found), 3)

    def test_tilde_prefix_is_preserved_in_output(self):
        home = Path.home()
        found = path_candidates("~/")
        self.assertTrue(all(c.startswith("~/") for c in found))
        # And they correspond to real entries in the home directory.
        names = {c[len("~/"):].rstrip("/") for c in found}
        actual = {p.name for p in home.iterdir() if p.is_dir()}
        self.assertTrue(names.issubset(actual))

    def test_nonexistent_directory_returns_empty(self):
        self.assertEqual(path_candidates(f"{self.base}/absent/xyz"), [])


class TestPathCompleter(CompletionTestCase):
    def test_state_iterates_then_returns_none(self):
        completer = PathCompleter()
        first = completer.complete(f"{self.base}/Webst", 0)
        second = completer.complete(f"{self.base}/Webst", 1)
        third = completer.complete(f"{self.base}/Webst", 2)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertIsNone(third)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
