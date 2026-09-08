"""Store model deployment settings and secrets outside the project directory."""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Callable


PUBLIC_PROFILE = "public"
STANDARD_OPENAI = "standard_openai"
VALID_CLOUD_VARIANTS = {STANDARD_OPENAI}
STANDARD_OPENAI_KEYS = {"base_url", "model", "timeout_seconds", "ca_bundle_path"}


def default_config_dir(home: Path | None = None) -> Path:
	base = home if home is not None else Path.home()
	return base / "Library" / "Application Support" / "My Notes Tool Public"


def default_deployment() -> dict[str, Any]:
	return {
		"release_profile": PUBLIC_PROFILE,
		"allowed_cloud_variants": [STANDARD_OPENAI],
	}


def default_model_settings() -> dict[str, Any]:
	return {
		"provider": "ollama",
		"cloud_variant": STANDARD_OPENAI,
		STANDARD_OPENAI: {
			"base_url": "",
			"model": "",
			"timeout_seconds": 120,
			"ca_bundle_path": "",
		},
	}


class ExternalModelConfigStore:
	"""Own local model configuration while never exposing stored API keys."""

	def __init__(
		self,
		config_dir: Path | None = None,
		*,
		project_dir: Path | None = None,
		replacer: Callable[[str | bytes | os.PathLike[str] | os.PathLike[bytes], str | bytes | os.PathLike[str] | os.PathLike[bytes]], None] = os.replace,
	) -> None:
		self.config_dir = (config_dir or default_config_dir()).resolve()
		if project_dir is not None and _is_within(self.config_dir, project_dir.resolve()):
			raise ValueError("模型配置目录不能位于项目目录内")
		self.deployment_path = self.config_dir / "deployment.json"
		self.model_settings_path = self.config_dir / "model-settings.json"
		self.secrets_path = self.config_dir / "secrets.json"
		self.model_history_path = self.config_dir / "model-history.json"
		self.replacer = replacer

	def load_deployment(self) -> dict[str, Any]:
		if not self.deployment_path.exists():
			return default_deployment()
		return validate_deployment(_load_private_json_object(self.deployment_path))

	def save_deployment(self, deployment: dict[str, Any]) -> None:
		_write_private_json(self.deployment_path, validate_deployment(deployment), self.replacer)

	def load_model_settings(self) -> dict[str, Any]:
		if not self.model_settings_path.exists():
			return default_model_settings()
		return validate_model_settings(
			_load_private_json_object(self.model_settings_path),
			self.load_deployment(),
		)

	def save_model_settings(self, settings: dict[str, Any]) -> None:
		validated = validate_model_settings(settings, self.load_deployment())
		_write_private_json(self.model_settings_path, validated, self.replacer)

	def save_configuration(
		self,
		settings: dict[str, Any],
		*,
		api_key: str | None = None,
		clear_api_key: bool = False,
	) -> None:
		"""Save settings plus an optional Secret update, rolling back on failure."""
		validated = validate_model_settings(settings, self.load_deployment())
		if api_key is not None:
			_validate_api_key(api_key)
		if api_key is not None and clear_api_key:
			raise ValueError("不能同时替换和清除 API Key")
		snapshots = {
			self.model_settings_path: _snapshot(self.model_settings_path),
			self.secrets_path: _snapshot(self.secrets_path),
		}
		try:
			_write_private_json(self.model_settings_path, validated, self.replacer)
			if api_key is not None:
				_write_private_json(
					self.secrets_path,
					{"cloud_api_key": api_key},
					self.replacer,
				)
			elif clear_api_key:
				self.clear_api_key()
		except Exception:
			for path, snapshot in snapshots.items():
				_restore_snapshot(path, snapshot)
			raise

	def save_api_key(self, api_key: str) -> None:
		_validate_api_key(api_key)
		_write_private_json(self.secrets_path, {"cloud_api_key": api_key}, self.replacer)

	def clear_api_key(self) -> None:
		if self.secrets_path.exists():
			self.secrets_path.unlink()
			_fsync_directory(self.config_dir)

	def load_api_key(self) -> str:
		if not self.secrets_path.exists():
			raise ValueError("尚未配置云端 API Key")
		value = _load_private_json_object(self.secrets_path).get("cloud_api_key")
		if not isinstance(value, str) or not value:
			raise ValueError("本地 API Key 文件无效")
		return value

	def public_settings(self) -> dict[str, Any]:
		deployment = self.load_deployment()
		settings, configuration_error = self._load_model_settings_for_editing(deployment)
		allowed = deployment["allowed_cloud_variants"]
		visible_settings = {
			variant: settings[variant]
			for variant in allowed
		}
		return {
			"release_profile": deployment["release_profile"],
			"allowed_cloud_variants": list(allowed),
			"provider": settings["provider"],
			"cloud_variant": settings["cloud_variant"],
			"cloud_settings": visible_settings,
			"api_key_configured": self._api_key_configured(),
			"configuration_error": configuration_error,
		}

	def _load_model_settings_for_editing(
		self,
		deployment: dict[str, Any],
	) -> tuple[dict[str, Any], str | None]:
		try:
			return self.load_model_settings(), None
		except ValueError as error:
			raw = _load_private_json_object(self.model_settings_path)
			if raw.get("provider") != "cloud":
				raise
			fallback = dict(raw)
			fallback["provider"] = "ollama"
			validated = validate_model_settings(fallback, deployment)
			validated["provider"] = "cloud"
			return validated, str(error)

	def register_model_backend(self, backend: dict[str, str]) -> None:
		allowed = {"id", "label", "provider", "model"}
		if set(backend) != allowed or any(not isinstance(backend[key], str) or not backend[key] for key in allowed):
			raise ValueError("模型历史条目无效")
		history = self.load_model_history()
		by_id = {item["id"]: item for item in history}
		by_id[backend["id"]] = dict(backend)
		_write_private_json(self.model_history_path, list(by_id.values()), self.replacer)

	def load_model_history(self) -> list[dict[str, str]]:
		if not self.model_history_path.exists():
			return []
		value = _load_private_json_value(self.model_history_path)
		if not isinstance(value, list):
			raise ValueError("model-history.json 顶层必须是数组")
		allowed = {"id", "label", "provider", "model"}
		if any(
			not isinstance(item, dict)
			or set(item) != allowed
			or any(not isinstance(item[key], str) or not item[key] for key in allowed)
			for item in value
		):
			raise ValueError("model-history.json 包含无效条目")
		return [dict(item) for item in value]

	def _api_key_configured(self) -> bool:
		try:
			self.load_api_key()
		except (OSError, ValueError, json.JSONDecodeError):
			return False
		return True


