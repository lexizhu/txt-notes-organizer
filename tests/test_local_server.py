"""Tests for the loopback-only local service and organize job lock."""

import http.client
import hashlib
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_server import (
	DiagnosticStore,
	JobCancellationTooLateError,
	JobConflictError,
	ModelSettingsManager,
	OrganizeJobManager,
	PurgeManager,
	ReprocessPlanManager,
	ReprocessJobManager,
	ReprocessPlanChangedError,
	UserActionManager,
	create_production_purge_manager,
	create_server,
	current_model_timeout_seconds,
	progress,
	progress_from_output,
	recover_purges_at_startup,
	run_reprocess_pipeline,
	test_cloud_connection,
)
from llm_adapter import AnalysisResult, LLMError
from model_config import (
	STANDARD_OPENAI,
	ExternalModelConfigStore,
	default_model_settings,
)
from purge import PurgeArtifacts, PurgePaths, purge_generated_data


class LocalServerTests(unittest.TestCase):
	def setUp(self) -> None:
		self.job_managers: list[OrganizeJobManager | ReprocessJobManager] = []
		self.temporary_directory = tempfile.TemporaryDirectory()
		root = Path(self.temporary_directory.name)
		self.release_pipeline = threading.Event()
		self.pipeline_started = threading.Event()

		def pipeline(report) -> bool:
			self.pipeline_started.set()
			report({
				"stage": "classifying",
				"label": "AI 分类",
				"message": "正在处理第 1/2 条。",
				"percent": 35,
				"current": 1,
				"total": 2,
				"updated_at": "2026-08-26T10:00:00+08:00",
			})
			self.release_pipeline.wait(timeout=2)
			return True

		self.manager = self.make_organize_manager(root / "organize.lock", pipeline)
		self.entries_path = root / "entries.json"
		entry = {
			"id": "record one",
			"text": "正文",
			"record_time": "2026-08-26T10:00:00+08:00",
			"event_time": None,
			"project": "项目甲",
			"category": "work",
			"status": "none",
			"source_file": "项目甲.txt",
			"user_edited": False,
			"created_at": "2026-08-26T10:00:00+08:00",
			"updated_at": "2026-08-26T10:00:00+08:00",
		}
		self.entries_path.write_text(
			json.dumps([entry], ensure_ascii=False),
			encoding="utf-8",
		)
		self.actions_path = root / "user_actions.jsonl"
		self.build_calls = 0

		def build_html() -> bool:
			self.build_calls += 1
			return True

		self.action_manager = UserActionManager(
			root / "organize.lock",
			self.actions_path,
			self.entries_path,
			build_html,
		)
		self.purged_ids = []
		self.purge_manager = PurgeManager(
			root / "organize.lock",
			root / "watcher-scan.lock",
			lambda record_id: self.purged_ids.append(record_id),
		)
		self.model_store = ExternalModelConfigStore(
			root / "external-model-config",
			project_dir=root / "project",
		)
		self.tested_connections = []

		def test_connection(settings, api_key):
			self.tested_connections.append((settings, api_key))

		self.model_settings_manager = ModelSettingsManager(
			self.model_store,
			root / "organize.lock",
			test_connection,
		)
		self.reprocess_plan_manager = ReprocessPlanManager(
			self.entries_path,
			root / "organize.lock",
			self.model_store,
		)
		self.reprocess_calls = []
		self.reprocess_job_manager = self.make_reprocess_manager(
			self.reprocess_plan_manager,
			root / "organize.lock",
			lambda target_ids, components, report, cancel_check: self.reprocess_calls.append(
				(target_ids, components)
			) or True,
		)
		self.review_path = root / "review.html"
		self.review_path.write_text("<!doctype html><title>Test</title>", encoding="utf-8")
		self.token = "test-token"
		self.server = create_server(
			port=0,
			job_manager=self.manager,
			action_manager=self.action_manager,
			purge_manager=self.purge_manager,
			model_settings_manager=self.model_settings_manager,
			reprocess_plan_manager=self.reprocess_plan_manager,
			reprocess_job_manager=self.reprocess_job_manager,
			review_html_path=self.review_path,
			token=self.token,
		)
		self.port = self.server.server_address[1]
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()

	def make_organize_manager(self, *args, **kwargs) -> OrganizeJobManager:
		manager = OrganizeJobManager(*args, **kwargs)
		self.job_managers.append(manager)
		return manager

	def make_reprocess_manager(self, *args, **kwargs) -> ReprocessJobManager:
		manager = ReprocessJobManager(*args, **kwargs)
		self.job_managers.append(manager)
		return manager

	def tearDown(self) -> None:
		self.release_pipeline.set()
		self.server.shutdown()
		self.server.server_close()
		self.thread.join(timeout=2)
		self.assertFalse(self.thread.is_alive(), "HTTP server did not stop")
		for manager in self.job_managers:
			manager.wait_for_workers(timeout=5)
		self.temporary_directory.cleanup()

	def request(self, method: str, path: str, *, headers=None, body=None):
		connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
		connection.request(method, path, body=body, headers=headers or {})
		response = connection.getresponse()
		payload = response.read()
		result = (response.status, dict(response.getheaders()), payload)
		connection.close()
		return result

	def write_headers(self) -> dict[str, str]:
		return {
			"Host": f"127.0.0.1:{self.port}",
			"Origin": f"http://127.0.0.1:{self.port}",
			"X-Note-Token": self.token,
			"Content-Type": "application/json",
		}

	def test_server_binds_only_to_loopback_and_serves_fixed_review(self) -> None:
		self.assertEqual(self.server.server_address[0], "127.0.0.1")

		status, headers, body = self.request("GET", "/")

		self.assertEqual(status, 200)
		self.assertIn(b"<title>Test</title>", body)
		self.assertNotIn("Access-Control-Allow-Origin", headers)

		status, _, body = self.request("GET", "/api/health")
		self.assertEqual(status, 200)
		health = json.loads(body)
		self.assertEqual(health["status"], "ok")
		self.assertRegex(health["instance_id"], r"^[0-9a-f]{64}$")

	def test_serves_embedded_png_favicon_for_safari(self) -> None:
		status, headers, body = self.request("GET", "/favicon.ico")

		self.assertEqual(status, 200)
		self.assertEqual(headers["Content-Type"], "image/png")
		self.assertEqual(headers["Cache-Control"], "no-store")
		self.assertEqual(body[:8], b"\x89PNG\r\n\x1a\n")

	def test_session_returns_startup_token_only_for_valid_host(self) -> None:
		status, _, body = self.request("GET", "/api/session")
		self.assertEqual(status, 200)
		session = json.loads(body)
		self.assertEqual(session["token"], self.token)
		self.assertTrue(session["capabilities"]["purge"])
		self.assertTrue(session["capabilities"]["model_settings"])
		self.assertTrue(session["capabilities"]["reprocess_plan"])
		self.assertTrue(session["capabilities"]["reprocess_execute"])
		self.assertTrue(session["capabilities"]["diagnostics"])

		status, _, body = self.request(
			"GET",
			"/api/session",
			headers={
				"Host": f"localhost:{self.port}",
				"Origin": f"http://localhost:{self.port}",
			},
		)
		self.assertEqual(status, 200)
		self.assertEqual(json.loads(body)["token"], self.token)

	def test_diagnostics_endpoint_returns_local_bounded_reports(self) -> None:
		status, _, body = self.request("GET", "/api/diagnostics")

		self.assertEqual(status, 200)
		self.assertEqual(json.loads(body), {"reports": []})

		status, _, body = self.request(
			"GET",
			"/api/diagnostics",
			headers={"Host": f"127.0.0.1:{self.port}", "Origin": "https://attacker.example"},
		)
		self.assertEqual(status, 403)
		self.assertEqual(json.loads(body)["error"], "invalid_origin")

	def test_diagnostic_store_excludes_raw_error_and_keeps_latest_five(self) -> None:
		path = Path(self.temporary_directory.name) / "private-diagnostics.json"
		store = DiagnosticStore(path)
		for index in range(6):
			store.record({
				"id": f"job-{index}",
				"status": "failed",
				"created_at": "2026-09-03T10:00:00+08:00",
				"started_at": "2026-09-03T10:00:00+08:00",
				"finished_at": "2026-09-03T10:00:01+08:00",
				"error": "HTTP 401 secret-key note-body /Users/private/note.txt",
				"failure_stage": "classifying",
			})

		serialized = path.read_text(encoding="utf-8")
		reports = json.loads(serialized)
		self.assertEqual(len(reports), 5)
		self.assertEqual(reports[0]["task_id"], "job-5")
		self.assertEqual(reports[-1]["task_id"], "job-1")
		self.assertEqual(reports[0]["error_code"], "MODEL_AUTH_FAILED")
		self.assertNotIn("secret-key", serialized)
		self.assertNotIn("note-body", serialized)
		self.assertNotIn("/Users/", serialized)

	def test_successful_job_with_fallback_is_not_reported_as_plain_success(self) -> None:
		path = Path(self.temporary_directory.name) / "fallback-diagnostics.json"
		store = DiagnosticStore(path)

		def fallback_pipeline(report):
			report(progress("failed", "排版失败", "排版结果无效，已保留原文显示。", 60))
			return True

		manager = self.make_organize_manager(Path(self.temporary_directory.name) / "fallback.lock", fallback_pipeline, diagnostic_store=store)
		job = manager.start()
		for _ in range(100):
			if manager.get(job["id"])["status"] == "succeeded":
				break
			time.sleep(0.01)

		manager.wait_for_workers(timeout=5)
		report = store.list()[0]
		self.assertEqual(report["status"], "succeeded_with_fallback")
		self.assertEqual(report["error_code"], "FORMAT_INVALID")
		self.assertIn("整理完成", report["summary"])

		status, _, _ = self.request(
			"GET",
			"/api/session",
			headers={"Host": "attacker.example"},
		)
		self.assertEqual(status, 400)

	def test_organize_rejects_missing_token_and_cross_origin(self) -> None:
		headers = self.write_headers()
		headers.pop("X-Note-Token")
		status, response_headers, _ = self.request(
			"POST", "/api/organize", headers=headers, body="{}"
		)
		self.assertEqual(status, 403)
		self.assertNotIn("Access-Control-Allow-Origin", response_headers)

		headers = self.write_headers()
		headers["Origin"] = "https://attacker.example"
		status, _, _ = self.request("POST", "/api/organize", headers=headers, body="{}")
		self.assertEqual(status, 403)

	def test_model_settings_get_is_public_profile_and_never_returns_key(self) -> None:
		self.model_store.save_api_key("virtual-secret")

		status, _, body = self.request("GET", "/api/model-settings")
		payload = json.loads(body)

		self.assertEqual(status, 200)
		self.assertEqual(payload["release_profile"], "public")
		self.assertEqual(payload["allowed_cloud_variants"], [STANDARD_OPENAI])
		self.assertTrue(payload["api_key_configured"])
		self.assertEqual(payload["ollama_model"], "qwen3:4b")
		self.assertNotIn("virtual-secret", body.decode())

	def test_model_settings_save_requires_auth_and_privacy_confirmation(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI].update({
			"base_url": "https://api.example.test/v1",
			"model": "virtual-model",
		})
		body = json.dumps({
			"settings": settings,
			"api_key": "virtual-secret",
			"privacy_acknowledged": True,
		})
		headers = self.write_headers()
		headers.pop("X-Note-Token")
		status, _, _ = self.request("POST", "/api/model-settings", headers=headers, body=body)
		self.assertEqual(status, 403)

		unconfirmed = json.dumps({"settings": settings, "api_key": "virtual-secret"})
		status, _, response = self.request(
			"POST", "/api/model-settings", headers=self.write_headers(), body=unconfirmed
		)
		self.assertEqual(status, 400)
		self.assertIn("必须确认", json.loads(response)["message"])
		self.assertFalse(self.model_store.model_settings_path.exists())

	def test_model_settings_api_rejects_blank_active_model_before_saving(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI].update({"base_url": "https://api.example.test/v1", "model": "   "})

		status, _, response = self.request(
			"POST", "/api/model-settings", headers=self.write_headers(),
			body=json.dumps({"settings": settings, "api_key": "virtual-secret", "privacy_acknowledged": True}),
		)

		self.assertEqual(status, 400)
		self.assertEqual(json.loads(response)["error"], "invalid_model_settings")
		self.assertIn("model 不能为空", json.loads(response)["message"])
		self.assertFalse(self.model_store.model_settings_path.exists())

	def test_model_settings_save_stores_key_separately_and_returns_no_secret(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI].update({
			"base_url": "https://api.example.test/v1",
			"model": "virtual-model",
		})

		status, _, body = self.request(
			"POST",
			"/api/model-settings",
			headers=self.write_headers(),
			body=json.dumps({
				"settings": settings,
				"api_key": "virtual-secret",
				"privacy_acknowledged": True,
			}),
		)

		self.assertEqual(status, 200)
		self.assertTrue(json.loads(body)["api_key_configured"])
		self.assertNotIn("virtual-secret", body.decode())
		self.assertNotIn("virtual-secret", self.model_store.model_settings_path.read_text())
		self.assertEqual(self.model_store.load_api_key(), "virtual-secret")

		settings["provider"] = "ollama"
		status, _, _ = self.request(
			"POST",
			"/api/model-settings",
			headers=self.write_headers(),
			body=json.dumps({"settings": settings}),
		)
		self.assertEqual(status, 200)
		self.assertEqual(self.model_store.load_api_key(), "virtual-secret")

		status, _, body = self.request(
			"POST",
			"/api/model-settings",
			headers=self.write_headers(),
			body=json.dumps({"settings": settings, "clear_api_key": True}),
		)
		self.assertEqual(status, 200)
		self.assertFalse(json.loads(body)["api_key_configured"])
		self.assertFalse(self.model_store.secrets_path.exists())

	def test_model_connection_uses_draft_without_saving_and_respects_organize_lock(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI].update({
			"base_url": "https://api.example.test/v1",
			"model": "virtual-model",
		})
		payload = json.dumps({
			"settings": settings,
			"api_key": "virtual-test-key",
			"privacy_acknowledged": True,
		})

		status, _, body = self.request(
			"POST", "/api/model-settings/test", headers=self.write_headers(), body=payload
		)
		self.assertEqual(status, 200)
		self.assertEqual(json.loads(body), {
			"connected": True,
			"cloud_variant": STANDARD_OPENAI,
		})
		self.assertEqual(self.tested_connections[0][1], "virtual-test-key")
		self.assertFalse(self.model_store.model_settings_path.exists())
		self.assertFalse(self.model_store.secrets_path.exists())

		status, _, _ = self.request(
			"POST", "/api/organize", headers=self.write_headers(), body="{}"
		)
		self.assertEqual(status, 202)
		self.assertTrue(self.pipeline_started.wait(timeout=1))
		status, _, body = self.request(
			"POST", "/api/model-settings/test", headers=self.write_headers(), body=payload
		)
		self.assertEqual(status, 409)
		self.assertEqual(json.loads(body)["error"], "organize_in_progress")

	def test_default_connection_probe_sends_only_fixed_virtual_content(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI].update({
			"base_url": "https://api.example.test/v1",
			"model": "virtual-model",
		})

		class FakeTransport:
			def __init__(self):
				self.payload = None

			def post_json(self, path, payload):
				self.payload = payload
				return {"message": {"content": '{"connection":"ok"}'}}

		transport = FakeTransport()
		with patch("local_server.create_cloud_transport", return_value=transport):
			test_cloud_connection(settings, "virtual-secret")

		serialized = json.dumps(transport.payload)
		self.assertIn("connection", serialized)
		self.assertNotIn("entries", serialized)
		self.assertNotIn("notes", serialized.lower())
		self.assertNotIn("virtual-secret", serialized)

	def test_reprocess_plan_is_read_only_and_protects_user_edits(self) -> None:
		entries = [
			{"id": "one", "project": "A", "text": "plain", "user_edited": False},
			{"id": "two", "project": "A", "text": "A  B\n1  2", "user_edited": True},
			{"id": "three", "project": "B", "text": "hidden", "hidden": True, "user_edited": False},
		]
		self.entries_path.write_text(json.dumps(entries), encoding="utf-8")
		before = self.entries_path.read_bytes()
		manager = ReprocessPlanManager(
			self.entries_path,
			self.manager.lock_path,
			self.model_store,
		)

		plan = manager.plan({
			"scope": {"type": "all"},
			"components": ["analysis", "display", "tables"],
		})

		self.assertEqual(plan["matched_records"], 3)
		self.assertRegex(plan["plan_id"], r"^reprocess-plan-v1:[0-9a-f]{64}$")
		self.assertEqual(plan["selected_records"], 2)
		self.assertEqual(plan["skipped_hidden"], 1)
		self.assertEqual(plan["protected_user_edits"], 1)
		self.assertEqual(plan["work_items"], {"analysis": 1, "display": 2, "tables": 2})
		self.assertEqual(plan["estimated_model_calls"], 4)
		self.assertFalse(plan["uses_cloud"])
		self.assertTrue(plan["requires_confirmation"])
		self.assertTrue(plan["has_work"])
		self.assertEqual(self.entries_path.read_bytes(), before)
		self.assertEqual(manager.plan({
			"scope": {"type": "all"},
			"components": ["analysis", "display", "tables"],
		})["plan_id"], plan["plan_id"])

	def test_reprocess_v2_filters_sources_and_repairs_only_degraded_stages(self) -> None:
		text_with_table = "Name  Value\nA     1"
		entries = [
			{"id": "old", "project": "A", "text": "plain", "record_time": "2026-09-01T23:59:00+08:00", "analysis_backend": "backend-low", "user_edited": False},
			{
				"id": "table", "project": "B", "text": text_with_table,
				"record_time": "2026-09-05T00:00:00+08:00", "hidden": True,
				"analysis_backend": "backend-low", "user_edited": False,
				"display_markdown": text_with_table,
				"display_text_sha256": hashlib.sha256(text_with_table.encode()).hexdigest(),
				"display_backend": "backend-low",
			},
			{"id": "outside", "project": "C", "text": "other", "record_time": "2026-09-06T00:00:00+08:00", "analysis_backend": "backend-low", "user_edited": False},
		]
		self.entries_path.write_text(json.dumps(entries), encoding="utf-8")
		scope = {
			"type": "filters", "projects": ["A", "B"],
			"record_date_from": "2026-09-01", "record_date_to": "2026-09-05",
			"visibility": "all",
		}

		full, full_targets = self.reprocess_plan_manager.resolve_unlocked({
			"mode": "full", "scope": scope, "source_backend_ids": ["backend-low"]
		})
		repair, repair_targets = self.reprocess_plan_manager.resolve_unlocked({"mode": "repair", "scope": scope})

		self.assertEqual(full["selected_records"], 2)
		self.assertEqual(full_targets["analysis"], {"old", "table"})
		self.assertEqual(full_targets["display"], {"old", "table"})
		self.assertEqual(repair_targets["analysis"], set())
		self.assertEqual(repair_targets["display"], {"old"})
		self.assertEqual(repair_targets["tables"], {"table"})
		self.assertEqual(repair["estimated_model_calls"], 2)
		self.assertTrue(repair["estimated_input_tokens"]["approximate"])
		self.assertEqual(repair["estimate_scope"], "content_only")

	def test_reprocess_dates_are_canonical_and_use_record_local_calendar_date(self) -> None:
		invalid_scope = {
			"type": "filters", "projects": ["A"],
			"record_date_from": "2026-9-1", "record_date_to": None,
			"visibility": "visible",
		}
		with self.assertRaises(ValueError):
			self.reprocess_plan_manager.plan({"scope": invalid_scope, "components": ["analysis"]})

		entries = [
			{"id": "east", "project": "A", "text": "east", "record_time": "2026-09-02T00:30:00+14:00", "user_edited": False},
			{"id": "west", "project": "A", "text": "west", "record_time": "2026-09-01T23:30:00-10:00", "user_edited": False},
		]
		self.entries_path.write_text(json.dumps(entries), encoding="utf-8")
		base = {"type": "filters", "projects": ["A"], "visibility": "visible"}

		day_one = self.reprocess_plan_manager.plan({"scope": {**base, "record_date_from": "2026-09-01", "record_date_to": "2026-09-01"}, "components": ["analysis"]})
		day_two = self.reprocess_plan_manager.plan({"scope": {**base, "record_date_from": "2026-09-02", "record_date_to": "2026-09-02"}, "components": ["analysis"]})

		self.assertEqual(day_one["selected_records"], 1)
		self.assertEqual(day_two["selected_records"], 1)

	def test_reprocess_options_are_read_only_and_plan_binds_stage_targets(self) -> None:
		self.assertFalse(self.model_store.model_history_path.exists())
		self.reprocess_plan_manager.options()
		self.assertFalse(self.model_store.model_history_path.exists())

		text = "Name  Value\nA     1"
		entry = {
			"id": "one", "project": "A", "text": text,
			"record_time": "2026-09-03T10:00:00+08:00", "user_edited": False,
			"display_markdown": "changed",
			"display_text_sha256": hashlib.sha256(text.encode()).hexdigest(),
			"display_backend": "backend-old",
		}
		self.entries_path.write_text(json.dumps([entry]), encoding="utf-8")
		request = {
			"mode": "repair",
			"scope": {"type": "filters", "projects": ["A"], "record_date_from": None, "record_date_to": None, "visibility": "visible"},
		}
		first = self.reprocess_plan_manager.plan(request)
		entry["display_markdown"] = text
		self.entries_path.write_text(json.dumps([entry]), encoding="utf-8")
		second = self.reprocess_plan_manager.plan(request)

		self.assertNotEqual(first["plan_id"], second["plan_id"])
		self.assertEqual(first["work_items"]["display"], 1)
		self.assertEqual(second["work_items"]["display"], 0)

	def test_reprocess_plan_api_requires_auth_and_supports_exact_record_scope(self) -> None:
		entries = [
			{"id": "one", "project": "A", "text": "first", "user_edited": False},
			{"id": "two", "project": "B", "text": "second", "user_edited": True},
		]
		self.entries_path.write_text(json.dumps(entries), encoding="utf-8")
		body = json.dumps({
			"scope": {"type": "records", "record_ids": ["two"]},
			"components": ["analysis", "display"],
			"include_hidden": True,
		})

		status, _, _ = self.request("POST", "/api/reprocess/plan", body=body)
		self.assertEqual(status, 403)

		before = self.entries_path.read_bytes()
		status, _, response = self.request(
			"POST", "/api/reprocess/plan", headers=self.write_headers(), body=body
		)
		plan = json.loads(response)

		self.assertEqual(status, 200)
		self.assertEqual(plan["scope"], {"type": "records", "record_ids": ["two"]})
		self.assertEqual(plan["selected_records"], 1)
		self.assertEqual(plan["protected_user_edits"], 1)
		self.assertEqual(plan["work_items"], {"analysis": 0, "display": 1, "tables": 0})
		self.assertEqual(self.entries_path.read_bytes(), before)

	def test_reprocess_plan_project_scope_hidden_option_and_cloud_warning(self) -> None:
		entries = [
			{"id": "one", "project": "A", "text": "first", "user_edited": False},
			{"id": "two", "project": "A", "text": "second", "hidden": True, "user_edited": False},
			{"id": "three", "project": "B", "text": "third", "user_edited": False},
		]
		self.entries_path.write_text(json.dumps(entries), encoding="utf-8")
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI].update({
			"base_url": "https://api.example.test/v1",
			"model": "virtual-model",
		})
		self.model_store.save_model_settings(settings)

		status, _, response = self.request(
			"POST",
			"/api/reprocess/plan",
			headers=self.write_headers(),
			body=json.dumps({
				"scope": {"type": "project", "project": "A"},
				"components": ["display"],
				"include_hidden": True,
			}),
		)
		plan = json.loads(response)

		self.assertEqual(status, 200)
		self.assertEqual(plan["matched_records"], 2)
		self.assertEqual(plan["selected_records"], 2)
		self.assertEqual(plan["skipped_hidden"], 0)
		self.assertEqual(plan["estimated_model_calls"], 2)
		self.assertTrue(plan["uses_cloud"])

	def test_reprocess_v2_filters_sources_and_repairs_only_degraded_stages(self) -> None:
		text_with_table = "Name  Value\nA     1"
		entries = [
			{"id": "old", "project": "A", "text": "plain", "record_time": "2026-09-01T23:59:00+08:00", "analysis_backend": "backend-low", "user_edited": False},
			{
				"id": "table", "project": "B", "text": text_with_table,
				"record_time": "2026-09-05T00:00:00+08:00", "hidden": True,
				"analysis_backend": "backend-low", "user_edited": False,
				"display_markdown": text_with_table,
				"display_text_sha256": hashlib.sha256(text_with_table.encode()).hexdigest(),
				"display_backend": "backend-low",
			},
			{"id": "outside", "project": "C", "text": "other", "record_time": "2026-09-06T00:00:00+08:00", "analysis_backend": "backend-low", "user_edited": False},
		]
		self.entries_path.write_text(json.dumps(entries), encoding="utf-8")
		scope = {
			"type": "filters", "projects": ["A", "B"],
			"record_date_from": "2026-09-01", "record_date_to": "2026-09-05",
			"visibility": "all",
		}

		full, full_targets = self.reprocess_plan_manager.resolve_unlocked({
			"mode": "full", "scope": scope, "source_backend_ids": ["backend-low"]
		})
		repair, repair_targets = self.reprocess_plan_manager.resolve_unlocked({"mode": "repair", "scope": scope})

		self.assertEqual(full["selected_records"], 2)
		self.assertEqual(full_targets["analysis"], {"old", "table"})
		self.assertEqual(full_targets["display"], {"old", "table"})
		self.assertEqual(repair_targets["analysis"], set())
		self.assertEqual(repair_targets["display"], {"old"})
		self.assertEqual(repair_targets["tables"], {"table"})
		self.assertEqual(repair["estimated_model_calls"], 2)
		self.assertTrue(repair["estimated_input_tokens"]["approximate"])

	def test_reprocess_plan_rejects_invalid_selection_and_conflicts_with_organize(self) -> None:
		invalid_payloads = [
			{"scope": {"type": "records", "record_ids": ["missing"]}, "components": ["analysis"]},
			{"scope": {"type": "all"}, "components": ["unknown"]},
			{"scope": {"type": "all", "extra": True}, "components": ["analysis"]},
		]
		for payload in invalid_payloads:
			status, _, response = self.request(
				"POST",
				"/api/reprocess/plan",
				headers=self.write_headers(),
				body=json.dumps(payload),
			)
			self.assertEqual(status, 400)
			self.assertEqual(json.loads(response)["error"], "invalid_reprocess_plan")

		status, _, _ = self.request(
			"POST", "/api/organize", headers=self.write_headers(), body="{}"
		)
		self.assertEqual(status, 202)
		self.assertTrue(self.pipeline_started.wait(timeout=1))
		status, _, response = self.request(
			"POST",
			"/api/reprocess/plan",
			headers=self.write_headers(),
			body=json.dumps({"scope": {"type": "all"}, "components": ["analysis"]}),
		)
		self.assertEqual(status, 409)
		self.assertEqual(json.loads(response)["error"], "organize_in_progress")

	def test_reprocess_job_requires_current_plan_and_cloud_confirmation(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI].update({
			"base_url": "https://api.example.test/v1",
			"model": "virtual-model",
		})
		self.model_store.save_model_settings(settings)
		calls = []
		manager = self.make_reprocess_manager(
			self.reprocess_plan_manager,
			self.manager.lock_path,
			lambda target_ids, components, report, cancel_check: calls.append((target_ids, components)) or True,
		)
		request = {
			"scope": {"type": "all"},
			"components": ["analysis"],
			"include_hidden": False,
		}
		plan = self.reprocess_plan_manager.plan(request)

		with self.assertRaisesRegex(ValueError, "明确确认"):
			manager.start({**request, "plan_id": plan["plan_id"]})
		with self.assertRaisesRegex(ValueError, "隐私和费用"):
			manager.start({**request, "plan_id": plan["plan_id"], "confirmed": True})

		self.entries_path.write_text(
			json.dumps([{**json.loads(self.entries_path.read_text())[0], "text": "changed"}]),
			encoding="utf-8",
		)
		with self.assertRaises(ReprocessPlanChangedError):
			manager.start({
				**request,
				"plan_id": plan["plan_id"],
				"confirmed": True,
				"cloud_acknowledged": True,
			})
		self.assertEqual(calls, [])

	def test_reprocess_api_starts_confirmed_job_and_exposes_progress(self) -> None:
		request = {
			"scope": {"type": "all"},
			"components": ["analysis", "display"],
			"include_hidden": False,
		}
		plan = self.reprocess_plan_manager.plan(request)
		status, _, response = self.request(
			"POST",
			"/api/reprocess",
			headers=self.write_headers(),
			body=json.dumps({
				**request,
				"plan_id": plan["plan_id"],
				"confirmed": True,
			}),
		)
		job = json.loads(response)["job"]

		self.assertEqual(status, 202)
		for _ in range(100):
			status, _, body = self.request("GET", f"/api/jobs/{job['id']}")
			polled = json.loads(body)["job"]
			if polled["status"] == "succeeded":
				break
			time.sleep(0.01)
		self.assertEqual(status, 200)
		self.assertEqual(polled["status"], "succeeded")
		self.assertEqual(self.reprocess_calls, [({
			"analysis": {"record one"},
			"display": {"record one"},
			"tables": set(),
		}, ["analysis", "display"])])
		status, _, body = self.request(
			"POST",
			"/api/reprocess",
			headers=self.write_headers(),
			body=json.dumps({
				**request,
				"plan_id": plan["plan_id"],
				"confirmed": True,
			}),
		)
		self.assertEqual(status, 409)
		self.assertEqual(json.loads(body)["error"], "reprocess_plan_changed")

	def test_reprocess_job_writes_terminal_diagnostic(self) -> None:
		root = Path(self.temporary_directory.name)
		store = DiagnosticStore(root / "reprocess-diagnostics.json")
		manager = self.make_reprocess_manager(
			self.reprocess_plan_manager,
			root / "reprocess-diagnostic.lock",
			lambda target_ids, components, report, cancel_check: True,
			diagnostic_store=store,
		)
		request = {"scope": {"type": "all"}, "components": ["analysis"]}
		plan = self.reprocess_plan_manager.plan(request)
		job = manager.start({**request, "plan_id": plan["plan_id"], "confirmed": True})
		for _ in range(100):
			if manager.get(job["id"])["status"] == "succeeded":
				break
			time.sleep(0.01)

		report = store.list()[0]
		self.assertEqual(report["task_id"], job["id"])
		self.assertEqual(report["status"], "succeeded")
		self.assertEqual(report["error_code"], "OK")

	def test_reprocess_plan_id_binds_current_model_identity(self) -> None:
		request = {"scope": {"type": "all"}, "components": ["analysis"]}
		with patch("local_server.describe_model_backend", return_value={
			"id": "backend-one", "label": "One", "provider": "ollama", "model": "one"
		}):
			first = self.reprocess_plan_manager.plan(request)
		with patch("local_server.describe_model_backend", return_value={
			"id": "backend-two", "label": "Two", "provider": "ollama", "model": "two"
		}):
			second = self.reprocess_plan_manager.plan(request)

		self.assertNotEqual(first["plan_id"], second["plan_id"])

	def test_reprocess_cancel_api_stops_and_releases_lock(self) -> None:
		started = threading.Event()
		release = threading.Event()
		observed_cancel = []

		def cancellable_executor(target_ids, components, report, cancel_check):
			started.set()
			release.wait(timeout=2)
			observed_cancel.append(cancel_check())
			return False

		manager = self.make_reprocess_manager(
			self.reprocess_plan_manager, self.manager.lock_path, cancellable_executor
		)
		self.server.reprocess_job_manager = manager
		request = {"scope": {"type": "all"}, "components": ["display"], "include_hidden": False}
		plan = self.reprocess_plan_manager.plan(request)
		status, _, body = self.request(
			"POST", "/api/reprocess", headers=self.write_headers(),
			body=json.dumps({**request, "plan_id": plan["plan_id"], "confirmed": True}),
		)
		self.assertEqual(status, 202)
		job_id = json.loads(body)["job"]["id"]
		self.assertTrue(started.wait(timeout=1))
		status, _, body = self.request(
			"POST", f"/api/jobs/{job_id}/cancel", headers=self.write_headers(), body="{}"
		)
		self.assertEqual(status, 202)
		self.assertEqual(json.loads(body)["job"]["status"], "cancelling")
		release.set()
		for _ in range(100):
			status, _, body = self.request("GET", f"/api/jobs/{job_id}")
			job = json.loads(body)["job"]
			if job["status"] == "cancelled":
				break
			time.sleep(0.01)
		self.assertEqual(job["status"], "cancelled")
		self.assertEqual(observed_cancel, [True])
		with self.manager.lock_path.open("a+") as lock_file:
			import fcntl
			fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

	def test_reprocess_pipeline_updates_selected_derivatives_then_rebuilds_html(self) -> None:
		root = Path(self.temporary_directory.name) / "reprocess-pipeline"
		root.mkdir()
		entries_path = root / "entries.json"
		settings_path = root / "settings.json"
		categories_path = root / "categories.json"
		entries_path.write_text(json.dumps([{
			"id": "one",
			"text": "目标正文",
			"record_time": "2026-08-27T10:00:00+08:00",
			"project": "A",
			"category": "other",
			"status": "none",
			"event_time": None,
			"user_edited": False,
		}]), encoding="utf-8")
		settings_path.write_text("{}", encoding="utf-8")
		categories_path.write_text(json.dumps([
			{"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}},
			{"code": "other", "labels": {"zh-CN": "其他", "en": "Other"}},
		]), encoding="utf-8")

		class FakeBackend:
			def analyze(self, text, record_time, categories):
				return AnalysisResult("work", "todo", None)

			def format_markdown(self, text):
				return text

			def identify_tables(self, text):
				return []

		progress_updates = []
		build_calls = []
		with patch(
			"local_server.create_model_adapter",
			return_value=(FakeBackend(), "backend-new", "虚拟模型"),
		):
			succeeded = run_reprocess_pipeline(
				{"one"},
				["analysis", "display", "tables"],
				progress_updates.append,
				self.model_store,
				entries_path=entries_path,
				settings_path=settings_path,
				categories_path=categories_path,
				html_builder=lambda: build_calls.append(True) or True,
			)

		self.assertTrue(succeeded)
		self.assertEqual(build_calls, [True])
		self.assertEqual(
			[update["stage"] for update in progress_updates],
			["classifying", "formatting", "tables", "rebuilding"],
		)
		saved = json.loads(entries_path.read_text(encoding="utf-8"))[0]
		self.assertEqual(saved["category"], "work")
		self.assertEqual(saved["analysis_backend"], "backend-new")
		self.assertEqual(saved["display_backend"], "backend-new")
		self.assertEqual(saved["table_backend"], "backend-new")

	def test_model_connection_response_redacts_key_from_untrusted_tester_error(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI].update({
			"base_url": "https://api.example.test/v1",
			"model": "virtual-model",
		})
		self.model_settings_manager.connection_tester = lambda settings, api_key: (
			_ for _ in ()
		).throw(LLMError(f"upstream echoed {api_key}"))

		status, _, body = self.request(
			"POST",
			"/api/model-settings/test",
			headers=self.write_headers(),
			body=json.dumps({
				"settings": settings,
				"api_key": "virtual-secret",
				"privacy_acknowledged": True,
			}),
		)

		self.assertEqual(status, 502)
		self.assertIn("[REDACTED]", json.loads(body)["message"])
		self.assertNotIn("virtual-secret", body.decode())

	def test_organize_is_async_and_job_can_be_polled(self) -> None:
		started_at = time.monotonic()
		status, _, body = self.request(
			"POST", "/api/organize", headers=self.write_headers(), body="{}"
		)
		elapsed = time.monotonic() - started_at
		job_id = json.loads(body)["job"]["id"]

		self.assertEqual(status, 202)
		self.assertLess(elapsed, 1)
		self.assertTrue(self.pipeline_started.wait(timeout=1))
		status, _, body = self.request("GET", f"/api/jobs/{job_id}")
		self.assertEqual(status, 200)
		current_job = json.loads(body)["job"]
		self.assertEqual(current_job["status"], "running")
		self.assertEqual(current_job["progress"]["stage"], "classifying")
		self.assertEqual(current_job["progress"]["current"], 1)
		self.assertEqual(current_job["progress"]["total"], 2)

		self.release_pipeline.set()
		for _ in range(100):
			status, _, body = self.request("GET", f"/api/jobs/{job_id}")
			if json.loads(body)["job"]["status"] == "succeeded":
				break
			time.sleep(0.01)
		self.assertEqual(json.loads(body)["job"]["status"], "succeeded")

	def test_organize_cancel_api_requires_auth_and_reaches_cancelled(self) -> None:
		status, _, body = self.request(
			"POST", "/api/organize", headers=self.write_headers(), body="{}"
		)
		self.assertEqual(status, 202)
		job_id = json.loads(body)["job"]["id"]
		self.assertTrue(self.pipeline_started.wait(timeout=1))
		status, _, _ = self.request("POST", f"/api/jobs/{job_id}/cancel", body="{}")
		self.assertEqual(status, 403)
		status, _, body = self.request(
			"POST", f"/api/jobs/{job_id}/cancel", headers=self.write_headers(), body="{}"
		)
		self.assertEqual(status, 202)
		self.assertEqual(json.loads(body)["job"]["status"], "cancelling")
		self.release_pipeline.set()
		for _ in range(100):
			status, _, body = self.request("GET", f"/api/jobs/{job_id}")
			job = json.loads(body)["job"]
			if job["status"] == "cancelled":
				break
			time.sleep(0.01)
		self.assertEqual(job["status"], "cancelled")

	def test_cancelled_organize_waits_for_pipeline_then_releases_lock(self) -> None:
		root = Path(self.temporary_directory.name)
		started = threading.Event()
		release = threading.Event()

		def slow_pipeline(report):
			started.set()
			release.wait(timeout=2)
			report(progress("classifying", "AI 分类", "迟到进度", 50))
			return True

		manager = self.make_organize_manager(root / "cancel.lock", slow_pipeline)
		job = manager.start()
		self.assertTrue(started.wait(timeout=1))
		cancelling = manager.cancel(job["id"])
		self.assertEqual(cancelling["status"], "cancelling")
		self.assertIn("最多约再等 5 分钟", cancelling["progress"]["message"])
		release.set()
		for _ in range(100):
			finished = manager.get(job["id"])
			if finished["status"] == "cancelled":
				break
			time.sleep(0.01)
		self.assertEqual(finished["status"], "cancelled")
		self.assertFalse((root / f".organize-cancel-{job['id']}").exists())
		second = self.make_organize_manager(root / "cancel.lock", lambda report: True)
		self.assertEqual(second.start()["status"], "queued")

	def test_cancel_wait_limit_uses_current_model_timeout(self) -> None:
		root = Path(self.temporary_directory.name)
		settings_path = root / "settings.json"
		settings_path.write_text(json.dumps({"ollama": {"timeout_seconds": 300}}), encoding="utf-8")
		self.assertEqual(current_model_timeout_seconds(self.model_store, settings_path), 300)
		model_settings = default_model_settings()
		model_settings["provider"] = "cloud"
		model_settings[STANDARD_OPENAI]["base_url"] = "https://api.example.test/v1"
		model_settings[STANDARD_OPENAI]["model"] = "virtual-model"
		model_settings[STANDARD_OPENAI]["timeout_seconds"] = 120
		self.model_store.save_model_settings(model_settings)
		self.assertEqual(current_model_timeout_seconds(self.model_store, settings_path), 120)
		settings_path.write_text(json.dumps({"ollama": {"timeout_seconds": float("nan")}}), encoding="utf-8")
		model_settings["provider"] = "ollama"
		self.model_store.save_model_settings(model_settings)
		self.assertEqual(current_model_timeout_seconds(self.model_store, settings_path), 300)

	def test_organize_cancel_is_rejected_after_page_rebuild_starts(self) -> None:
		root = Path(self.temporary_directory.name)
		manager = self.make_organize_manager(root / "late-cancel.lock", lambda report: True)
		manager.jobs["late"] = {
			"id": "late", "status": "running", "cancel_requested": False,
			"progress": progress("rebuilding", "重建页面", "正在生成页面", 90),
		}
		with self.assertRaisesRegex(JobCancellationTooLateError, "重建"):
			manager.cancel("late")

	def test_organize_cancel_marker_failure_does_not_change_job_state(self) -> None:
		root = Path(self.temporary_directory.name)
		manager = self.make_organize_manager(root / "marker-failure.lock", lambda report: True)
		manager.jobs["write-failure"] = {
			"id": "write-failure", "status": "running", "cancel_requested": False,
			"progress": progress("classifying", "AI 分类", "正在处理", 30),
		}
		with patch("pathlib.Path.touch", side_effect=OSError("injected write failure")):
			with self.assertRaisesRegex(OSError, "injected"):
				manager.cancel("write-failure")
		self.assertEqual(manager.jobs["write-failure"]["status"], "running")
		self.assertFalse(manager.jobs["write-failure"]["cancel_requested"])

	def test_second_request_returns_conflict_while_job_runs(self) -> None:
		status, _, first_body = self.request(
			"POST", "/api/organize", headers=self.write_headers(), body="{}"
		)
		self.assertEqual(status, 202)
		self.assertTrue(self.pipeline_started.wait(timeout=1))

		status, _, second_body = self.request(
			"POST", "/api/organize", headers=self.write_headers(), body="{}"
		)

		self.assertEqual(status, 409)
		self.assertEqual(
			json.loads(second_body)["job_id"],
			json.loads(first_body)["job"]["id"],
		)

	def test_process_lock_blocks_a_second_manager(self) -> None:
		first_job = self.manager.start()
		self.assertTrue(self.pipeline_started.wait(timeout=1))
		second_manager = self.make_organize_manager(self.manager.lock_path, lambda report: True)

		with self.assertRaises(JobConflictError):
			second_manager.start()

		self.release_pipeline.set()
		for _ in range(100):
			if self.manager.get(first_job["id"])["status"] == "succeeded":
				break
			time.sleep(0.01)
		self.assertEqual(self.manager.get(first_job["id"])["status"], "succeeded")

	def test_api_accepts_no_command_or_path_parameters(self) -> None:
		status, _, body = self.request(
			"POST",
			"/api/organize",
			headers=self.write_headers(),
			body=json.dumps({"command": "anything", "path": "/tmp"}),
		)

		self.assertEqual(status, 400)
		self.assertEqual(json.loads(body)["error"], "unexpected_parameters")

		status, _, _ = self.request(
			"POST",
			"/api/organize?command=anything",
			headers=self.write_headers(),
			body="{}",
		)
		self.assertEqual(status, 404)

	def test_failed_pipeline_reports_failure_and_releases_lock(self) -> None:
		root = Path(self.temporary_directory.name)
		def failed_pipeline(report):
			report({
				"stage": "formatting",
				"label": "生成排版",
				"message": "第 2 条排版失败：模型没有返回结果。",
				"percent": 60,
				"current": 2,
				"total": 3,
				"updated_at": "2026-08-26T10:00:00+08:00",
			})
			return False

		manager = self.make_organize_manager(root / "failure.lock", failed_pipeline)
		job = manager.start()
		for _ in range(100):
			current = manager.get(job["id"])
			if current is not None and current["status"] == "failed":
				break
			time.sleep(0.01)

		self.assertIsNotNone(current)
		self.assertEqual(current["status"], "failed")
		self.assertEqual(current["error"], "第 2 条排版失败：模型没有返回结果。")
		self.assertEqual(current["progress"]["stage"], "failed")
		second_manager = self.make_organize_manager(root / "failure.lock", lambda report: True)
		second_job = second_manager.start()
		for _ in range(100):
			second_current = second_manager.get(second_job["id"])
			if second_current is not None and second_current["status"] == "succeeded":
				break
			time.sleep(0.01)
		self.assertEqual(second_current["status"], "succeeded")

	def test_translates_pipeline_output_into_detailed_progress(self) -> None:
		detected = progress_from_output(
			"[time] [INFO] 读取到 8 条原始记录，其中 3 条需要整理；模型：test。"
		)
		classified = progress_from_output("[time] [INFO] 正在处理第 2/3 条：正文")
		formatting = progress_from_output("[time] [INFO] 正在排版第 1/2 条：正文")
		tables = progress_from_output("[time] [INFO] 正在识别表格第 2/2 条：发现 1 个候选。")
		scanned = progress_from_output("[time] [INFO] [扫描] 已检查 notes/，发现 4 条新增")
		rebuilding = progress_from_output("[time] [INFO] 步骤 3/3：开始生成 HTML。")
		failed = progress_from_output("[time] [ERROR] 排版失败：模型超时")
		waiting = progress_from_output("[time] [INFO] 正在调用本地 LLM，请稍候……")

		self.assertEqual(detected["message"], "检查了 8 条记录，发现 3 条需要整理。")
		self.assertEqual(classified["stage"], "classifying")
		self.assertIn("第 2/3 条", classified["message"])
		self.assertIn("1-2 分钟", classified["message"])
		self.assertEqual(formatting["stage"], "formatting")
		self.assertEqual(tables["percent"], 85)
		self.assertEqual(scanned["message"], "发现 4 条新增或修改的完整记录。")
		self.assertEqual(rebuilding["stage"], "rebuilding")
		self.assertEqual(failed["message"], "排版失败：模型超时")
		self.assertIsNone(waiting)

	def test_hide_and_restore_append_events_and_update_projection(self) -> None:
		status, _, body = self.request(
			"POST",
			"/api/records/record%20one/hide",
			headers=self.write_headers(),
			body="{}",
		)

		self.assertEqual(status, 200)
		self.assertTrue(json.loads(body)["hidden"])
		self.assertEqual(json.loads(body)["hidden_count"], 1)
		entries = json.loads(self.entries_path.read_text(encoding="utf-8"))
		self.assertTrue(entries[0]["hidden"])
		self.assertIn("hidden_at", entries[0])

		status, _, body = self.request(
			"POST",
			"/api/records/record%20one/restore",
			headers=self.write_headers(),
			body="{}",
		)
		self.assertEqual(status, 200)
		self.assertFalse(json.loads(body)["hidden"])
		self.assertEqual(json.loads(body)["hidden_count"], 0)
		entries = json.loads(self.entries_path.read_text(encoding="utf-8"))
		self.assertFalse(entries[0]["hidden"])
		self.assertNotIn("hidden_at", entries[0])
		actions = [json.loads(line) for line in self.actions_path.read_text(encoding="utf-8").splitlines()]
		self.assertEqual([action["event_type"] for action in actions], ["hide", "restore"])
		self.assertEqual(self.build_calls, 2)

	def test_user_action_rejects_unknown_record_and_invalid_path(self) -> None:
		status, _, body = self.request(
			"POST",
			"/api/records/missing/hide",
			headers=self.write_headers(),
			body="{}",
		)
		self.assertEqual(status, 404)
		self.assertEqual(json.loads(body)["error"], "record_not_found")
		self.assertFalse(self.actions_path.exists())

		status, _, _ = self.request(
			"POST",
			"/api/records/record%2Fone/hide",
			headers=self.write_headers(),
			body="{}",
		)
		self.assertEqual(status, 404)

	def test_user_action_conflicts_with_running_organize(self) -> None:
		status, _, _ = self.request(
			"POST", "/api/organize", headers=self.write_headers(), body="{}"
		)
		self.assertEqual(status, 202)
		self.assertTrue(self.pipeline_started.wait(timeout=1))

		status, _, body = self.request(
			"POST",
			"/api/records/record%20one/hide",
			headers=self.write_headers(),
			body="{}",
		)
		self.assertEqual(status, 409)
		self.assertEqual(json.loads(body)["error"], "organize_in_progress")
		self.assertFalse(self.actions_path.exists())

	def test_purge_api_requires_authentication_and_calls_injected_purger(self) -> None:
		headers = self.write_headers()
		headers.pop("X-Note-Token")
		status, _, _ = self.request(
			"POST", "/api/records/record%20one/purge", headers=headers, body="{}"
		)
		self.assertEqual(status, 403)
		self.assertEqual(self.purged_ids, [])

		status, _, body = self.request(
			"POST",
			"/api/records/record%20one/purge",
			headers=self.write_headers(),
			body="{}",
		)
		self.assertEqual(status, 200)
		self.assertEqual(json.loads(body), {"record_id": "record one", "deleted": True})
		self.assertEqual(self.purged_ids, ["record one"])

	def test_purge_conflicts_with_organize_and_scan_locks(self) -> None:
		status, _, _ = self.request(
			"POST", "/api/organize", headers=self.write_headers(), body="{}"
		)
		self.assertEqual(status, 202)
		self.assertTrue(self.pipeline_started.wait(timeout=1))
		status, _, body = self.request(
			"POST",
			"/api/records/record%20one/purge",
			headers=self.write_headers(),
			body="{}",
		)
		self.assertEqual(status, 409)
		self.assertEqual(json.loads(body)["error"], "operation_in_progress")
		self.release_pipeline.set()
		for _ in range(100):
			if self.manager.active_job_id is None:
				break
			time.sleep(0.01)

		import fcntl
		with self.purge_manager.scan_lock_path.open("a+", encoding="utf-8") as scan_lock:
			fcntl.flock(scan_lock.fileno(), fcntl.LOCK_EX)
			status, _, body = self.request(
				"POST",
				"/api/records/record%20one/purge",
				headers=self.write_headers(),
				body="{}",
			)
			self.assertEqual(status, 409)
			self.assertEqual(json.loads(body)["error"], "operation_in_progress")

	def test_production_server_without_purger_keeps_endpoint_disabled(self) -> None:
		self.server.purge_manager = None

		status, _, body = self.request(
			"POST",
			"/api/records/record%20one/purge",
			headers=self.write_headers(),
			body="{}",
		)

		self.assertEqual(status, 503)
		self.assertEqual(json.loads(body)["error"], "purge_not_available")

	def test_server_without_model_settings_manager_disables_capability_and_api(self) -> None:
		self.server.model_settings_manager = None

		status, _, body = self.request("GET", "/api/session")
		self.assertEqual(status, 200)
		self.assertFalse(json.loads(body)["capabilities"]["model_settings"])

		status, _, body = self.request("GET", "/api/model-settings")
		self.assertEqual(status, 503)
		self.assertEqual(json.loads(body)["error"], "model_settings_not_available")

		self.server.reprocess_plan_manager = None
		status, _, body = self.request("GET", "/api/session")
		self.assertEqual(status, 200)
		self.assertFalse(json.loads(body)["capabilities"]["reprocess_plan"])
		self.server.reprocess_job_manager = None
		status, _, body = self.request("GET", "/api/session")
		self.assertEqual(status, 200)
		self.assertFalse(json.loads(body)["capabilities"]["reprocess_execute"])
		status, _, body = self.request(
			"POST",
			"/api/reprocess/plan",
			headers=self.write_headers(),
			body=json.dumps({"scope": {"type": "all"}, "components": ["analysis"]}),
		)
		self.assertEqual(status, 503)
		self.assertEqual(json.loads(body)["error"], "reprocess_plan_not_available")

	def test_purge_service_failure_returns_500_and_releases_locks(self) -> None:
		self.server.purge_manager = PurgeManager(
			self.purge_manager.organize_lock_path,
			self.purge_manager.scan_lock_path,
			lambda record_id: (_ for _ in ()).throw(RuntimeError("测试服务故障")),
		)

		status, _, body = self.request(
			"POST",
			"/api/records/record%20one/purge",
			headers=self.write_headers(),
			body="{}",
		)

		self.assertEqual(status, 500)
		self.assertEqual(json.loads(body)["error"], "purge_failed")
		self.server.purge_manager = self.purge_manager
		self.assertEqual(self.purge_manager.purge("retry"), {"record_id": "retry", "deleted": True})

	def test_startup_recovery_uses_both_locks_and_removes_backup(self) -> None:
		root = Path(self.temporary_directory.name)
		data_dir = root / "recovery-data"
		backup_dir = data_dir / ".purge-transaction-leftover" / "backup"
		backup_dir.mkdir(parents=True)
		entries_path = data_dir / "entries.json"
		original = '[{"id":"kept"}]\n'
		entries_path.write_text("[]\n", encoding="utf-8")
		(backup_dir / "entries.json").write_text(original, encoding="utf-8")
		organize_lock = data_dir / "organize.lock"
		scan_lock = data_dir / "watcher-scan.lock"

		recovered = recover_purges_at_startup(data_dir, organize_lock, scan_lock)

		self.assertEqual(recovered, 1)
		self.assertEqual(entries_path.read_text(encoding="utf-8"), original)
		self.assertEqual(list(data_dir.glob(".purge-transaction-*")), [])
		manager = PurgeManager(organize_lock, scan_lock, lambda record_id: None)
		self.assertEqual(manager.purge("kept"), {"record_id": "kept", "deleted": True})

	def test_hide_request_rebuilds_served_page_with_real_builder(self) -> None:
		from build_html import build_html

		project_root = Path(__file__).parents[1]
		categories = [{"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}}]

		def build_real_html() -> bool:
			entries = json.loads(self.entries_path.read_text(encoding="utf-8"))
			output = build_html(
				entries,
				categories,
				"zh-CN",
				4,
				(project_root / "vendor" / "markdown-it.min.js").read_text(encoding="utf-8"),
				(project_root / "output" / "review-mock.html").read_text(encoding="utf-8"),
			)
			self.review_path.write_text(output, encoding="utf-8")
			return True

		self.server.action_manager = UserActionManager(
			self.manager.lock_path,
			self.actions_path,
			self.entries_path,
			build_real_html,
		)

		status, _, _ = self.request(
			"POST",
			"/api/records/record%20one/hide",
			headers=self.write_headers(),
			body="{}",
		)
		self.assertEqual(status, 200)

		status, _, page = self.request("GET", "/")
		self.assertEqual(status, 200)
		self.assertIn(b'"hidden": true', page)
		self.assertIn("离线只读模式".encode(), page)
		self.assertIn(b'id="hidden-toggle"', page)

	def test_purge_api_runs_full_temporary_data_html_and_log_transaction(self) -> None:
		from build_html import build_html

		root = Path(self.temporary_directory.name) / "purge-e2e"
		data_dir = root / "data"
		output_dir = root / "output"
		notes_dir = root / "notes"
		data_dir.mkdir(parents=True)
		output_dir.mkdir()
		notes_dir.mkdir()
		note_path = notes_dir / "demo.txt"
		note_path.write_text("虚拟敏感正文\n\n\n\n虚拟邻居正文\n\n\n\n", encoding="utf-8")
		note_hash = hashlib.sha256(note_path.read_bytes()).hexdigest()
		paths = PurgePaths(
			data_dir / "captured_entries.jsonl",
			data_dir / "entries.json",
			data_dir / "user_actions.jsonl",
			data_dir / "watcher_state.json",
		)
		target_event = {
			"event_id": "insert-target",
			"event_type": "insert",
			"record_id": "target-id",
			"id": "target-id",
			"text": "虚拟敏感正文",
			"record_time": "2026-08-26T10:00:00+08:00",
			"project": "Demo",
			"source_file": "demo.txt",
		}
		neighbor_event = {**target_event, "event_id": "insert-neighbor", "record_id": "neighbor-id", "id": "neighbor-id", "text": "虚拟邻居正文"}
		paths.captured_entries.write_text(
			json.dumps(target_event, ensure_ascii=False) + "\n" + json.dumps(neighbor_event, ensure_ascii=False) + "\n",
			encoding="utf-8",
		)
		entries = [
			{
				"id": "target-id", "text": "虚拟敏感正文", "source_file": "demo.txt",
				"project": "Demo", "category": "work", "status": "none",
				"record_time": "2026-08-26T10:00:00+08:00", "event_time": None,
				"user_edited": False, "created_at": "2026-08-26T10:00:00+08:00",
				"updated_at": "2026-08-26T10:00:00+08:00", "hidden": True,
				"hidden_at": "2026-08-26T11:00:00+08:00",
			},
			{
				"id": "neighbor-id", "text": "虚拟邻居正文", "source_file": "demo.txt",
				"project": "Demo", "category": "work", "status": "none",
				"record_time": "2026-08-26T10:00:00+08:00", "event_time": None,
				"user_edited": False, "created_at": "2026-08-26T10:00:00+08:00",
				"updated_at": "2026-08-26T10:00:00+08:00", "hidden": False,
			},
		]
		paths.entries.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
		paths.user_actions.write_text(
			json.dumps({
				"event_id": "hide-target", "event_type": "hide", "record_id": "target-id",
				"occurred_at": "2026-08-26T11:00:00+08:00", "actor": "user",
			}, ensure_ascii=False) + "\n",
			encoding="utf-8",
		)
		paths.watcher_state.write_text(json.dumps({"files": {"demo.txt": {"records": [
			{"record_id": "target-id", "text": "虚拟敏感正文"},
			{"record_id": "neighbor-id", "text": "虚拟邻居正文"},
		]}}}, ensure_ascii=False), encoding="utf-8")
		review_path = output_dir / "review.html"
		review_path.write_text("旧页面：虚拟敏感正文", encoding="utf-8")
		log_path = data_dir / "local-server.log"
		rotated_log = data_dir / "local-server.log.1"
		log_path.write_text("[INFO] 虚拟敏感正文\n[INFO] 虚拟邻居正文\n", encoding="utf-8")
		rotated_log.write_text("[INFO] target-id 虚拟敏感正文\n", encoding="utf-8")
		project_root = Path(__file__).parents[1]
		config_dir = root / "config"
		vendor_dir = root / "vendor"
		config_dir.mkdir()
		vendor_dir.mkdir()
		shutil.copy2(project_root / "config" / "settings.json", config_dir / "settings.json")
		(config_dir / "categories.json").write_text(
			json.dumps([
				{"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}},
				{"code": "other", "labels": {"zh-CN": "其他", "en": "Other"}},
			]),
			encoding="utf-8",
		)
		shutil.copy2(project_root / "vendor" / "markdown-it.min.js", vendor_dir / "markdown-it.min.js")
		shutil.copy2(project_root / "output" / "review-mock.html", output_dir / "review-mock.html")

		self.server.purge_manager = create_production_purge_manager(root)
		late_rotated_log = data_dir / "watcher.log.3"
		late_rotated_log.write_text("[INFO] target-id 虚拟敏感正文\n", encoding="utf-8")

		status, _, body = self.request(
			"POST", "/api/records/target-id/purge", headers=self.write_headers(), body="{}"
		)

		self.assertEqual(status, 200)
		self.assertEqual(json.loads(body), {"record_id": "target-id", "deleted": True})
		for path in (*paths.files().values(), review_path, log_path, rotated_log, late_rotated_log):
			content = path.read_text(encoding="utf-8")
			self.assertNotIn("虚拟敏感正文", content)
			self.assertNotIn("target-id", content)
		self.assertIn("虚拟邻居正文", review_path.read_text(encoding="utf-8"))
		self.assertEqual(hashlib.sha256(note_path.read_bytes()).hexdigest(), note_hash)
		self.assertEqual(list(data_dir.glob(".purge-transaction-*")), [])


if __name__ == "__main__":
	unittest.main()