"""Tests for the one-command review pipeline."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from review import run_review_pipeline


class FakeRunner:
	def __init__(self, return_codes: list[int]) -> None:
		self.return_codes = return_codes
		self.commands: list[list[str]] = []

	def __call__(self, command, **kwargs):
		self.commands.append(command)
		return subprocess.CompletedProcess(command, self.return_codes[len(self.commands) - 1])


class ReviewPipelineTests(unittest.TestCase):
	def test_runs_organize_then_build(self) -> None:
		runner = FakeRunner([0, 0, 0])

		success = run_review_pipeline(runner=runner, python_executable="python-test")

		self.assertTrue(success)
		self.assertTrue(runner.commands[0][1].endswith("watcher.py"))
		self.assertEqual(runner.commands[0][2], "--once")
		self.assertTrue(runner.commands[1][1].endswith("organize.py"))
		self.assertTrue(runner.commands[2][1].endswith("build_html.py"))

	def test_stops_when_organize_fails(self) -> None:
		runner = FakeRunner([2])

		success = run_review_pipeline(runner=runner, python_executable="python-test")

		self.assertFalse(success)
		self.assertEqual(len(runner.commands), 1)

	def test_cancel_marker_stops_before_next_stage_and_html_build(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			cancel_file = Path(directory) / "cancel"

			def runner(command, **kwargs):
				cancel_file.touch()
				return subprocess.CompletedProcess(command, 0)

			with patch.dict("os.environ", {"MY_NOTES_CANCEL_FILE": str(cancel_file)}):
				success = run_review_pipeline(runner=runner, python_executable="python-test")

		self.assertFalse(success)


if __name__ == "__main__":
	unittest.main()