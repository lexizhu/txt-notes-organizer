"""Serve the authenticated local UI and coordinate settings, jobs, and diagnostics."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import unquote, urlsplit

from build_html import build_html, validate_entries
from llm_adapter import validate_categories
from llm_adapter import LLMError
from model_config import ExternalModelConfigStore, validate_model_settings
from organize import (
	apply_user_actions,
	create_model_adapter,
	describe_model_backend,
	describe_legacy_model_backend,
	fold_user_actions,
	has_valid_display_cache,
	has_valid_table_cache,
	load_json,
	load_user_actions,
	reprocess_selected_entries,
	save_entries,
	create_cloud_transport,
)
from purge import (
	PurgeArtifacts,
	PurgePaths,
	discover_purge_logs,
	purge_generated_data,
	recover_incomplete_purges,
)
from table_layout import candidate_payload, detect_table_candidates


HOST = "127.0.0.1"
ALLOWED_BROWSER_HOSTS = {"127.0.0.1", "localhost"}
DEFAULT_PORT = 8765
PROJECT_DIR = Path(__file__).resolve().parent
INSTANCE_ID = hashlib.sha256(str(PROJECT_DIR).encode("utf-8")).hexdigest()
REVIEW_HTML_PATH = PROJECT_DIR / "output" / "review.html"
ORGANIZE_LOCK_PATH = PROJECT_DIR / "data" / "organize.lock"
USER_ACTIONS_PATH = PROJECT_DIR / "data" / "user_actions.jsonl"
ENTRIES_PATH = PROJECT_DIR / "data" / "entries.json"
DIAGNOSTICS_PATH = PROJECT_DIR / "data" / "diagnostics.json"
FAVICON_PNG = base64.b64decode(
	"iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAaElEQVR42mNgwAHM4if+"
	"pyZmIAZQ21KSHEMvy7E6gt6WYzhiZDuAkKKbTz+hYHIBTkeMOmBQOgDd0pHnAGKjgJDe4"
	"e+A0VwwmgtGc8FoLhjNBcPDAYTahbR0wGizfPB0zQZF53SguucA3JubhfNBjN4AAAAASU"
	"VORK5CYII="
)


def now_iso() -> str:
	return datetime.now().astimezone().isoformat(timespec="seconds")


def estimate_input_tokens(parts: list[str]) -> dict[str, int | bool]:
	"""Return a deliberately broad mixed Chinese/Latin input-token estimate."""
	text = "".join(parts)
	cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
	other_count = max(0, len(text) - cjk_count)
	return {
		"min": round(cjk_count * 0.8 + other_count / 4),
		"max": round(cjk_count * 1.5 + other_count / 2),
		"approximate": True,
	}


def classify_diagnostic_error(message: str) -> tuple[str, str, str]:
	"""Map raw pipeline output to a stable code and controlled, shareable text."""
	lowered = message.lower()
	if "http 401" in lowered or "http 403" in lowered:
		return "MODEL_AUTH_FAILED", "模型服务认证失败", "检查模型设置中的认证信息后重试。"
	if "http 429" in lowered:
		return "MODEL_RATE_LIMITED", "模型服务暂时限流", "稍后重试，并检查服务额度。"
	if "timeout" in lowered or "超时" in message:
		return "MODEL_TIMEOUT", "模型请求超时", "稍后重试，或适当提高模型超时设置。"
	if "排版" in message:
		return "FORMAT_INVALID", "模型排版结果无效", "正文未改变；日常整理会使用原文显示。"
	if "表格" in message:
		return "TABLE_LAYOUT_INVALID", "表格识别结果无效", "正文未改变；页面会保留原文块。"
	if "json" in lowered:
		return "DATA_OR_RESPONSE_INVALID", "数据或模型响应格式无效", "重试后若仍失败，请复制本诊断摘要。"
	if "permission" in lowered or "权限" in message:
		return "FILE_PERMISSION_DENIED", "本地文件权限不足", "检查工具目录和笔记文件的读写权限。"
	if "生成 html" in lowered or "页面" in message:
		return "PAGE_BUILD_FAILED", "回顾页面生成失败", "现有数据仍保留，请复制本诊断摘要。"
	return "ORGANIZE_FAILED", "整理任务未完成", "重试后若仍失败，请复制本诊断摘要。"


class DiagnosticStore:
	"""Persist a bounded history containing no note text, secrets, URLs, or paths."""

	def __init__(self, path: Path, limit: int = 5) -> None:
		self.path = path
		self.limit = limit
		self.lock = threading.Lock()

	def list(self) -> list[dict[str, Any]]:
		with self.lock:
			return self._load()

	def record(self, job: dict[str, Any]) -> None:
		status = str(job.get("status", "failed"))
		raw_message = str(job.get("diagnostic_error") or job.get("error") or "")
		if status == "failed":
			code, summary, action = classify_diagnostic_error(raw_message)
		elif status == "cancelled":
			code, summary, action = "JOB_CANCELLED", "整理任务已取消", "下次整理会继续未完成部分。"
		elif raw_message:
			status = "succeeded_with_fallback"
			code, summary, action = classify_diagnostic_error(raw_message)
			summary = f"整理完成，但{summary}"
		else:
			code, summary, action = "OK", "整理任务完成", "无需处理。"
		started_at = job.get("started_at")
		finished_at = job.get("finished_at")
		duration_seconds = None
		try:
			if started_at and finished_at:
				duration_seconds = max(0, round((datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds(), 1))
		except (TypeError, ValueError):
			pass
		report = {
			"task_id": job.get("id"),
			"created_at": job.get("created_at"),
			"finished_at": finished_at,
			"status": status,
			"stage": job.get("failure_stage") or ("complete" if status == "succeeded" else status),
			"error_code": code,
			"summary": summary,
			"action": action,
			"duration_seconds": duration_seconds,
			"environment": {"platform": sys.platform, "python": f"{sys.version_info.major}.{sys.version_info.minor}"},
			"privacy": "No note text, secrets, URLs, or full paths included.",
		}
		with self.lock:
			reports = self._load()
			reports.insert(0, report)
			self.path.parent.mkdir(parents=True, exist_ok=True)
			temporary_path = self.path.with_suffix(".tmp")
			temporary_path.write_text(json.dumps(reports[:self.limit], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
			os.chmod(temporary_path, 0o600)
			temporary_path.replace(self.path)

	def _load(self) -> list[dict[str, Any]]:
		try:
			loaded = json.loads(self.path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			return []
		return loaded if isinstance(loaded, list) else []


def progress_from_output(line: str) -> dict[str, Any] | None:
	"""Translate one existing pipeline log line into user-facing progress."""
	if "[ERROR]" in line:
		message = line.split("[ERROR]", 1)[1].strip()
		return progress("failed", "整理失败", message, 100)
	if "步骤 1/3：开始扫描最新笔记" in line:
		return progress("reading", "读取笔记", "正在读取笔记和现有整理结果…", 5)
	match = re.search(r"\[扫描\] 已检查 notes/，发现 (\d+) 条新增", line)
	if match:
		changed_records = int(match.group(1))
		return progress(
			"detecting",
			"检测变化",
			f"发现 {changed_records} 条新增或修改的完整记录。",
			15,
			changed_records,
			changed_records,
		)
	if "[扫描] 已检查 notes/，无新增" in line:
		return progress("detecting", "检测变化", "没有发现新增或修改的完整记录。", 15, 0, 0)
	match = re.search(r"读取到 (\d+) 条原始记录，其中 (\d+) 条需要整理", line)
	if match:
		total_records, changed_records = map(int, match.groups())
		return progress(
			"detecting",
			"检测变化",
			f"检查了 {total_records} 条记录，发现 {changed_records} 条需要整理。",
			15,
			changed_records,
			changed_records,
		)
	match = re.search(r"正在处理第 (\d+)/(\d+) 条", line)
	if match:
		current, total = map(int, match.groups())
		percent = 20 + round(30 * current / max(total, 1))
		return progress(
			"classifying",
			"AI 分类",
			f"正在处理第 {current}/{total} 条。本地 AI 处理较慢，单条通常需要 1-2 分钟。",
			percent,
			current,
			total,
		)
	if "正在调用" in line and ("Ollama" in line or "云端 API" in line or "LLM" in line):
		# The preceding "正在处理第 X/N 条" update already carries truthful
		# item progress. Do not replace it with a generic fixed percentage.
		return None
	match = re.search(r"排版缓存检查：(\d+) 条需要生成或更新", line)
	if match:
		total = int(match.group(1))
		return progress("formatting", "生成排版", f"有 {total} 条记录需要生成或更新排版。", 55, 0, total)
	match = re.search(r"正在排版第 (\d+)/(\d+) 条", line)
	if match:
		current, total = map(int, match.groups())
		percent = 55 + round(15 * current / max(total, 1))
		return progress("formatting", "生成排版", f"正在排版第 {current}/{total} 条。", percent, current, total)
	match = re.search(r"表格坐标缓存检查：(\d+) 条需要生成或更新", line)
	if match:
		total = int(match.group(1))
		return progress("tables", "识别表格", f"有 {total} 条记录需要检查表格。", 72, 0, total)
	match = re.search(r"正在识别表格第 (\d+)/(\d+) 条", line)
	if match:
		current, total = map(int, match.groups())
		percent = 72 + round(13 * current / max(total, 1))
		return progress("tables", "识别表格", f"正在检查第 {current}/{total} 条记录中的表格。", percent, current, total)
	if "步骤 3/3：开始生成 HTML" in line:
		return progress("rebuilding", "重建页面", "正在生成新的回顾页面…", 90)
	if "日常整理流程完成" in line:
		return progress("complete", "整理完成", "全部步骤已完成，页面可以刷新。", 100)
	return None


def progress(
	stage: str,
	label: str,
	message: str,
	percent: int,
	current: int | None = None,
	total: int | None = None,
) -> dict[str, Any]:
	return {
		"stage": stage,
		"label": label,
		"message": message,
		"percent": percent,
		"current": current,
		"total": total,
		"updated_at": now_iso(),
	}


def format_wait_limit(seconds: float) -> str:
	minutes = max(1, round(seconds / 60))
	return f"{minutes} 分钟"


def cancellation_message(wait_seconds: float) -> str:
	return (
		"已请求取消；当前模型调用结束后停止。"
		f"按当前超时设置，从现在起最多约再等 {format_wait_limit(wait_seconds)}，"
		"通常会更快；不会生成新页面。"
	)


def current_model_timeout_seconds(
	model_store: ExternalModelConfigStore,
	project_settings_path: Path = PROJECT_DIR / "config" / "settings.json",
) -> float:
	try:
		model_settings = model_store.load_model_settings()
		if model_settings["provider"] == "cloud":
			variant = model_settings["cloud_variant"]
			value = float(model_settings[variant]["timeout_seconds"])
		else:
			value = float(load_json(project_settings_path)["ollama"]["timeout_seconds"])
		if not math.isfinite(value) or value <= 0:
			raise ValueError("timeout must be positive")
		return value
	except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
		return 300


def run_organize_pipeline(
	report: Callable[[dict[str, Any]], None],
	cancel_file: Path | None = None,
) -> bool:
	"""Run the fixed review pipeline and report parsed progress as it prints."""
	environment = os.environ.copy()
	if cancel_file is not None:
		environment["MY_NOTES_CANCEL_FILE"] = str(cancel_file)
	process = subprocess.Popen(
		[sys.executable, "-u", str(PROJECT_DIR / "review.py")],
		cwd=PROJECT_DIR,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		text=True,
		bufsize=1,
		env=environment,
	)
	if process.stdout is None:
		return False
	for line in process.stdout:
		print(line, end="", flush=True)
		update = progress_from_output(line)
		if update is not None:
			report(update)
	return process.wait() == 0


def run_html_build() -> bool:
	"""Rebuild only the fixed offline HTML after a visibility change."""
	result = subprocess.run(
		[sys.executable, str(PROJECT_DIR / "build_html.py")],
		cwd=PROJECT_DIR,
		text=True,
	)
	return result.returncode == 0


def run_reprocess_pipeline(
	target_ids: set[str] | dict[str, set[str]],
	components: list[str],
	report: Callable[[dict[str, Any]], None],
	model_store: ExternalModelConfigStore,
	entries_path: Path = ENTRIES_PATH,
	settings_path: Path = PROJECT_DIR / "config" / "settings.json",
	categories_path: Path = PROJECT_DIR / "config" / "categories.json",
	html_builder: Callable[[], bool] = run_html_build,
	cancel_check: Callable[[], bool] = lambda: False,
) -> bool:
	"""Reprocess exact existing records with the currently saved model backend."""
	settings = load_json(settings_path)
	categories = validate_categories(load_json(categories_path))
	entries = load_json(entries_path)
	if not isinstance(entries, list):
		raise ValueError("entries.json 的顶层必须是数组")
	analyzer, backend_id, backend_label = create_model_adapter(settings, model_store)
	stage_details = {
		"analysis": ("classifying", "重新分类", 25),
		"display": ("formatting", "重新生成排版", 55),
		"tables": ("tables", "重新识别表格", 75),
	}
	for component in components:
		if cancel_check():
			return False
		component_target_ids = target_ids.get(component, set()) if isinstance(target_ids, dict) else target_ids
		if not component_target_ids:
			continue
		stage, label, percent = stage_details[component]
		report(progress(stage, label, f"正在处理所选记录：{label}。", percent))
		reprocess_selected_entries(
			entries,
			component_target_ids,
			[component],
			categories,
			analyzer,
			entries_path,
			backend_id=backend_id,
			backend_label=backend_label,
			cancel_check=cancel_check,
		)
	if cancel_check():
		return False
	report(progress("rebuilding", "重建页面", "正在生成新的回顾页面…", 90))
	return html_builder()


class JobConflictError(RuntimeError):
	def __init__(self, active_job_id: str | None = None) -> None:
		super().__init__("已有整理任务正在运行")
		self.active_job_id = active_job_id


class JobCancellationTooLateError(RuntimeError):
	pass


class RecordNotFoundError(ValueError):
	pass


class PurgeBusyError(RuntimeError):
	pass


class PurgeManager:
	"""Run a purge only while both organize and watcher scan writes are excluded."""

	def __init__(
		self,
		organize_lock_path: Path,
		scan_lock_path: Path,
		purger: Callable[[str], Any],
	) -> None:
		self.organize_lock_path = organize_lock_path
		self.scan_lock_path = scan_lock_path
		self.purger = purger

	def purge(self, record_id: str) -> dict[str, Any]:
		with _purge_locks(self.organize_lock_path, self.scan_lock_path, blocking=False):
			self.purger(record_id)
		return {"record_id": record_id, "deleted": True}


@contextmanager
def _purge_locks(organize_lock_path: Path, scan_lock_path: Path, *, blocking: bool):
	"""Acquire locks in one fixed order to avoid deadlocks."""
	lock_flag = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
	opened = []
	try:
		for path in (organize_lock_path, scan_lock_path):
			path.parent.mkdir(parents=True, exist_ok=True)
			lock_file = path.open("a+", encoding="utf-8")
			try:
				fcntl.flock(lock_file.fileno(), lock_flag)
			except BlockingIOError as error:
				lock_file.close()
				raise PurgeBusyError("整理或文件扫描正在运行") from error
			opened.append(lock_file)
		yield
	finally:
		for lock_file in reversed(opened):
			fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
			lock_file.close()


def recover_purges_at_startup(
	data_dir: Path,
	organize_lock_path: Path,
	scan_lock_path: Path,
) -> int:
	"""Recover incomplete purge transactions before accepting HTTP requests."""
	with _purge_locks(organize_lock_path, scan_lock_path, blocking=True):
		return recover_incomplete_purges(data_dir)


class UserActionManager:
	"""Persist hide/restore events and refresh their derived projection."""

	def __init__(
		self,
		lock_path: Path,
		actions_path: Path,
		entries_path: Path,
		html_builder: Callable[[], bool] = run_html_build,
	) -> None:
		self.lock_path = lock_path
		self.actions_path = actions_path
		self.entries_path = entries_path
		self.html_builder = html_builder

	def apply(self, record_id: str, event_type: str) -> dict[str, Any]:
		if event_type not in {"hide", "restore"}:
			raise ValueError("不支持的人工操作")
		self.lock_path.parent.mkdir(parents=True, exist_ok=True)
		lock_file = self.lock_path.open("a+", encoding="utf-8")
		try:
			try:
				fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError as error:
				raise JobConflictError() from error
			entries = load_json(self.entries_path)
			if not isinstance(entries, list):
				raise ValueError("entries.json 的顶层必须是数组")
			if not any(
				isinstance(entry, dict) and entry.get("id") == record_id
				for entry in entries
			):
				raise RecordNotFoundError(record_id)
			action = {
				"event_id": str(uuid.uuid4()),
				"event_type": event_type,
				"record_id": record_id,
				"occurred_at": now_iso(),
				"actor": "user",
			}
			self._append(action)
			visibility = fold_user_actions(load_user_actions(self.actions_path))
			apply_user_actions(entries, visibility)
			save_entries(self.entries_path, entries)
			html_updated = self.html_builder()
			hidden_count = sum(entry.get("hidden", False) is True for entry in entries)
			return {
				"record_id": record_id,
				"hidden": event_type == "hide",
				"hidden_count": hidden_count,
				"html_updated": html_updated,
			}
		finally:
			try:
				fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
			finally:
				lock_file.close()

	def _append(self, action: dict[str, Any]) -> None:
		self.actions_path.parent.mkdir(parents=True, exist_ok=True)
		with self.actions_path.open("a", encoding="utf-8") as file:
			file.write(json.dumps(action, ensure_ascii=False, separators=(",", ":")))
			file.write("\n")
			file.flush()
			os.fsync(file.fileno())


def test_cloud_connection(settings: dict[str, Any], api_key: str) -> None:
	"""Test cloud configuration using fixed virtual content only."""
	transport = create_cloud_transport(settings, api_key)
	response = transport.post_json(
		"/api/chat",
		{
			"messages": [
				{
					"role": "system",
					"content": "Return only the requested JSON object.",
				},
				{
					"role": "user",
					"content": 'Return exactly this JSON object: {"connection":"ok"}',
				},
			],
			"options": {"temperature": 0},
		},
	)
	try:
		result = json.loads(response["message"]["content"])
	except (KeyError, TypeError, json.JSONDecodeError) as error:
		raise LLMError("测试响应不是有效 JSON。") from error
	if result != {"connection": "ok"}:
		raise LLMError("测试响应内容不符合预期。")


class ModelSettingsManager:
	"""Read, save, and test model settings while excluding organize jobs."""

	ALLOWED_SAVE_KEYS = {
		"settings",
		"api_key",
		"clear_api_key",
		"privacy_acknowledged",
	}
	ALLOWED_TEST_KEYS = {"settings", "api_key", "privacy_acknowledged"}

	def __init__(
		self,
		store: ExternalModelConfigStore,
		organize_lock_path: Path,
		connection_tester: Callable[[dict[str, Any], str], None] = test_cloud_connection,
		project_settings_path: Path = PROJECT_DIR / "config" / "settings.json",
	) -> None:
		self.store = store
		self.organize_lock_path = organize_lock_path
		self.connection_tester = connection_tester
		self.project_settings_path = project_settings_path
		self.state_lock = threading.Lock()

	def get_public_settings(self) -> dict[str, Any]:
		settings = self.store.public_settings()
		project_settings = load_json(self.project_settings_path)
		ollama = project_settings.get("ollama", {})
		model = ollama.get("model") if isinstance(ollama, dict) else None
		settings["ollama_model"] = model.strip() if isinstance(model, str) and model.strip() else None
		return settings

	def save(self, payload: dict[str, Any]) -> dict[str, Any]:
		self._validate_payload_keys(payload, self.ALLOWED_SAVE_KEYS)
		deployment = self.store.load_deployment()
		settings = validate_model_settings(payload.get("settings"), deployment)
		api_key = payload.get("api_key")
		clear_api_key = payload.get("clear_api_key", False)
		if api_key is not None and not isinstance(api_key, str):
			raise ValueError("api_key 必须是字符串")
		if not isinstance(clear_api_key, bool):
			raise ValueError("clear_api_key 必须是布尔值")
		self._require_privacy_confirmation(settings, payload)
		effective_key = self._effective_key(settings, api_key, clear_api_key)
		if settings["provider"] == "cloud":
			create_cloud_transport(settings, effective_key)
		with self._settings_lock():
			self.store.save_configuration(
				settings,
				api_key=api_key,
				clear_api_key=clear_api_key,
			)
		return self.get_public_settings()

	def test(self, payload: dict[str, Any]) -> dict[str, Any]:
		self._validate_payload_keys(payload, self.ALLOWED_TEST_KEYS)
		settings = validate_model_settings(payload.get("settings"), self.store.load_deployment())
		if settings["provider"] != "cloud":
			raise ValueError("测试连接仅适用于云端 API")
		self._require_privacy_confirmation(settings, payload)
		api_key = payload.get("api_key")
		if api_key is not None and not isinstance(api_key, str):
			raise ValueError("api_key 必须是字符串")
		effective_key = api_key if api_key is not None else self.store.load_api_key()
		create_cloud_transport(settings, effective_key)
		with self._settings_lock():
			try:
				self.connection_tester(settings, effective_key)
			except LLMError as error:
				safe_message = str(error).replace(effective_key, "[REDACTED]")
				raise LLMError(safe_message) from error
		return {
			"connected": True,
			"cloud_variant": settings["cloud_variant"],
		}

	@contextmanager
	def _settings_lock(self):
		with self.state_lock:
			self.organize_lock_path.parent.mkdir(parents=True, exist_ok=True)
			with self.organize_lock_path.open("a+", encoding="utf-8") as lock_file:
				try:
					fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
				except BlockingIOError as error:
					raise JobConflictError() from error
				try:
					yield
				finally:
					fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

	@staticmethod
	def _validate_payload_keys(payload: dict[str, Any], allowed: set[str]) -> None:
		if not isinstance(payload, dict):
			raise ValueError("请求内容必须是 JSON 对象")
		unexpected = payload.keys() - allowed
		if unexpected:
			raise ValueError(f"请求包含不支持的字段：{', '.join(sorted(unexpected))}")

	@staticmethod
	def _require_privacy_confirmation(
		settings: dict[str, Any],
		payload: dict[str, Any],
	) -> None:
		if settings["provider"] == "cloud" and payload.get("privacy_acknowledged") is not True:
			raise ValueError("使用云端 API 前必须确认笔记内容会发送到该服务")

	def _effective_key(
		self,
		settings: dict[str, Any],
		api_key: str | None,
		clear_api_key: bool,
	) -> str:
		if api_key is not None and clear_api_key:
			raise ValueError("不能同时替换和清除 API Key")
		if settings["provider"] != "cloud":
			return ""
		if clear_api_key:
			raise ValueError("使用云端 API 时不能清除 API Key")
		return api_key if api_key is not None else self.store.load_api_key()


class ReprocessPlanManager:
	"""Build a read-only plan for explicitly reprocessing existing entries."""

	COMPONENTS = ("analysis", "display", "tables")
	SCOPE_TYPES = {"all", "project", "records", "filters"}

	def __init__(
		self,
		entries_path: Path,
		organize_lock_path: Path,
		model_store: ExternalModelConfigStore,
	) -> None:
		self.entries_path = entries_path
		self.organize_lock_path = organize_lock_path
		self.model_store = model_store

	def plan(self, payload: dict[str, Any]) -> dict[str, Any]:
		with self._data_lock():
			plan, _ = self.resolve_unlocked(payload)
		return plan

	def options(self) -> dict[str, Any]:
		with self._data_lock():
			entries = load_json(self.entries_path)
			if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
				raise RuntimeError("entries.json 不是有效的记录数组")
			project_settings = load_json(PROJECT_DIR / "config" / "settings.json")
			model_settings = self.model_store.load_model_settings()
			current = describe_model_backend(project_settings, model_settings)
			legacy = describe_legacy_model_backend(project_settings, model_settings)
			try:
				history = {item["id"]: item for item in self.model_store.load_model_history()}
			except (OSError, ValueError, json.JSONDecodeError):
				history = {}
			current_ids = {current["id"], legacy["id"]}
			counts: dict[str, int] = {}
			for entry in entries:
				backend_id = entry.get("analysis_backend")
				key = backend_id if isinstance(backend_id, str) and backend_id else "__missing__"
				counts[key] = counts.get(key, 0) + 1
			sources = []
			for backend_id, count in counts.items():
				known = history.get(backend_id)
				sources.append({
					"id": backend_id,
					"label": (
						"缺少整理来源" if backend_id == "__missing__"
						else current["label"] if backend_id in current_ids
						else known["label"] if known
						else f"历史模型 · {backend_id[-6:]}"
					),
					"count": count,
					"current": backend_id in current_ids,
				})
			if not current_ids & counts.keys():
				sources.append({"id": current["id"], "label": current["label"], "count": 0, "current": True})
			dates = []
			for entry in entries:
				try:
					dates.append(datetime.fromisoformat(str(entry.get("record_time", "")).replace("Z", "+00:00")).date().isoformat())
				except ValueError:
					pass
			return {
				"projects": sorted({str(entry.get("project")) for entry in entries if entry.get("project")}),
				"record_date_min": min(dates) if dates else None,
				"record_date_max": max(dates) if dates else None,
				"sources": sorted(sources, key=lambda item: (not item["current"], item["label"])),
				"current_backend_id": current["id"],
			}

	def resolve_unlocked(self, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, set[str]]]:
		if not isinstance(payload, dict):
			raise ValueError("请求内容必须是 JSON 对象")
		unexpected = payload.keys() - {
			"scope", "components", "include_hidden", "mode", "source_backend_ids", "allow_current_backend"
		}
		if unexpected:
			raise ValueError(f"请求包含不支持的字段：{', '.join(sorted(unexpected))}")
		scope = self._validate_scope(payload.get("scope"))
		mode = payload.get("mode", "legacy")
		if mode not in {"legacy", "full", "repair"}:
			raise ValueError("mode 必须是 full 或 repair")
		components = self._validate_components(payload.get("components")) if mode == "legacy" else ["analysis", "display", "tables"]
		include_hidden = payload.get("include_hidden", False)
		if not isinstance(include_hidden, bool):
			raise ValueError("include_hidden 必须是布尔值")

		entries = load_json(self.entries_path)
		if not isinstance(entries, list) or not all(isinstance(entry, dict) for entry in entries):
			raise RuntimeError("entries.json 不是有效的记录数组")
		matched = self._select_entries(entries, scope)
		selected = matched if scope["type"] == "filters" else [
			entry for entry in matched if include_hidden or entry.get("hidden") is not True
		]
		project_settings = load_json(PROJECT_DIR / "config" / "settings.json")
		model_settings = self.model_store.load_model_settings()
		current = describe_model_backend(project_settings, model_settings)
		legacy = describe_legacy_model_backend(project_settings, model_settings)
		if mode == "full":
			source_ids = payload.get("source_backend_ids")
			if not isinstance(source_ids, list) or not source_ids or any(not isinstance(item, str) or not item for item in source_ids):
				raise ValueError("完整重新整理必须选择至少一个原整理来源")
			if set(source_ids) & {current["id"], legacy["id"]} and payload.get("allow_current_backend") is not True:
				raise ValueError("当前模型结果默认不能重新处理")
			source_set = set(source_ids)
			selected = [entry for entry in selected if (entry.get("analysis_backend") or "__missing__") in source_set]
		if mode == "repair":
			display_targets = {
				str(entry["id"]) for entry in selected
				if isinstance(entry.get("id"), str)
				and (not has_valid_display_cache(entry) or not isinstance(entry.get("display_backend"), str))
			}
			table_targets = {
				str(entry["id"]) for entry in selected
				if isinstance(entry.get("id"), str)
				and bool(detect_table_candidates(str(entry.get("text", ""))))
				and (not has_valid_table_cache(entry) or not isinstance(entry.get("table_backend"), str))
			}
			target_ids_by_component = {"analysis": set(), "display": display_targets, "tables": table_targets}
			selected = [entry for entry in selected if entry.get("id") in display_targets | table_targets]
		else:
			selected_ids = {str(entry["id"]) for entry in selected if isinstance(entry.get("id"), str)}
			target_ids_by_component = {
				"analysis": {str(entry["id"]) for entry in selected if isinstance(entry.get("id"), str) and entry.get("user_edited") is not True} if "analysis" in components else set(),
				"display": set(selected_ids) if "display" in components else set(),
				"tables": set(selected_ids) if "tables" in components else set(),
			}
		analysis_count = len(target_ids_by_component["analysis"])
		protected_count = len(selected) - analysis_count if mode in {"legacy", "full"} and "analysis" in components else 0
		table_model_calls = sum(
			bool(detect_table_candidates(str(entry.get("text", ""))))
			for entry in selected
			if str(entry.get("id")) in target_ids_by_component["tables"]
		)
		work_items = {
			"analysis": analysis_count if "analysis" in components else 0,
			"display": len(target_ids_by_component["display"]),
			"tables": len(target_ids_by_component["tables"]),
		}
		model_settings = self.model_store.load_model_settings()
		provider = model_settings["provider"]
		plan_snapshot = {
			"mode": mode,
			"current_backend_id": current["id"],
			"scope": scope,
			"components": components,
			"include_hidden": include_hidden,
			"targets": {
				component: sorted(target_ids_by_component[component])
				for component in self.COMPONENTS
			},
			"entries": [
				{
					"id": entry.get("id"),
					"text": entry.get("text"),
					"project": entry.get("project"),
					"hidden": entry.get("hidden") is True,
					"user_edited": entry.get("user_edited") is True,
					"updated_at": entry.get("updated_at"),
					"analysis_backend": entry.get("analysis_backend"),
					"display_generated_at": entry.get("display_generated_at"),
					"display_backend": entry.get("display_backend"),
					"display_tables_generated_at": entry.get("display_tables_generated_at"),
					"table_backend": entry.get("table_backend"),
				}
				for entry in selected
			],
			"model_settings": model_settings,
		}
		plan_id = hashlib.sha256(
			json.dumps(
				plan_snapshot,
				ensure_ascii=False,
				sort_keys=True,
				separators=(",", ":"),
			).encode("utf-8")
		).hexdigest()

		plan = {
			"mode": mode,
			"plan_id": f"reprocess-plan-v1:{plan_id}",
			"scope": scope,
			"components": components,
			"include_hidden": include_hidden,
			"matched_records": len(matched),
			"selected_records": len(selected),
			"skipped_hidden": len(matched) - len(selected),
			"protected_user_edits": protected_count,
			"work_items": work_items,
			"estimated_model_calls": (
				work_items["analysis"] + work_items["display"] + table_model_calls
			),
			"estimated_table_model_calls": table_model_calls,
			"uses_cloud": provider == "cloud",
			"has_work": sum(work_items.values()) > 0,
			"requires_confirmation": True,
		}
		input_parts = []
		for entry in selected:
			entry_id = str(entry.get("id"))
			text = str(entry.get("text", ""))
			if entry_id in target_ids_by_component["analysis"]:
				input_parts.append(text)
			if entry_id in target_ids_by_component["display"]:
				input_parts.append(text)
			if entry_id in target_ids_by_component["tables"]:
				input_parts.append(json.dumps(candidate_payload(detect_table_candidates(text)), ensure_ascii=False))
		plan["estimated_input_characters"] = sum(len(part) for part in input_parts)
		plan["estimated_input_tokens"] = estimate_input_tokens(input_parts)
		plan["estimate_scope"] = "content_only"
		return plan, target_ids_by_component

	@classmethod
	def _validate_components(cls, value: Any) -> list[str]:
		if not isinstance(value, list) or not value:
			raise ValueError("components 必须是非空数组")
		if any(not isinstance(component, str) or component not in cls.COMPONENTS for component in value):
			raise ValueError("components 包含未知处理类型")
		if len(set(value)) != len(value):
			raise ValueError("components 不能重复")
		return [component for component in cls.COMPONENTS if component in value]

	@classmethod
	def _validate_scope(cls, value: Any) -> dict[str, Any]:
		if not isinstance(value, dict) or value.get("type") not in cls.SCOPE_TYPES:
			raise ValueError("scope.type 必须是 all、project、records 或 filters")
		scope_type = value["type"]
		allowed = {"type"}
		if scope_type == "project":
			allowed.add("project")
			if not isinstance(value.get("project"), str) or not value["project"].strip():
				raise ValueError("project 范围必须提供项目名称")
		if scope_type == "records":
			allowed.add("record_ids")
			record_ids = value.get("record_ids")
			if (
				not isinstance(record_ids, list)
				or not record_ids
				or any(not isinstance(record_id, str) or not record_id for record_id in record_ids)
				or len(set(record_ids)) != len(record_ids)
			):
				raise ValueError("records 范围必须提供不重复的记录 ID")
		if scope_type == "filters":
			allowed.update({"projects", "record_date_from", "record_date_to", "visibility"})
			projects = value.get("projects")
			if (
				not isinstance(projects, list)
				or not projects
				or any(not isinstance(project, str) or not project for project in projects)
				or len(set(projects)) != len(projects)
			):
				raise ValueError("filters 范围必须提供不重复的项目名称")
			for key in ("record_date_from", "record_date_to"):
				date_value = value.get(key)
				if date_value is not None:
					if not isinstance(date_value, str):
						raise ValueError("记录日期必须是 YYYY-MM-DD 或 null")
					try:
						parsed_date = datetime.strptime(date_value, "%Y-%m-%d")
					except ValueError as error:
						raise ValueError("记录日期必须是有效的 YYYY-MM-DD") from error
					if parsed_date.strftime("%Y-%m-%d") != date_value:
						raise ValueError("记录日期必须是有效的 YYYY-MM-DD")
			if value.get("record_date_from") and value.get("record_date_to"):
				if value["record_date_from"] > value["record_date_to"]:
					raise ValueError("记录开始日期不能晚于结束日期")
			if value.get("visibility") not in {"visible", "all", "hidden"}:
				raise ValueError("visibility 必须是 visible、all 或 hidden")
		if value.keys() - allowed:
			raise ValueError("scope 包含不支持的字段")
		return dict(value)

	@staticmethod
	def _select_entries(entries: list[dict[str, Any]], scope: dict[str, Any]) -> list[dict[str, Any]]:
		if scope["type"] == "all":
			return entries
		if scope["type"] == "project":
			return [entry for entry in entries if entry.get("project") == scope["project"]]
		if scope["type"] == "filters":
			projects = set(scope["projects"])
			start = scope.get("record_date_from")
			end = scope.get("record_date_to")
			visibility = scope["visibility"]
			selected = []
			for entry in entries:
				if entry.get("project") not in projects:
					continue
				hidden = entry.get("hidden") is True
				if visibility == "visible" and hidden:
					continue
				if visibility == "hidden" and not hidden:
					continue
				try:
					record_date = datetime.fromisoformat(
						str(entry.get("record_time", "")).replace("Z", "+00:00")
					).date().isoformat()
				except ValueError:
					continue
				if start and record_date < start:
					continue
				if end and record_date > end:
					continue
				selected.append(entry)
			return selected
		requested = set(scope["record_ids"])
		found = {entry.get("id") for entry in entries if entry.get("id") in requested}
		if found != requested:
			raise ValueError("records 范围包含不存在的记录 ID")
		return [entry for entry in entries if entry.get("id") in requested]

	@contextmanager
	def _data_lock(self):
		self.organize_lock_path.parent.mkdir(parents=True, exist_ok=True)
		with self.organize_lock_path.open("a+", encoding="utf-8") as lock_file:
			try:
				fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
			except BlockingIOError as error:
				raise JobConflictError() from error
			try:
				yield
			finally:
				fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class ReprocessPlanChangedError(RuntimeError):
	pass


class ReprocessJobManager:
	"""Run one confirmed reprocessing task while excluding normal organize work."""

	ALLOWED_KEYS = {
		"plan_id",
		"scope",
		"components",
		"include_hidden",
		"confirmed",
		"cloud_acknowledged",
		"mode",
		"source_backend_ids",
		"allow_current_backend",
	}

	def __init__(
		self,
		plan_manager: ReprocessPlanManager,
		lock_path: Path,
		executor: Callable[
			[set[str] | dict[str, set[str]], list[str], Callable[[dict[str, Any]], None], Callable[[], bool]],
			bool,
		],
		cancel_wait_seconds_provider: Callable[[], float] = lambda: 300,
		diagnostic_store: DiagnosticStore | None = None,
	) -> None:
		self.plan_manager = plan_manager
		self.lock_path = lock_path
		self.executor = executor
		self.cancel_wait_seconds_provider = cancel_wait_seconds_provider
		self.diagnostic_store = diagnostic_store or DiagnosticStore(lock_path.parent / "diagnostics.json")
		self.state_lock = threading.Lock()
		self.jobs: dict[str, dict[str, Any]] = {}
		self.active_job_id: str | None = None
		self.consumed_plan_ids: set[str] = set()
		self.cancel_events: dict[str, threading.Event] = {}

	def start(self, payload: dict[str, Any]) -> dict[str, Any]:
		if not isinstance(payload, dict) or payload.keys() - self.ALLOWED_KEYS:
			raise ValueError("重处理请求包含不支持的字段")
		if payload.get("confirmed") is not True:
			raise ValueError("必须明确确认重新处理")
		plan_id = payload.get("plan_id")
		if not isinstance(plan_id, str) or not plan_id.startswith("reprocess-plan-v1:"):
			raise ValueError("缺少有效的计划 ID")
		plan_payload = {
			"scope": payload.get("scope"),
			"components": payload.get("components"),
			"include_hidden": payload.get("include_hidden", False),
			"mode": payload.get("mode", "legacy"),
			"source_backend_ids": payload.get("source_backend_ids"),
			"allow_current_backend": payload.get("allow_current_backend", False),
		}
		with self.state_lock:
			if plan_id in self.consumed_plan_ids:
				raise ReprocessPlanChangedError("该计划已经使用，请重新计算")
			if self.active_job_id is not None:
				raise JobConflictError(self.active_job_id)
			self.lock_path.parent.mkdir(parents=True, exist_ok=True)
			lock_file = self.lock_path.open("a+", encoding="utf-8")
			try:
				fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError as error:
				lock_file.close()
				raise JobConflictError() from error
			try:
				plan, target_ids = self.plan_manager.resolve_unlocked(plan_payload)
				if plan["plan_id"] != plan_id:
					raise ReprocessPlanChangedError("计划生成后数据或模型设置已变化")
				if not plan["has_work"]:
					raise ValueError("没有可重新处理的记录")
				if plan["uses_cloud"] and payload.get("cloud_acknowledged") is not True:
					raise ValueError("云端重处理前必须再次确认隐私和费用风险")
			except Exception:
				fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
				lock_file.close()
				raise
			job_id = str(uuid.uuid4())
			self.consumed_plan_ids.add(plan_id)
			job = {
				"id": job_id,
				"kind": "reprocess",
				"status": "queued",
				"cancel_requested": False,
				"cancel_wait_seconds": float(self.cancel_wait_seconds_provider()),
				"created_at": now_iso(),
				"plan": plan,
				"progress": progress("starting", "准备重新处理", "正在启动一次性任务…", 1),
			}
			self.jobs[job_id] = job
			self.cancel_events[job_id] = threading.Event()
			self.active_job_id = job_id
			thread = threading.Thread(
				target=self._run,
				args=(job_id, lock_file, target_ids, plan["components"]),
				name=f"reprocess-{job_id}",
				daemon=True,
			)
			thread.start()
			return dict(job)

	def get(self, job_id: str) -> dict[str, Any] | None:
		with self.state_lock:
			job = self.jobs.get(job_id)
			return dict(job) if job is not None else None

	def cancel(self, job_id: str) -> dict[str, Any]:
		with self.state_lock:
			job = self.jobs.get(job_id)
			if job is None:
				raise KeyError(job_id)
			if job["status"] in {"succeeded", "failed", "cancelled"}:
				return dict(job)
			if job["progress"].get("stage") == "rebuilding":
				raise JobCancellationTooLateError("页面重建已经开始")
			job["cancel_requested"] = True
			job["status"] = "cancelling"
			job["progress"] = progress(
				"cancelling", "正在取消",
				cancellation_message(job["cancel_wait_seconds"]),
				job["progress"].get("percent", 0),
			)
			self.cancel_events[job_id].set()
			return dict(job)

	def _run(
		self,
		job_id: str,
		lock_file: Any,
		target_ids: set[str] | dict[str, set[str]],
		components: list[str],
	) -> None:
		lock_released = False
		with self.state_lock:
			if not self.jobs[job_id]["cancel_requested"]:
				self.jobs[job_id]["status"] = "running"
			self.jobs[job_id]["started_at"] = now_iso()
		try:
			try:
				succeeded = self.executor(
					target_ids,
					components,
					lambda update: self._report(job_id, update),
					self.cancel_events[job_id].is_set,
				)
			except Exception:
				succeeded = False
			fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
			lock_file.close()
			lock_released = True
			with self.state_lock:
				job = self.jobs[job_id]
				last_stage = job["progress"].get("stage")
				cancelled = job.get("cancel_requested", False)
				job["status"] = "cancelled" if cancelled else "succeeded" if succeeded else "failed"
				job["finished_at"] = now_iso()
				if cancelled:
					job["progress"] = progress(
						"cancelled", "重新处理已取消", "未开始后续处理，也未生成新页面。", 100
					)
				else:
					if not succeeded:
						job["error"] = job.get("diagnostic_error") or "重新处理未完成，未发布新页面。"
						job["failure_stage"] = job.get("diagnostic_stage") or last_stage
					job["progress"] = progress(
						"complete" if succeeded else "failed",
						"重新处理完成" if succeeded else "重新处理失败",
						"所选记录已重新处理。" if succeeded else "重新处理未完成，未发布新页面。",
						100,
					)
				self.active_job_id = None
				finished_job = dict(job)
			try:
				self.diagnostic_store.record(finished_job)
			except OSError:
				pass
		finally:
			with self.state_lock:
				self.cancel_events.pop(job_id, None)
			if not lock_released:
				fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
				lock_file.close()

	def _report(self, job_id: str, update: dict[str, Any]) -> None:
		with self.state_lock:
			if job_id in self.jobs and not self.jobs[job_id].get("cancel_requested", False):
				if update.get("stage") == "failed" and "diagnostic_error" not in self.jobs[job_id]:
					self.jobs[job_id]["diagnostic_error"] = update.get("message")
					self.jobs[job_id]["diagnostic_stage"] = "reprocess"
				self.jobs[job_id]["progress"] = dict(update)


class OrganizeJobManager:
	"""Run one asynchronous pipeline at a time, including across processes."""

	def __init__(
		self,
		lock_path: Path,
		pipeline: Callable[[Callable[[dict[str, Any]], None]], bool] = run_organize_pipeline,
		cancel_wait_seconds_provider: Callable[[], float] = lambda: 300,
		diagnostic_store: DiagnosticStore | None = None,
	) -> None:
		self.lock_path = lock_path
		self.pipeline = pipeline
		self.cancel_wait_seconds_provider = cancel_wait_seconds_provider
		self.diagnostic_store = diagnostic_store or DiagnosticStore(lock_path.parent / "diagnostics.json")
		self.jobs: dict[str, dict[str, Any]] = {}
		self.active_job_id: str | None = None
		self.state_lock = threading.Lock()

	def start(self) -> dict[str, Any]:
		with self.state_lock:
			if self.active_job_id is not None:
				raise JobConflictError(self.active_job_id)
			self.lock_path.parent.mkdir(parents=True, exist_ok=True)
			lock_file = self.lock_path.open("a+", encoding="utf-8")
			try:
				fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
			except BlockingIOError as error:
				lock_file.close()
				raise JobConflictError() from error

			job_id = str(uuid.uuid4())
			cancel_file = self.lock_path.parent / f".organize-cancel-{job_id}"
			cancel_file.unlink(missing_ok=True)
			job = {
				"id": job_id,
				"status": "queued",
				"created_at": now_iso(),
				"started_at": None,
				"finished_at": None,
				"error": None,
				"cancel_requested": False,
				"cancel_wait_seconds": float(self.cancel_wait_seconds_provider()),
				"progress": progress("queued", "等待开始", "整理任务已创建。", 0),
			}
			self.jobs[job_id] = job
			self.active_job_id = job_id
			thread = threading.Thread(
				target=self._run,
				args=(job_id, lock_file, cancel_file),
				name=f"organize-{job_id}",
				daemon=True,
			)
			thread.start()
			return dict(job)

	def get(self, job_id: str) -> dict[str, Any] | None:
		with self.state_lock:
			job = self.jobs.get(job_id)
			return dict(job) if job is not None else None

	def cancel(self, job_id: str) -> dict[str, Any]:
		with self.state_lock:
			job = self.jobs.get(job_id)
			if job is None:
				raise KeyError(job_id)
			if job["status"] in {"succeeded", "failed", "cancelled"}:
				return dict(job)
			if job["progress"].get("stage") == "rebuilding":
				raise JobCancellationTooLateError("页面重建已经开始")
			cancel_file = self.lock_path.parent / f".organize-cancel-{job_id}"
			cancel_file.touch(mode=0o600, exist_ok=True)
			job["cancel_requested"] = True
			job["status"] = "cancelling"
			job["progress"] = progress(
				"cancelling", "正在取消",
				cancellation_message(job["cancel_wait_seconds"]),
				job["progress"].get("percent", 0),
			)
			return dict(job)

	def _run(self, job_id: str, lock_file: Any, cancel_file: Path) -> None:
		lock_released = False
		with self.state_lock:
			if not self.jobs[job_id]["cancel_requested"]:
				self.jobs[job_id]["status"] = "running"
				self.jobs[job_id]["progress"] = progress(
					"starting", "启动整理", "正在启动整理程序…", 2
				)
			self.jobs[job_id]["started_at"] = now_iso()
		try:
			try:
				if self.pipeline is run_organize_pipeline:
					succeeded = self.pipeline(lambda update: self._report(job_id, update), cancel_file)
				else:
					succeeded = self.pipeline(lambda update: self._report(job_id, update))
			except Exception:
				succeeded = False
			cancellation_marker_exists = cancel_file.exists()
			cancel_file.unlink(missing_ok=True)
			fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
			lock_file.close()
			lock_released = True
			with self.state_lock:
				job = self.jobs[job_id]
				last_stage = job["progress"].get("stage")
				cancelled = job["cancel_requested"] or cancellation_marker_exists
				job["status"] = "cancelled" if cancelled else "succeeded" if succeeded else "failed"
				job["finished_at"] = now_iso()
				if cancelled:
					job["error"] = None
					job["progress"] = progress(
						"cancelled", "整理已取消", "未开始后续处理，也未生成新页面。", 100
					)
				elif succeeded:
					job["progress"] = progress(
						"complete", "整理完成", "全部步骤已完成，页面可以刷新。", 100
					)
				else:
					last_message = job["progress"].get("message")
					job["error"] = last_message or "整理失败，请查看本地服务输出。"
					job["failure_stage"] = job.get("diagnostic_stage") or last_stage
					job["progress"] = progress(
						"failed", "整理失败", job["error"], job["progress"].get("percent", 0)
					)
				self.active_job_id = None
				finished_job = dict(job)
			try:
				self.diagnostic_store.record(finished_job)
			except OSError:
				pass
		finally:
			cancel_file.unlink(missing_ok=True)
			if not lock_released:
				fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
				lock_file.close()

	def _report(self, job_id: str, update: dict[str, Any]) -> None:
		with self.state_lock:
			if job_id in self.jobs and not self.jobs[job_id].get("cancel_requested", False):
				if update.get("stage") == "failed" and "diagnostic_error" not in self.jobs[job_id]:
					self.jobs[job_id]["diagnostic_error"] = update.get("message")
					message = str(update.get("message") or "")
					self.jobs[job_id]["diagnostic_stage"] = (
						"formatting" if "排版" in message
						else "tables" if "表格" in message
						else "pipeline"
					)
				self.jobs[job_id]["progress"] = dict(update)


class LocalHTTPServer(ThreadingHTTPServer):
	daemon_threads = True

	def __init__(
		self,
		server_address: tuple[str, int],
		handler_class: type[BaseHTTPRequestHandler],
		job_manager: OrganizeJobManager,
		action_manager: UserActionManager,
		purge_manager: PurgeManager | None,
		model_settings_manager: ModelSettingsManager | None,
		reprocess_plan_manager: ReprocessPlanManager | None,
		reprocess_job_manager: ReprocessJobManager | None,
		review_html_path: Path,
		token: str,
	) -> None:
		super().__init__(server_address, handler_class)
		self.job_manager = job_manager
		self.action_manager = action_manager
		self.purge_manager = purge_manager
		self.model_settings_manager = model_settings_manager
		self.reprocess_plan_manager = reprocess_plan_manager
		self.reprocess_job_manager = reprocess_job_manager
		self.review_html_path = review_html_path
		self.token = token


class LocalRequestHandler(BaseHTTPRequestHandler):
	server: LocalHTTPServer
	protocol_version = "HTTP/1.1"

	def do_GET(self) -> None:
		if not self._valid_host():
			self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_host"})
			return
		path = urlsplit(self.path).path
		if path == "/":
			self._serve_review()
			return
		if path == "/favicon.ico":
			self._bytes(HTTPStatus.OK, FAVICON_PNG, "image/png")
			return
		if path == "/api/health":
			self._json(HTTPStatus.OK, {"status": "ok", "instance_id": INSTANCE_ID})
			return
		if path == "/api/session":
			if not self._valid_read_origin():
				self._json(HTTPStatus.FORBIDDEN, {"error": "invalid_origin"})
				return
			self._json(HTTPStatus.OK, {
				"token": self.server.token,
				"capabilities": {
					"purge": self.server.purge_manager is not None,
					"model_settings": self.server.model_settings_manager is not None,
					"reprocess_plan": self.server.reprocess_plan_manager is not None,
					"reprocess_execute": self.server.reprocess_job_manager is not None,
					"diagnostics": True,
				},
			})
			return
		if path == "/api/diagnostics":
			if not self._valid_read_origin():
				self._json(HTTPStatus.FORBIDDEN, {"error": "invalid_origin"})
				return
			self._json(HTTPStatus.OK, {"reports": self.server.job_manager.diagnostic_store.list()})
			return
		if path == "/api/model-settings":
			if not self._valid_read_origin():
				self._json(HTTPStatus.FORBIDDEN, {"error": "invalid_origin"})
				return
			manager = self.server.model_settings_manager
			if manager is None:
				self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "model_settings_not_available"})
				return
			try:
				settings = manager.get_public_settings()
			except (OSError, ValueError, json.JSONDecodeError):
				self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "model_settings_failed"})
				return
			self._json(HTTPStatus.OK, settings)
			return
		if path == "/api/reprocess/options":
			if not self._valid_read_origin():
				self._json(HTTPStatus.FORBIDDEN, {"error": "invalid_origin"})
				return
			manager = self.server.reprocess_plan_manager
			if manager is None:
				self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "reprocess_not_available"})
				return
			try:
				options = manager.options()
			except (OSError, ValueError, RuntimeError, json.JSONDecodeError):
				self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "reprocess_options_failed"})
				return
			self._json(HTTPStatus.OK, options)
			return
		prefix = "/api/jobs/"
		if path.startswith(prefix) and path.count("/") == 3:
			job = self.server.job_manager.get(path.removeprefix(prefix))
			if job is None and self.server.reprocess_job_manager is not None:
				job = self.server.reprocess_job_manager.get(path.removeprefix(prefix))
			if job is None:
				self._json(HTTPStatus.NOT_FOUND, {"error": "job_not_found"})
			else:
				self._json(HTTPStatus.OK, {"job": job})
			return
		self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

	def do_POST(self) -> None:
		if not self._valid_host():
			self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_host"})
			return
		parsed_path = urlsplit(self.path)
		if parsed_path.query:
			self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
			return
		if not self._valid_write_request():
			self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
			return
		if parsed_path.path in {"/api/model-settings", "/api/model-settings/test"}:
			payload = self._read_json_body(65536)
			if payload is None:
				return
			self._handle_model_settings(parsed_path.path, payload)
			return
		if parsed_path.path == "/api/reprocess/plan":
			payload = self._read_json_body(65536)
			if payload is None:
				return
			self._handle_reprocess_plan(payload)
			return
		if parsed_path.path == "/api/reprocess":
			payload = self._read_json_body(65536)
			if payload is None:
				return
			self._start_reprocess(payload)
			return
		if not self._consume_empty_json_body():
			return
		if parsed_path.path == "/api/organize":
			self._start_organize()
			return
		parts = parsed_path.path.split("/")
		if len(parts) == 5 and parts[1:3] == ["api", "jobs"] and parts[4] == "cancel":
			self._cancel_organize(parts[3])
			return
		if len(parts) == 5 and parts[1:3] == ["api", "records"] and parts[4] in {"hide", "restore", "purge"}:
			record_id = unquote(parts[3])
			if not record_id or "/" in record_id:
				self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
				return
			if parts[4] == "purge":
				self._purge_record(record_id)
			else:
				self._apply_user_action(record_id, parts[4])
			return
		self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

	def _start_organize(self) -> None:
		try:
			job = self.server.job_manager.start()
		except JobConflictError as error:
			payload: dict[str, Any] = {"error": "organize_in_progress"}
			if error.active_job_id is not None:
				payload["job_id"] = error.active_job_id
			self._json(HTTPStatus.CONFLICT, payload)
			return
		self._json(HTTPStatus.ACCEPTED, {"job": job})

	def _cancel_organize(self, job_id: str) -> None:
		try:
			job = self.server.job_manager.get(job_id)
			if job is not None:
				job = self.server.job_manager.cancel(job_id)
			elif self.server.reprocess_job_manager is not None:
				job = self.server.reprocess_job_manager.cancel(job_id)
			else:
				raise KeyError(job_id)
		except KeyError:
			self._json(HTTPStatus.NOT_FOUND, {"error": "job_not_found"})
			return
		except JobCancellationTooLateError:
			self._json(HTTPStatus.CONFLICT, {"error": "cancellation_too_late"})
			return
		except OSError:
			self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "cancellation_failed"})
			return
		self._json(HTTPStatus.ACCEPTED, {"job": job})

	def _apply_user_action(self, record_id: str, event_type: str) -> None:
		try:
			result = self.server.action_manager.apply(record_id, event_type)
		except JobConflictError:
			self._json(HTTPStatus.CONFLICT, {"error": "organize_in_progress"})
			return
		except RecordNotFoundError:
			self._json(HTTPStatus.NOT_FOUND, {"error": "record_not_found"})
			return
		except (OSError, ValueError, TypeError, json.JSONDecodeError):
			self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "action_failed"})
			return
		self._json(HTTPStatus.OK, result)

	def _purge_record(self, record_id: str) -> None:
		manager = self.server.purge_manager
		if manager is None:
			self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "purge_not_available"})
			return
		try:
			result = manager.purge(record_id)
		except PurgeBusyError:
			self._json(HTTPStatus.CONFLICT, {"error": "operation_in_progress"})
			return
		except ValueError:
			self._json(HTTPStatus.CONFLICT, {"error": "purge_rejected"})
			return
		except (OSError, RuntimeError):
			self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "purge_failed"})
			return
		self._json(HTTPStatus.OK, result)

	def _handle_model_settings(self, path: str, payload: dict[str, Any]) -> None:
		manager = self.server.model_settings_manager
		if manager is None:
			self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "model_settings_not_available"})
			return
		try:
			result = manager.save(payload) if path == "/api/model-settings" else manager.test(payload)
		except JobConflictError:
			self._json(HTTPStatus.CONFLICT, {"error": "organize_in_progress"})
			return
		except LLMError as error:
			self._json(HTTPStatus(502), {
				"error": "model_connection_failed",
				"message": str(error),
			})
			return
		except (ValueError, KeyError, TypeError) as error:
			self._json(HTTPStatus.BAD_REQUEST, {
				"error": "invalid_model_settings",
				"message": str(error),
			})
			return
		except (OSError, json.JSONDecodeError):
			self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "model_settings_failed"})
			return
		self._json(HTTPStatus.OK, result)

	def _handle_reprocess_plan(self, payload: dict[str, Any]) -> None:
		manager = self.server.reprocess_plan_manager
		if manager is None:
			self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "reprocess_plan_not_available"})
			return
		try:
			result = manager.plan(payload)
		except JobConflictError:
			self._json(HTTPStatus.CONFLICT, {"error": "organize_in_progress"})
			return
		except ValueError as error:
			self._json(HTTPStatus.BAD_REQUEST, {
				"error": "invalid_reprocess_plan",
				"message": str(error),
			})
			return
		except (OSError, RuntimeError, json.JSONDecodeError):
			self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "reprocess_plan_failed"})
			return
		self._json(HTTPStatus.OK, result)

	def _start_reprocess(self, payload: dict[str, Any]) -> None:
		manager = self.server.reprocess_job_manager
		if manager is None:
			self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "reprocess_not_available"})
			return
		try:
			job = manager.start(payload)
		except ReprocessPlanChangedError:
			self._json(HTTPStatus.CONFLICT, {"error": "reprocess_plan_changed"})
			return
		except JobConflictError as error:
			response: dict[str, Any] = {"error": "operation_in_progress"}
			if error.active_job_id is not None:
				response["job_id"] = error.active_job_id
			self._json(HTTPStatus.CONFLICT, response)
			return
		except ValueError as error:
			self._json(HTTPStatus.BAD_REQUEST, {
				"error": "invalid_reprocess_request",
				"message": str(error),
			})
			return
		except (OSError, RuntimeError, json.JSONDecodeError):
			self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "reprocess_failed"})
			return
		self._json(HTTPStatus.ACCEPTED, {"job": job})

	def _valid_host(self) -> bool:
		host = self.headers.get("Host", "").lower()
		port = self.server.server_address[1]
		return host in {f"{hostname}:{port}" for hostname in ALLOWED_BROWSER_HOSTS}

	def _valid_read_origin(self) -> bool:
		origin = self.headers.get("Origin")
		return origin is None or origin == f"http://{self.headers.get('Host', '').lower()}"

	def _valid_write_request(self) -> bool:
		origin = self.headers.get("Origin")
		expected_origin = f"http://{self.headers.get('Host', '').lower()}"
		return (
			origin == expected_origin
			and secrets.compare_digest(
				self.headers.get("X-Note-Token", ""),
				self.server.token,
			)
			and self.headers.get_content_type() == "application/json"
		)

	def _consume_empty_json_body(self) -> bool:
		body = self._read_json_body(1024)
		if body is None:
			return False
		if body != {}:
			self._json(HTTPStatus.BAD_REQUEST, {"error": "unexpected_parameters"})
			return False
		return True

	def _read_json_body(self, maximum_bytes: int) -> dict[str, Any] | None:
		try:
			length = int(self.headers.get("Content-Length", "0"))
		except ValueError:
			self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
			return False
		if length < 0:
			self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_content_length"})
			return None
		if length > maximum_bytes:
			self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body_too_large"})
			return None
		try:
			body = json.loads(self.rfile.read(length) or b"{}")
		except (json.JSONDecodeError, UnicodeDecodeError):
			self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
			return None
		if not isinstance(body, dict):
			self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json_object"})
			return None
		return body

	def _serve_review(self) -> None:
		try:
			content = self.server.review_html_path.read_bytes()
		except OSError:
			self._json(HTTPStatus.NOT_FOUND, {"error": "review_not_found"})
			return
		self.send_response(HTTPStatus.OK)
		self.send_header("Content-Type", "text/html; charset=utf-8")
		self.send_header("Content-Length", str(len(content)))
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(content)

	def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
		content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
		self._bytes(status, content, "application/json; charset=utf-8")

	def _bytes(self, status: HTTPStatus, content: bytes, content_type: str) -> None:
		self.send_response(status)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(content)))
		self.send_header("Cache-Control", "no-store")
		self.send_header("X-Content-Type-Options", "nosniff")
		self.end_headers()
		self.wfile.write(content)

	def log_message(self, format: str, *args: Any) -> None:
		return


def create_server(
	port: int = DEFAULT_PORT,
	job_manager: OrganizeJobManager | None = None,
	action_manager: UserActionManager | None = None,
	purge_manager: PurgeManager | None = None,
	model_settings_manager: ModelSettingsManager | None = None,
	reprocess_plan_manager: ReprocessPlanManager | None = None,
	reprocess_job_manager: ReprocessJobManager | None = None,
	review_html_path: Path = REVIEW_HTML_PATH,
	token: str | None = None,
) -> LocalHTTPServer:
	manager = job_manager or OrganizeJobManager(ORGANIZE_LOCK_PATH)
	actions = action_manager or UserActionManager(
		ORGANIZE_LOCK_PATH,
		USER_ACTIONS_PATH,
		ENTRIES_PATH,
	)
	return LocalHTTPServer(
		(HOST, port),
		LocalRequestHandler,
		manager,
		actions,
		purge_manager,
		model_settings_manager,
		reprocess_plan_manager,
		reprocess_job_manager,
		review_html_path,
		token or secrets.token_urlsafe(32),
	)


def create_production_purge_manager(project_dir: Path = PROJECT_DIR) -> PurgeManager:
	"""Build the purge service from fixed project-owned paths only."""
	root = project_dir.resolve()
	data_dir = root / "data"
	settings = load_json(root / "config" / "settings.json")
	categories = validate_categories(load_json(root / "config" / "categories.json"))
	category_codes = {category["code"] for category in categories}
	display_settings = settings.get("display", {})
	language = display_settings.get("language", "zh-CN")
	collapsed_lines = display_settings.get("collapsed_lines", 4)
	template = (root / "output" / "review-mock.html").read_text(encoding="utf-8")
	markdown_it_source = (root / "vendor" / "markdown-it.min.js").read_text(encoding="utf-8")
	paths = PurgePaths(
		data_dir / "captured_entries.jsonl",
		data_dir / "entries.json",
		data_dir / "user_actions.jsonl",
		data_dir / "watcher_state.json",
	)
	def render(remaining_entries: list[dict[str, Any]]) -> str:
		validated = validate_entries(remaining_entries, category_codes)
		return build_html(
			validated,
			categories,
			language,
			collapsed_lines,
			markdown_it_source,
			template,
		)

	return PurgeManager(
		data_dir / "organize.lock",
		data_dir / "watcher-scan.lock",
		lambda record_id: purge_generated_data(
			record_id,
			paths,
			PurgeArtifacts(
				root,
				root / "output" / "review.html",
				discover_purge_logs(data_dir),
			),
			render,
		),
	)


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="启动仅限本机访问的笔记工具服务。")
	parser.add_argument("--port", type=int, default=DEFAULT_PORT)
	return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
	args = parse_args(arguments)
	if not 1 <= args.port <= 65535:
		raise SystemExit("端口必须在 1 到 65535 之间")
	recovered_count = recover_purges_at_startup(
		PROJECT_DIR / "data",
		ORGANIZE_LOCK_PATH,
		PROJECT_DIR / "data" / "watcher-scan.lock",
	)
	if recovered_count:
		print(f"已恢复或完成清理 {recovered_count} 个未结束的彻底删除事务。", flush=True)
	model_store = ExternalModelConfigStore(project_dir=PROJECT_DIR)
	diagnostic_store = DiagnosticStore(DIAGNOSTICS_PATH)
	cancel_wait_provider = lambda: current_model_timeout_seconds(model_store)
	reprocess_plan_manager = ReprocessPlanManager(
		ENTRIES_PATH,
		ORGANIZE_LOCK_PATH,
		model_store,
	)
	server = create_server(
		port=args.port,
		job_manager=OrganizeJobManager(
			ORGANIZE_LOCK_PATH,
			cancel_wait_seconds_provider=cancel_wait_provider,
			diagnostic_store=diagnostic_store,
		),
		purge_manager=create_production_purge_manager(PROJECT_DIR),
		model_settings_manager=ModelSettingsManager(model_store, ORGANIZE_LOCK_PATH),
		reprocess_plan_manager=reprocess_plan_manager,
		reprocess_job_manager=ReprocessJobManager(
			reprocess_plan_manager,
			ORGANIZE_LOCK_PATH,
			lambda target_ids, components, report, cancel_check: run_reprocess_pipeline(
				target_ids,
				components,
				report,
				model_store,
				cancel_check=cancel_check,
			),
			cancel_wait_seconds_provider=cancel_wait_provider,
			diagnostic_store=diagnostic_store,
		),
	)
	print(f"本地笔记服务已启动：http://{HOST}:{args.port}/", flush=True)
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass
	finally:
		server.server_close()


if __name__ == "__main__":
	main()