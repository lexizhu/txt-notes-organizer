"""Tests for public project-external model settings and Secret storage."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from model_config import (
	STANDARD_OPENAI,
	ExternalModelConfigStore,
	default_config_dir,
	default_model_settings,
)


class ExternalModelConfigStoreTests(unittest.TestCase):
	def setUp(self) -> None:
		self.temporary_directory = tempfile.TemporaryDirectory()
		self.home = Path(self.temporary_directory.name)
		self.project_dir = self.home / "project"
		self.project_dir.mkdir()
		self.config_dir = default_config_dir(self.home)
		self.store = ExternalModelConfigStore(self.config_dir, project_dir=self.project_dir)

	def tearDown(self) -> None:
		self.temporary_directory.cleanup()

	def test_missing_files_default_to_public_ollama_without_creating_directory(self) -> None:
		self.assertEqual(self.store.load_deployment(), {
			"release_profile": "public",
			"allowed_cloud_variants": [STANDARD_OPENAI],
		})
		self.assertEqual(self.store.load_model_settings()["provider"], "ollama")
		self.assertFalse(self.config_dir.exists())

	def test_only_public_standard_openai_deployment_is_allowed(self) -> None:
		invalid_deployments = [
			{"release_profile": "private", "allowed_cloud_variants": [STANDARD_OPENAI]},
			{"release_profile": "public", "allowed_cloud_variants": ["custom"]},
		]
		for deployment in invalid_deployments:
			with self.subTest(deployment=deployment), self.assertRaises(ValueError):
				self.store.save_deployment(deployment)

	def test_private_files_are_atomic_and_current_user_only(self) -> None:
		self.store.save_deployment({
			"release_profile": "public",
			"allowed_cloud_variants": [STANDARD_OPENAI],
		})
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI].update({
			"base_url": "https://api.example.test/v1",
			"model": "virtual-model",
		})
		self.store.save_model_settings(settings)
		self.store.save_api_key("virtual-secret")

		self.assertEqual(self.config_dir.stat().st_mode & 0o777, 0o700)
		for path in (self.store.deployment_path, self.store.model_settings_path, self.store.secrets_path):
			self.assertEqual(path.stat().st_mode & 0o777, 0o600)
		self.assertEqual(self.store.load_api_key(), "virtual-secret")
		self.assertEqual(list(self.config_dir.glob("*.tmp")), [])

	def test_model_history_is_private_and_contains_no_connection_details(self) -> None:
		backend = {
			"id": "model-backend-v1:" + "a" * 64,
			"label": "云端 advanced-model",
			"provider": "cloud",
			"model": "advanced-model",
		}
		self.store.register_model_backend(backend)

		self.assertEqual(self.store.load_model_history(), [backend])
		self.assertEqual(self.store.model_history_path.stat().st_mode & 0o777, 0o600)
		serialized = self.store.model_history_path.read_text(encoding="utf-8")
		self.assertNotIn("api_key", serialized)
		self.assertNotIn("endpoint", serialized)

	def test_public_settings_never_return_secret(self) -> None:
		self.store.save_model_settings(default_model_settings())
		self.store.save_api_key("virtual-secret")

		public = self.store.public_settings()

		self.assertEqual(public["allowed_cloud_variants"], [STANDARD_OPENAI])
		self.assertEqual(set(public["cloud_settings"]), {STANDARD_OPENAI})
		self.assertTrue(public["api_key_configured"])
		self.assertNotIn("virtual-secret", json.dumps(public))
		self.assertNotIn("cloud_api_key", json.dumps(public))

	def test_model_settings_reject_secret_unknown_and_missing_fields(self) -> None:
		settings = default_model_settings()
		settings[STANDARD_OPENAI]["api_key"] = "must-not-save"
		with self.assertRaisesRegex(ValueError, "不能写入"):
			self.store.save_model_settings(settings)

		settings = default_model_settings()
		settings[STANDARD_OPENAI]["arbitrary"] = "must-not-save"
		with self.assertRaisesRegex(ValueError, "未知"):
			self.store.save_model_settings(settings)

	def test_active_cloud_model_rejects_blank_required_fields(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI]["base_url"] = "https://api.example.test/v1"
		settings[STANDARD_OPENAI]["model"] = "virtual-model"
		for field in ("base_url", "model"):
			invalid = json.loads(json.dumps(settings))
			invalid[STANDARD_OPENAI][field] = "   "
			with self.assertRaisesRegex(ValueError, rf"{field} 不能为空"):
				self.store.save_model_settings(invalid)

	def test_public_settings_exposes_legacy_blank_model_for_correction(self) -> None:
		settings = default_model_settings()
		settings["provider"] = "cloud"
		settings[STANDARD_OPENAI]["base_url"] = "https://api.example.test/v1"
		self.config_dir.mkdir(parents=True, mode=0o700)
		self.store.model_settings_path.write_text(json.dumps(settings), encoding="utf-8")
		os.chmod(self.store.model_settings_path, 0o600)

		with self.assertRaisesRegex(ValueError, "model 不能为空"):
			self.store.load_model_settings()
		public = self.store.public_settings()

		self.assertEqual(public["provider"], "cloud")
		self.assertEqual(public["cloud_settings"][STANDARD_OPENAI]["model"], "")
		self.assertIn("model 不能为空", public["configuration_error"])

		settings = default_model_settings()
		del settings[STANDARD_OPENAI]["timeout_seconds"]
		with self.assertRaisesRegex(ValueError, "缺少"):
			self.store.save_model_settings(settings)

		settings = default_model_settings()
		settings["extra_protocol"] = {"endpoint": "https://example.test"}
		with self.assertRaisesRegex(ValueError, "未知"):
			self.store.save_model_settings(settings)

	def test_failed_replace_preserves_previous_file_and_removes_temp(self) -> None:
		self.store.save_api_key("old-secret")
		before = self.store.secrets_path.read_bytes()

		def fail_replace(source, destination):
			raise OSError("injected replace failure")

		failing_store = ExternalModelConfigStore(
			self.config_dir,
			project_dir=self.project_dir,
			replacer=fail_replace,
		)
		with self.assertRaisesRegex(OSError, "injected"):
			failing_store.save_api_key("new-secret")
		self.assertEqual(self.store.secrets_path.read_bytes(), before)
		self.assertEqual(list(self.config_dir.glob("*.tmp")), [])

	def test_bundle_failure_rolls_back_settings_and_secret(self) -> None:
		self.store.save_model_settings(default_model_settings())
		self.store.save_api_key("old-secret")
		before_settings = self.store.model_settings_path.read_bytes()
		before_secret = self.store.secrets_path.read_bytes()
		call_count = 0

		def fail_second_replace(source, destination):
			nonlocal call_count
			call_count += 1
			if call_count == 2:
				raise OSError("injected second replace failure")
			os.replace(source, destination)

		failing_store = ExternalModelConfigStore(
			self.config_dir,
			project_dir=self.project_dir,
			replacer=fail_second_replace,
		)
		settings = default_model_settings()
		settings[STANDARD_OPENAI]["model"] = "new-model"
		with self.assertRaisesRegex(OSError, "second replace"):
			failing_store.save_configuration(settings, api_key="new-secret")
		self.assertEqual(self.store.model_settings_path.read_bytes(), before_settings)
		self.assertEqual(self.store.secrets_path.read_bytes(), before_secret)

	def test_config_directory_inside_project_is_rejected(self) -> None:
		with self.assertRaisesRegex(ValueError, "不能位于项目目录内"):
			ExternalModelConfigStore(
				self.project_dir / "config-secrets",
				project_dir=self.project_dir,
			)

	def test_clear_api_key_removes_only_secret_file(self) -> None:
		self.store.save_deployment({
			"release_profile": "public",
			"allowed_cloud_variants": [STANDARD_OPENAI],
		})
		self.store.save_api_key("virtual-secret")
		self.store.clear_api_key()
		self.assertFalse(self.store.secrets_path.exists())
		self.assertTrue(self.store.deployment_path.exists())
		self.assertFalse(self.store.public_settings()["api_key_configured"])

	def test_rejects_overly_permissive_or_symlinked_secret_file(self) -> None:
		self.config_dir.mkdir(parents=True)
		self.store.secrets_path.write_text('{"cloud_api_key":"virtual"}', encoding="utf-8")
		os.chmod(self.store.secrets_path, 0o644)
		with self.assertRaisesRegex(ValueError, "权限过宽"):
			self.store.load_api_key()

		self.store.secrets_path.unlink()
		target = self.home / "outside-secret.json"
		target.write_text('{"cloud_api_key":"virtual"}', encoding="utf-8")
		os.chmod(target, 0o600)
		self.store.secrets_path.symlink_to(target)
		with self.assertRaisesRegex(ValueError, "符号链接"):
			self.store.load_api_key()


if __name__ == "__main__":
	unittest.main()
