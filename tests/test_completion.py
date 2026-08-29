import io
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from idea_migrate.cli import main
from idea_migrate.completion import PathCompleter, path_candidates, prompt_for_path
from idea_migrate.errors import MigrateError


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

    def test_plain_files_are_never_offered(self):
        # Every prompt that completes a path is asking for a directory, so a
        # regular file is never a valid answer and is never suggested.
        self.assertEqual(path_candidates(f"{self.base}/not"), [])

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


class TestPromptForPath(unittest.TestCase):
    """How the prompt distinguishes "nobody is there" from "the user cancelled"."""

    def test_end_of_input_becomes_a_clean_message_about_the_flags(self):
        with patch("idea_migrate.completion.input", side_effect=EOFError()):
            with self.assertRaises(MigrateError) as ctx:
                prompt_for_path("Source directory: ")
        self.assertIn("--source", str(ctx.exception))

    def test_ctrl_c_propagates_instead_of_being_relabelled(self):
        """Ctrl-C is a deliberate cancel and must be reported as one.

        Converting it into the non-interactive error told a user who had just
        pressed Ctrl-C that no input was available and that they should pass
        --source and --dest - advice for a problem they did not have - and
        exited 1 rather than the 130 that means "interrupted".
        """
        with patch("idea_migrate.completion.input", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                prompt_for_path("Source directory: ")

    def test_main_turns_ctrl_c_at_the_prompt_into_a_clean_cancel(self):
        stderr = io.StringIO()
        with (
            patch("idea_migrate.completion.input", side_effect=KeyboardInterrupt()),
            redirect_stderr(stderr),
            redirect_stdout(io.StringIO()),
        ):
            code = main(["--dest", "/tmp/somewhere", "--yes"])
        self.assertEqual(code, 130)
        self.assertIn("Cancelled", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("--source", stderr.getvalue())


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
