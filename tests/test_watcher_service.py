"""Tests for LaunchAgent configuration without installing it."""

import plistlib
import tempfile
import unittest
from pathlib import Path

from install_watcher import LABEL, build_launch_agent, launch_domain, write_plist


class WatcherServiceTests(unittest.TestCase):
	def test_configuration_keeps_runtime_files_in_project(self) -> None:
		project_dir = Path("/tmp/portable-notes")

		configuration = build_launch_agent(project_dir, "/usr/bin/python3")

		self.assertEqual(configuration["Label"], LABEL)
		self.assertEqual(configuration["WorkingDirectory"], str(project_dir.resolve()))
		self.assertTrue(configuration["ProgramArguments"][1].endswith("watcher.py"))
		self.assertEqual(configuration["StandardOutPath"], "/dev/null")
		self.assertTrue(configuration["StandardErrorPath"].endswith("data/watcher-error.log"))
		self.assertTrue(configuration["RunAtLoad"])
		self.assertTrue(configuration["KeepAlive"])

	def test_writes_valid_plist(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "agent.plist"
			configuration = build_launch_agent(Path(directory), "/usr/bin/python3")

			write_plist(path, configuration)

			with path.open("rb") as file:
				self.assertEqual(plistlib.load(file), configuration)

	def test_launch_domain_uses_given_user_id(self) -> None:
		self.assertEqual(launch_domain(501), "gui/501")


if __name__ == "__main__":
	unittest.main()