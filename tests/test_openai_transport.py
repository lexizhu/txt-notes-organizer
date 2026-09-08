"""Tests for the standard OpenAI transport using a loopback fake service."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from llm_adapter import LLMError, OllamaAdapter, StandardOpenAITransport


CATEGORIES = [
	{"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}},
	{"code": "other", "labels": {"zh-CN": "其他", "en": "Other"}},
]


class FakeOpenAIHandler(BaseHTTPRequestHandler):
	response_status = 200
	response_body = {
		"choices": [{"message": {"content": '{"category":"work"}'}}]
	}
	requests = []

	def do_POST(self):
		length = int(self.headers.get("Content-Length", "0"))
		body = self.rfile.read(length)
		type(self).requests.append({
			"path": self.path,
			"headers": dict(self.headers.items()),
			"body": json.loads(body),
		})
		content = json.dumps(type(self).response_body).encode("utf-8")
		self.send_response(type(self).response_status)
		self.send_header("Content-Type", "application/json")
		self.send_header("Content-Length", str(len(content)))
		self.end_headers()
		self.wfile.write(content)

	def log_message(self, format, *args):
		return


class StandardOpenAITransportTests(unittest.TestCase):
	def setUp(self) -> None:
		FakeOpenAIHandler.response_status = 200
		FakeOpenAIHandler.response_body = {
			"choices": [{"message": {"content": '{"category":"work"}'}}]
		}
		FakeOpenAIHandler.requests = []
		self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeOpenAIHandler)
		self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
		self.thread.start()
		self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/"

	def tearDown(self) -> None:
		self.server.shutdown()
		self.server.server_close()
		self.thread.join(timeout=2)

	def transport(self, api_key="test-secret") -> StandardOpenAITransport:
		return StandardOpenAITransport(
			self.base_url,
			api_key,
			"test-model",
			timeout_seconds=5,
			_allow_http_for_testing=True,
		)

	def test_sends_standard_chat_completions_request_and_normalizes_response(self) -> None:
		result = self.transport().post_json(
			"/api/chat",
			{
				"model": "ollama-model-must-not-leak",
				"messages": [
					{"role": "system", "content": "system"},
					{"role": "user", "content": "virtual note"},
				],
				"stream": False,
				"think": False,
				"format": {"type": "object"},
				"options": {"temperature": 0},
			},
		)

		self.assertEqual(result, {"message": {"content": '{"category":"work"}'}})
		request = FakeOpenAIHandler.requests[0]
		self.assertEqual(request["path"], "/v1/chat/completions")
		self.assertEqual(request["headers"]["Authorization"], "Bearer test-secret")
		self.assertEqual(request["headers"]["Content-Type"], "application/json")
		self.assertEqual(request["body"], {
			"model": "test-model",
			"messages": [
				{
					"role": "system",
					"content": (
						"Return only one JSON value matching this JSON Schema exactly. "
						"Do not use Markdown fences or add explanatory text.\n"
						'JSON Schema: {"type":"object"}'
					),
				},
				{"role": "system", "content": "system"},
				{"role": "user", "content": "virtual note"},
			],
			"stream": False,
			"temperature": 0,
		})
		self.assertNotIn("think", request["body"])
		self.assertNotIn("format", request["body"])
		self.assertNotIn("options", request["body"])

	def test_requires_safe_https_url_outside_loopback_tests(self) -> None:
		invalid_urls = [
			"http://api.example.com/v1",
			"file:///tmp/api",
			"https://user:password" + "@api.example.com/v1",
			"https://api.example.com/v1?key=value",
			"https://api.example.com/v1#fragment",
			"https://api.example.com/v1\nInjected",
			"https://api.example.com:invalid/v1",
		]
		for url in invalid_urls:
			with self.subTest(url=url), self.assertRaises(ValueError):
				StandardOpenAITransport(url, "key", "model")

		transport = StandardOpenAITransport(
			"https://api.example.com/v1/",
			"key",
			"model",
		)
		self.assertEqual(transport.base_url, "https://api.example.com/v1")

	def test_optional_ca_bundle_creates_verified_ssl_context(self) -> None:
		ca_file = Path("/etc/ssl/cert.pem")
		if not ca_file.exists():
			self.skipTest("system CA bundle is unavailable")

		transport = StandardOpenAITransport(
			"https://api.example.com/v1",
			"key",
			"model",
			ca_bundle_path=str(ca_file),
		)

		self.assertIsNotNone(transport.ssl_context)

	def test_shared_business_adapter_validates_standard_openai_result(self) -> None:
		FakeOpenAIHandler.response_body = {
			"choices": [{
				"message": {
					"content": '{"category":"work","status":"todo","event_time":null}'
				}
			}]
		}
		transport = self.transport()
		adapter = OllamaAdapter(self.base_url, "test-model", transport=transport)

		result = adapter.analyze(
			"虚拟测试笔记",
			"2026-08-27T10:00:00+08:00",
			CATEGORIES,
		)

		self.assertEqual(result.category, "work")
		self.assertEqual(result.status, "todo")
		request = FakeOpenAIHandler.requests[0]
		self.assertEqual(request["body"]["model"], "test-model")
		self.assertNotIn("format", request["body"])

	def test_rejects_header_injection_and_invalid_request_shape(self) -> None:
		with self.assertRaisesRegex(ValueError, "单行"):
			StandardOpenAITransport("https://api.example.com/v1", "key\r\nInjected: yes", "model")
		with self.assertRaisesRegex(ValueError, "统一聊天请求"):
			self.transport().post_json("/arbitrary", {"messages": [{"role": "user", "content": "x"}]})
		with self.assertRaisesRegex(ValueError, "messages"):
			self.transport().post_json("/api/chat", {"messages": []})

	def test_authentication_error_does_not_expose_key_or_response_secret(self) -> None:
		FakeOpenAIHandler.response_status = 401
		FakeOpenAIHandler.response_body = {"error": "rejected test-secret"}

		with self.assertRaises(LLMError) as raised:
			self.transport().post_json(
				"/api/chat",
				{"messages": [{"role": "user", "content": "virtual"}]},
			)

		message = str(raised.exception)
		self.assertIn("检查 API Key", message)
		self.assertNotIn("test-secret", message)

	def test_rate_limit_and_invalid_response_have_clear_errors(self) -> None:
		FakeOpenAIHandler.response_status = 429
		with self.assertRaisesRegex(LLMError, "频繁或额度不足"):
			self.transport().post_json(
				"/api/chat",
				{"messages": [{"role": "user", "content": "virtual"}]},
			)

		FakeOpenAIHandler.response_status = 200
		FakeOpenAIHandler.response_body = {"unexpected": True}
		with self.assertRaisesRegex(LLMError, "Chat Completions"):
			self.transport().post_json(
				"/api/chat",
				{"messages": [{"role": "user", "content": "virtual"}]},
			)

	def test_server_error_cannot_echo_api_key_or_note_content(self) -> None:
		FakeOpenAIHandler.response_status = 500
		FakeOpenAIHandler.response_body = {
			"error": "test-secret virtual-sensitive-note",
		}

		with self.assertRaises(LLMError) as raised:
			self.transport().post_json(
				"/api/chat",
				{"messages": [{"role": "user", "content": "virtual-sensitive-note"}]},
			)

		message = str(raised.exception)
		self.assertIn("HTTP 500", message)
		self.assertNotIn("test-secret", message)
		self.assertNotIn("virtual-sensitive-note", message)

	def test_timeout_has_clear_error(self) -> None:
		with patch("llm_adapter.urlopen", side_effect=TimeoutError), self.assertRaisesRegex(
			LLMError,
			"云端 API 调用超过 5 秒",
		):
			self.transport().post_json(
				"/api/chat",
				{"messages": [{"role": "user", "content": "virtual"}]},
			)


if __name__ == "__main__":
	unittest.main()
