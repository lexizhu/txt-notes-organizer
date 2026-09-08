"""Tests for the click-to-open local tool launcher."""

import tempfile
import unittest
from pathlib import Path

from launch_tool import launch_tool, service_is_ready, service_responds, start_local_service


class FakeProcess:
	def __init__(self, return_code=None) -> None:
		self.return_code = return_code

	def poll(self):
		return self.return_code


class LaunchToolTests(unittest.TestCase):
	def test_reuses_ready_service_without_starting_another(self) -> None:
		started_ports = []
		opened_urls = []

		started = launch_tool(
			ready_checker=lambda url: True,
			starter=lambda port: started_ports.append(port),
			browser_opener=lambda url: not opened_urls.append(url),
		)

		self.assertFalse(started)
		self.assertEqual(started_ports, [])
		self.assertEqual(opened_urls, ["http://localhost:8765/"])

	def test_starts_waits_until_ready_then_opens_browser(self) -> None:
		checks = iter([False, False, True])
		started_ports = []
		opened_urls = []
		clock = iter([0.0, 0.0, 0.1])

		started = launch_tool(
			ready_checker=lambda url: next(checks),
			starter=lambda port: started_ports.append(port) or FakeProcess(),
			browser_opener=lambda url: not opened_urls.append(url),
			sleep=lambda seconds: None,
			monotonic=lambda: next(clock),
		)

		self.assertTrue(started)
		self.assertEqual(started_ports, [8765])
		self.assertEqual(opened_urls, ["http://localhost:8765/"])

	def test_reports_process_exit_before_readiness(self) -> None:
		with self.assertRaisesRegex(RuntimeError, "启动失败"):
			launch_tool(
				ready_checker=lambda url: False,
				starter=lambda port: FakeProcess(return_code=1),
				browser_opener=lambda url: True,
				sleep=lambda seconds: None,
				monotonic=lambda: 0.0,
			)

	def test_reports_startup_timeout_without_opening_browser(self) -> None:
		opened_urls = []
		clock = iter([0.0, 0.0, 1.1])

		with self.assertRaisesRegex(RuntimeError, "启动超时"):
			launch_tool(
				startup_timeout_seconds=1.0,
				ready_checker=lambda url: False,
				starter=lambda port: FakeProcess(),
				browser_opener=lambda url: not opened_urls.append(url),
				sleep=lambda seconds: None,
				monotonic=lambda: next(clock),
			)

		self.assertEqual(opened_urls, [])

	def test_health_check_rejects_unrelated_or_missing_service(self) -> None:
		self.assertFalse(service_is_ready("http://127.0.0.1:1/", timeout_seconds=0.01))
		self.assertFalse(service_responds("http://127.0.0.1:1/", timeout_seconds=0.01))

	def test_rejects_a_note_service_from_another_project_directory(self) -> None:
		with self.assertRaisesRegex(
			RuntimeError,
			"另一份笔记工具实例",
		):
			launch_tool(
				ready_checker=lambda url: False,
				responds_checker=lambda url: True,
				starter=lambda port: self.fail("must not start over another instance"),
				browser_opener=lambda url: self.fail("must not open another instance"),
			)

	def test_background_command_is_fixed_and_logs_to_requested_path(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			server_script = root / "fake_server.py"
			server_script.write_text("print('started')\n", encoding="utf-8")
			log_path = root / "service.log"
			import launch_tool as launcher

			original_script = launcher.SERVER_SCRIPT_PATH
			original_project = launcher.PROJECT_DIR
			try:
				launcher.SERVER_SCRIPT_PATH = server_script
				launcher.PROJECT_DIR = root
				process = start_local_service(
					43210,
					python_executable=__import__("sys").executable,
					log_path=log_path,
				)
				self.assertEqual(process.wait(timeout=2), 0)
			finally:
				launcher.SERVER_SCRIPT_PATH = original_script
				launcher.PROJECT_DIR = original_project

			self.assertEqual(log_path.read_text(encoding="utf-8"), "started\n")


if __name__ == "__main__":
	unittest.main()