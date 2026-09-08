"""End-to-end reprocessing tests through cloud-compatible HTTP transports."""

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from llm_adapter import OllamaAdapter, StandardOpenAITransport
from local_server import run_reprocess_pipeline
from model_config import ExternalModelConfigStore


class FakeChatHandler(BaseHTTPRequestHandler):
	requests = []

	def do_POST(self):
		length = int(self.headers.get("Content-Length", "0"))
		body = json.loads(self.rfile.read(length))
		type(self).requests.append({
			"path": self.path,
			"headers": dict(self.headers.items()),
			"body": body,
		})
		system_prompts = "\n".join(
			message.get("content", "")
			for message in body["messages"]
			if message.get("role") == "system"
		)
		if "样式枚举" in system_prompts:
			content = '{"styles":[]}'
		else:
			content = '{"category":"work","status":"todo","event_time":null}'
		payload = json.dumps({
			"choices": [{"message": {"content": content}}]
		}).encode("utf-8")
		self.send_response(200)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(payload)))
		self.end_headers()
		self.wfile.write(payload)

	def log_message(self, format, *args):
		return


class ReprocessCloudBackendTests(unittest.TestCase):
	def setUp(self) -> None:
		FakeChatHandler.requests = []
		self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeChatHandler)
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()
		self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

	def tearDown(self) -> None:
		self.server.shutdown()
		self.server.server_close()
		self.thread.join(timeout=2)

	def run_pipeline(self, transport, backend_id: str) -> dict:
		with tempfile.TemporaryDirectory() as directory:
			root = Path(directory)
			entries_path = root / "entries.json"
			entries_path.write_text(json.dumps([{
				"id": "virtual-entry",
				"text": "虚拟测试记录",
				"record_time": "2026-08-27T10:00:00+08:00",
				"event_time": None,
				"project": "虚拟项目",
				"category": "other",
				"status": "none",
				"source_file": "virtual.txt",
				"user_edited": False,
			}], ensure_ascii=False), encoding="utf-8")
			settings_path = root / "settings.json"
			settings_path.write_text("{}", encoding="utf-8")
			categories_path = root / "categories.json"
			categories_path.write_text(json.dumps([
				{"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}},
				{"code": "other", "labels": {"zh-CN": "其他", "en": "Other"}},
			], ensure_ascii=False), encoding="utf-8")
			store = ExternalModelConfigStore(root / "external-config", project_dir=root / "project")
			adapter = OllamaAdapter(self.origin, "virtual-model", transport=transport)
			with patch(
				"local_server.create_model_adapter",
				return_value=(adapter, backend_id, "虚拟云端 API"),
			):
				succeeded = run_reprocess_pipeline(
					{"virtual-entry"},
					["analysis", "display", "tables"],
					lambda update: None,
					store,
					entries_path=entries_path,
					settings_path=settings_path,
					categories_path=categories_path,
					html_builder=lambda: True,
				)
			self.assertTrue(succeeded)
			entry = json.loads(entries_path.read_text(encoding="utf-8"))[0]
			self.assertEqual(entry["category"], "work")
			self.assertEqual(entry["status"], "todo")
			self.assertEqual(entry["analysis_backend"], backend_id)
			self.assertEqual(entry["display_backend"], backend_id)
			self.assertEqual(entry["table_backend"], backend_id)
			return entry

	def test_standard_openai_transport_runs_full_reprocess_pipeline(self) -> None:
		transport = StandardOpenAITransport(
			self.origin + "/v1",
			"standard-test-secret",
			"standard-test-model",
			_allow_http_for_testing=True,
		)

		self.run_pipeline(transport, "backend-standard")

		self.assertEqual(len(FakeChatHandler.requests), 2)
		self.assertTrue(all(request["path"] == "/v1/chat/completions" for request in FakeChatHandler.requests))
		self.assertTrue(all(
			request["headers"]["Authorization"] == "Bearer standard-test-secret"
			for request in FakeChatHandler.requests
		))


if __name__ == "__main__":
	unittest.main()