def validate_deployment(value: Any) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise ValueError("deployment.json 顶层必须是对象")
	profile = value.get("release_profile")
	variants = value.get("allowed_cloud_variants")
	if profile != PUBLIC_PROFILE:
		raise ValueError("release_profile 必须是 public")
	if not isinstance(variants, list) or not variants or not all(
		isinstance(variant, str) for variant in variants
	):
		raise ValueError("allowed_cloud_variants 必须是非空字符串数组")
	if len(variants) != len(set(variants)) or not set(variants) <= VALID_CLOUD_VARIANTS:
		raise ValueError("allowed_cloud_variants 包含重复或未知值")
	if STANDARD_OPENAI not in variants:
		raise ValueError("allowed_cloud_variants 必须包含 standard_openai")
	if variants != [STANDARD_OPENAI]:
		raise ValueError("public 模式只允许 standard_openai")
	return {
		"release_profile": profile,
		"allowed_cloud_variants": [STANDARD_OPENAI],
	}


def validate_model_settings(value: Any, deployment: dict[str, Any]) -> dict[str, Any]:
	if not isinstance(value, dict):
		raise ValueError("model-settings.json 顶层必须是对象")
	_validate_exact_keys(
		value,
		{"provider", "cloud_variant", STANDARD_OPENAI},
		"model-settings.json",
	)
	if _contains_secret_field(value):
		raise ValueError("API Key 不能写入 model-settings.json")
	provider = value.get("provider")
	variant = value.get("cloud_variant")
	if provider not in {"ollama", "cloud"}:
		raise ValueError("provider 必须是 ollama 或 cloud")
	if variant not in VALID_CLOUD_VARIANTS:
		raise ValueError("cloud_variant 无效")
	if variant not in deployment["allowed_cloud_variants"]:
		raise ValueError("当前发布模式不允许选择该云端变体")
	standard = value.get(STANDARD_OPENAI)
	if not isinstance(standard, dict):
		raise ValueError("模型设置缺少 standard_openai 配置")
	_validate_exact_keys(standard, STANDARD_OPENAI_KEYS, STANDARD_OPENAI)
	for field in ("base_url", "model", "ca_bundle_path"):
		if not isinstance(standard[field], str):
			raise ValueError(f"standard_openai.{field} 必须是字符串")
	if provider == "cloud":
		for field in ("base_url", "model"):
			if not standard[field].strip():
				raise ValueError(f"standard_openai.{field} 不能为空")
	_validate_timeout(standard["timeout_seconds"], "standard_openai.timeout_seconds")
	return {
		"provider": provider,
		"cloud_variant": variant,
		STANDARD_OPENAI: dict(standard),
	}


