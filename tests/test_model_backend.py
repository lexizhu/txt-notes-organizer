"""Tests for configured model selection and opaque backend cache identities."""

import tempfile
import unittest
from pathlib import Path

from llm_adapter import OllamaTransport, StandardOpenAITransport
from model_config import (
	STANDARD_OPENAI,
	ExternalModelConfigStore,
	default_model_settings,
)
from organize import (
	create_model_adapter,
	describe_model_backend,
	format_display_entries,
	has_valid_display_cache,
	identify_table_entries,
	organize_entries,
	text_sha256,
)
from llm_adapter import AnalysisResult


PROJECT_SETTINGS = {
	"ollama": {
		"base_url": "http://localhost:11434",
		"model": "qwen3:4b",
		"timeout_seconds": 300,
	}
}
CATEGORIES = [
	{"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}},
	{"code": "other", "labels": {"zh-CN": "其他", "en": "Other"}},
]


class Analyzer:
	def __init__(self) -> None:
		self.calls = []

	def analyze(self, text, record_time, categories):
		self.calls.append(text)
		return AnalysisResult("work", "todo", None)


class Formatter:
	def __init__(self) -> None:
		self.calls = []

	def format_markdown(self, text):
		self.calls.append(text)
		return text


class Identifier:
	def __init__(self) -> None:
		self.calls = []

	def identify_tables(self, text):
		self.calls.append(text)
		return []


class ModelBackendTests(unittest.TestCase):
	def test_result_identity_ignores_connection_settings_but_tracks_model(self) -> None:
		settings = default_model_settings()
		first = describe_model_backend(PROJECT_SETTINGS, settings)
		changed_connection = {
			"ollama": {**PROJECT_SETTINGS["ollama"], "base_url": "http://localhost:9999", "timeout_seconds": 10}
		}
		second = describe_model_backend(changed_connection, settings)
		spaced_model = {"ollama": {**PROJECT_SETTINGS["ollama"], "model": "  qwen3:4b  "}}
		spaced = describe_model_backend(spaced_model, settings)
		changed_model = {"ollama": {**PROJECT_SETTINGS["ollama"], "model": "qwen3:8b"}}
		third = describe_model_backend(changed_model, settings)

		self.assertEqual(first["id"], second["id"])
		self.assertEqual(first["id"], spaced["id"])
		self.assertNotEqual(first["id"], third["id"])

	def setUp(self) -> None:
		self.temporary_directory = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary_directory.name)
		self.project = self.root / "project"
		self.project.mkdir()
		self.store = ExternalModelConfigStore(
			self.root / "external",
			project_dir=self.project,
		)

	def tearDown(self) -> None:
		self.temporary_directory.cleanup()

	def test_missing_external_config_keeps_existing_ollama_backend(self) -> None:
		adapter, fingerprint, label = create_model_adapter(PROJECT_SETTINGS, self.store)

		self.assertIsInstance(adapter.transport, OllamaTransport)
		self.assertEqual(adapter.base_url, "http://localhost:11434")
		self.assertEqual(adapter.model, "qwen3:4b")
		self.assertEqual(adapter.timeout_seconds, 300)
		self.assertEqual(label, "本地 Ollama")
		self.assertRegex(fingerprint, r"^model-backend-v1:[0-9a-f]{64}$")
		self.assertNotIn("qwen3", fingerprint)

	def test_ollama_adapter_rejects_blank_model_and_base_url(self) -> None:
		for field in ("base_url", "model"):
			settings = {"ollama": dict(PROJECT_SETTINGS["ollama"])}
			settings["ollama"][field] = "   "
			with self.assertRaisesRegex(ValueError, "不能为空"):
				create_model_adapter(settings, self.store)

	def test_public_standard_openai_factory_uses_external_secret_without_fingerprint_leak(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings["cloud_variant"] = STANDARD_OPENAI
		settings[STANDARD_OPENAI].update({
			"base_url": "https://api.example.test/v1",
			"model": "virtual-cloud-model",
			"timeout_seconds": 45,
		})
		self.store.save_model_settings(settings)
		self.store.save_api_key("virtual-cloud-secret")

		adapter, fingerprint, label = create_model_adapter(PROJECT_SETTINGS, self.store)

		self.assertIsInstance(adapter.transport, StandardOpenAITransport)
		self.assertEqual(adapter.transport.api_key, "virtual-cloud-secret")
		self.assertEqual(adapter.transport.model, "virtual-cloud-model")
		self.assertEqual(label, "云端 API")
		self.assertNotIn("virtual-cloud-secret", fingerprint)
		self.assertNotIn("api.example.test", fingerprint)
		self.assertNotIn("virtual-cloud-model", fingerprint)

	def test_new_results_store_backend_fingerprints(self) -> None:
		entries_path = self.root / "entries.json"
		backend_id = "model-backend-v1:" + "a" * 64
		entries = []
		captured = [{
			"id": "one",
			"text": "普通正文",
			"record_time": "2026-08-27T10:00:00+08:00",
			"project": "项目甲",
			"source_file": "项目甲.txt",
		}]

		organize_entries(
			captured,
			entries,
			CATEGORIES,
			Analyzer(),
			entries_path,
			backend_id=backend_id,
		)
		format_display_entries(entries, Formatter(), entries_path, backend_id=backend_id)
		identify_table_entries(entries, Identifier(), entries_path, backend_id=backend_id)

		self.assertEqual(entries[0]["analysis_backend"], backend_id)
		self.assertEqual(entries[0]["display_backend"], backend_id)
		self.assertEqual(entries[0]["table_backend"], backend_id)

	def test_backend_switch_does_not_invalidate_existing_valid_cache(self) -> None:
		text = "已有正文"
		entry = {
			"id": "one",
			"text": text,
			"display_markdown": text,
			"display_text_sha256": text_sha256(text),
			"display_generated_at": "2026-08-27T10:00:00+08:00",
			"display_backend": "model-backend-v1:" + "1" * 64,
		}
		formatter = Formatter()
		entries_path = self.root / "entries.json"

		counts = format_display_entries(
			[entry],
			formatter,
			entries_path,
			backend_id="model-backend-v1:" + "2" * 64,
		)

		self.assertTrue(has_valid_display_cache(entry))
		self.assertEqual(counts, (0, 0, 1))
		self.assertEqual(formatter.calls, [])
		self.assertEqual(entry["display_backend"], "model-backend-v1:" + "1" * 64)

	def test_user_edited_analysis_backend_is_not_overwritten(self) -> None:
		backend_id = "model-backend-v1:" + "b" * 64
		entry = {
			"id": "one",
			"text": "旧正文",
			"category": "other",
			"status": "none",
			"event_time": None,
			"user_edited": True,
			"analysis_backend": "model-backend-v1:" + "c" * 64,
		}
		captured = [{
			"id": "one",
			"text": "新正文",
			"record_time": "2026-08-27T10:00:00+08:00",
			"project": "项目甲",
			"source_file": "项目甲.txt",
		}]
		analyzer = Analyzer()

		organize_entries(
			captured,
			[entry],
			CATEGORIES,
			analyzer,
			self.root / "entries.json",
			backend_id=backend_id,
		)

		self.assertEqual(analyzer.calls, [])
		self.assertEqual(entry["analysis_backend"], "model-backend-v1:" + "c" * 64)


if __name__ == "__main__":
	unittest.main()
