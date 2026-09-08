"""Deterministic worker-lifecycle regressions using event-controlled diagnostics."""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

import test_local_server as service_tests

from local_server import DiagnosticStore, OrganizeJobManager, ReprocessJobManager, ReprocessPlanManager


class DelayedDiagnosticStore(DiagnosticStore):
	def __init__(self, path: Path, *, fail: bool = False) -> None:
		super().__init__(path)
		self.entered = threading.Event()
		self.release = threading.Event()
		self.finished = threading.Event()
		self.fail = fail

	def record(self, job) -> None:
		self.entered.set()
		try:
			if not self.release.wait(timeout=5):
				raise RuntimeError("Test diagnostic gate was not released")
			if self.fail:
				raise OSError("Injected diagnostic write failure")
			super().record(job)
		finally:
			self.finished.set()


class JobWorkerTests(unittest.TestCase):
	def setUp(self) -> None:
		self.temporary = tempfile.TemporaryDirectory(prefix="txt-notes-worker-test-")
		self.root = Path(self.temporary.name)
		self.stores = []
		self.managers = []

	def tearDown(self) -> None:
		for store in self.stores:
			store.release.set()
		for manager in self.managers:
			manager.wait_for_workers(timeout=5)
		self.temporary.cleanup()

	def make_manager(self, kind, store):
		if kind == "organize":
			manager = OrganizeJobManager(self.root / "organize.lock", lambda report: True,
				diagnostic_store=store)
			start = manager.start
		else:
			plan_manager = Mock(spec=ReprocessPlanManager)
			counter = 0

			def resolve(payload):
				return ({"plan_id": f"reprocess-plan-v1:{counter}", "has_work": True,
					"uses_cloud": False, "components": ["analysis"]}, {"fictional-record"})

			plan_manager.resolve_unlocked.side_effect = resolve
			manager = ReprocessJobManager(plan_manager, self.root / "reprocess.lock",
				lambda targets, components, report, cancelled: True, diagnostic_store=store)

			def start():
				nonlocal counter
				counter += 1
				return manager.start({"plan_id": f"reprocess-plan-v1:{counter}", "confirmed": True})

		self.managers.append(manager)
		return manager, start

	def delayed_store(self, name, *, fail=False):
		store = DelayedDiagnosticStore(self.root / name, fail=fail)
		self.stores.append(store)
		return store

	def check_final_write_wait(self, kind):
		store = self.delayed_store(f"{kind}-diagnostics.json")
		manager, start = self.make_manager(kind, store)
		job = start()
		self.assertTrue(store.entered.wait(timeout=3))
		self.assertEqual(manager.get(job["id"])["status"], "succeeded")
		self.assertIsNone(manager.active_job_id)
		# Terminal status alone is not a safe signal to delete the workspace.
		with self.assertRaises(TimeoutError):
			manager.wait_for_workers(timeout=0)
		self.assertFalse(store.finished.is_set())
		self.assertFalse(store.path.exists())
		store.release.set()
		manager.wait_for_workers(timeout=3)
		self.assertTrue(store.finished.is_set())
		self.assertEqual(store.list()[0]["task_id"], job["id"])
		self.assertFalse(store.path.with_suffix(".tmp").exists())
		self.assertEqual(list(self.root.glob(".organize-cancel-*")), [])
		manager.wait_for_workers(timeout=0)

	def test_organize_wait_includes_delayed_diagnostic_write(self):
		self.check_final_write_wait("organize")

	def test_reprocess_wait_includes_delayed_diagnostic_write(self):
		self.check_final_write_wait("reprocess")

	def test_service_teardown_waits_before_removing_directory(self):
		case = service_tests.LocalServerTests("test_second_request_returns_conflict_while_job_runs")
		case.setUp()
		workspace = Path(case.temporary_directory.name)
		store = self.delayed_store("unused.json")
		store.path = workspace / "diagnostics.json"
		case.manager.diagnostic_store = store
		waiting = threading.Event()
		errors = []
		original_wait = case.manager.wait_for_workers

		def observed_wait(timeout=5):
			waiting.set()
			original_wait(timeout)

		case.manager.wait_for_workers = observed_wait

		def teardown():
			try:
				case.tearDown()
			except BaseException as error:
				errors.append(error)

		cleanup_thread = None
		try:
			case.test_second_request_returns_conflict_while_job_runs()
			case.release_pipeline.set()
			self.assertTrue(store.entered.wait(timeout=3))
			cleanup_thread = threading.Thread(target=teardown, daemon=True)
			cleanup_thread.start()
			self.assertTrue(waiting.wait(timeout=3))
			self.assertTrue(workspace.exists())
			self.assertTrue(cleanup_thread.is_alive())
			store.release.set()
			cleanup_thread.join(timeout=5)
			self.assertFalse(cleanup_thread.is_alive())
			self.assertEqual(errors, [])
			self.assertFalse(workspace.exists())
		finally:
			store.release.set()
			case.release_pipeline.set()
			if cleanup_thread is not None:
				cleanup_thread.join(timeout=5)
			else:
				case.tearDown()
			# If an assertion or timeout interrupted teardown, retry only after
			# the diagnostic gate is released and worker writes can finish.
			if workspace.exists() and (cleanup_thread is None or not cleanup_thread.is_alive()):
				for manager in case.job_managers:
					manager.wait_for_workers(timeout=5)
				case.temporary_directory.cleanup()

	def test_wait_keeps_older_worker_after_next_job_starts(self):
		for kind in ("organize", "reprocess"):
			with self.subTest(kind=kind):
				first = self.delayed_store(f"{kind}-first.json")
				manager, start = self.make_manager(kind, first)
				start()
				self.assertTrue(first.entered.wait(timeout=3))
				second = self.delayed_store(f"{kind}-second.json")
				manager.diagnostic_store = second
				start()
				self.assertTrue(second.entered.wait(timeout=3))
				second.release.set()
				self.assertTrue(second.finished.wait(timeout=3))
				with self.assertRaises(TimeoutError):
					manager.wait_for_workers(timeout=0)
				first.release.set()
				manager.wait_for_workers(timeout=3)
				self.assertEqual(len(first.list()), 1)
				self.assertEqual(len(second.list()), 1)

	def test_wait_finishes_even_when_diagnostic_write_fails(self):
		for kind in ("organize", "reprocess"):
			with self.subTest(kind=kind):
				store = self.delayed_store(f"{kind}-failed.json", fail=True)
				manager, start = self.make_manager(kind, store)
				start()
				self.assertTrue(store.entered.wait(timeout=3))
				store.release.set()
				manager.wait_for_workers(timeout=3)
				self.assertTrue(store.finished.is_set())
				self.assertFalse(store.path.exists())

	def test_idle_wait_and_invalid_timeout(self):
		for kind in ("organize", "reprocess"):
			with self.subTest(kind=kind):
				manager, _ = self.make_manager(kind, DiagnosticStore(self.root / f"{kind}-idle.json"))
				manager.wait_for_workers(timeout=0)
				for timeout in (-1, float("inf"), float("nan")):
					with self.assertRaises(ValueError):
						manager.wait_for_workers(timeout=timeout)


if __name__ == "__main__":
	unittest.main()