def _write_private_json(
	path: Path,
	value: Any,
	replacer: Callable[[str | bytes | os.PathLike[str] | os.PathLike[bytes], str | bytes | os.PathLike[str] | os.PathLike[bytes]], None],
) -> None:
	path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
	os.chmod(path.parent, 0o700)
	temporary_path: Path | None = None
	try:
		file_descriptor, temporary_name = tempfile.mkstemp(
			prefix=f".{path.name}.",
			suffix=".tmp",
			dir=path.parent,
		)
		temporary_path = Path(temporary_name)
		os.fchmod(file_descriptor, 0o600)
		with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as file:
			json.dump(value, file, ensure_ascii=False, indent=2)
			file.write("\n")
			file.flush()
			os.fsync(file.fileno())
		replacer(temporary_path, path)
		os.chmod(path, 0o600)
		_fsync_directory(path.parent)
	finally:
		if temporary_path is not None and temporary_path.exists():
			temporary_path.unlink()


def _load_json_object(path: Path) -> dict[str, Any]:
	with path.open("r", encoding="utf-8") as file:
		value = json.load(file)
	if not isinstance(value, dict):
		raise ValueError(f"{path.name} 顶层必须是对象")
	return value


def _load_private_json_object(path: Path) -> dict[str, Any]:
	value = _load_private_json_value(path)
	if not isinstance(value, dict):
		raise ValueError(f"{path.name} 顶层必须是对象")
	return value


def _load_private_json_value(path: Path) -> Any:
	if path.is_symlink():
		raise ValueError(f"{path.name} 不能是符号链接")
	if path.stat().st_mode & 0o077:
		raise ValueError(f"{path.name} 权限过宽，必须仅当前用户可读写")
	with path.open("r", encoding="utf-8") as file:
		return json.load(file)


def _fsync_directory(path: Path) -> None:
	file_descriptor = os.open(path, os.O_RDONLY)
	try:
		os.fsync(file_descriptor)
	finally:
		os.close(file_descriptor)


def _is_within(path: Path, parent: Path) -> bool:
	try:
		path.relative_to(parent)
	except ValueError:
		return False
	return True


def _contains_secret_field(value: Any) -> bool:
	if isinstance(value, dict):
		return any(
			key in {"api_key", "cloud_api_key"} or _contains_secret_field(item)
			for key, item in value.items()
		)
	if isinstance(value, list):
		return any(_contains_secret_field(item) for item in value)
	return False


def _validate_exact_keys(value: dict[str, Any], expected: set[str], label: str) -> None:
	actual = set(value)
	if actual != expected:
		missing = sorted(expected - actual)
		unexpected = sorted(actual - expected)
		raise ValueError(
			f"{label} 字段不完整或包含未知字段；缺少 {missing}，未知 {unexpected}"
		)


def _validate_timeout(value: Any, label: str) -> None:
	if (
		not isinstance(value, (int, float))
		or isinstance(value, bool)
		or not math.isfinite(value)
		or value <= 0
	):
		raise ValueError(f"{label} 必须大于 0")


def _validate_api_key(api_key: str) -> None:
	if not isinstance(api_key, str) or not api_key or "\r" in api_key or "\n" in api_key:
		raise ValueError("API Key 必须是非空单行字符串")


def _snapshot(path: Path) -> tuple[bytes, int] | None:
	if not path.exists():
		return None
	if path.is_symlink():
		raise ValueError(f"{path.name} 不能是符号链接")
	return path.read_bytes(), path.stat().st_mode & 0o777


def _restore_snapshot(path: Path, snapshot: tuple[bytes, int] | None) -> None:
	if snapshot is None:
		if path.exists() or path.is_symlink():
			path.unlink()
		if path.parent.exists():
			_fsync_directory(path.parent)
		return
	content, mode = snapshot
	path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
	file_descriptor, temporary_name = tempfile.mkstemp(
		prefix=f".{path.name}.restore.",
		suffix=".tmp",
		dir=path.parent,
	)
	temporary_path = Path(temporary_name)
	try:
		os.fchmod(file_descriptor, mode)
		with os.fdopen(file_descriptor, "wb") as file:
			file.write(content)
			file.flush()
			os.fsync(file.fileno())
		os.replace(temporary_path, path)
		_fsync_directory(path.parent)
	finally:
		if temporary_path.exists():
			temporary_path.unlink()