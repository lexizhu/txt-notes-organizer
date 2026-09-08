"""Turn immutable captured notes into AI-enriched entries.json records."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from llm_adapter import (
	AnalysisResult,
	LLMError,
	OllamaAdapter,
	StandardOpenAITransport,
	validate_categories,
	validate_display_markdown,
)
from model_config import (
	STANDARD_OPENAI,
	ExternalModelConfigStore,
)
from table_layout import detect_table_candidates


PROJECT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = PROJECT_DIR / "config" / "settings.json"
CATEGORIES_PATH = PROJECT_DIR / "config" / "categories.json"
CAPTURED_ENTRIES_PATH = PROJECT_DIR / "data" / "captured_entries.jsonl"
USER_ACTIONS_PATH = PROJECT_DIR / "data" / "user_actions.jsonl"
ENTRIES_PATH = PROJECT_DIR / "data" / "entries.json"
REQUIRED_CAPTURED_KEYS = {"id", "text", "record_time", "project", "source_file"}
REQUIRED_USER_ACTION_KEYS = {
	"event_id",
	"event_type",
	"record_id",
	"occurred_at",
	"actor",
}
DISPLAY_CACHE_KEYS = {
	"display_markdown",
	"display_text_sha256",
	"display_generated_at",
	"display_backend",
	"display_tables",
	"display_tables_text_sha256",
	"display_tables_generated_at",
	"display_tables_version",
	"table_backend",
}


class NoteAnalyzer(Protocol):
	def analyze(
		self,
		text: str,
		record_time: str,
		categories: list[dict[str, Any]],
	) -> AnalysisResult: ...


class NoteFormatter(Protocol):
	def format_markdown(self, text: str) -> str: ...


class TableIdentifier(Protocol):
	def identify_tables(self, text: str) -> list[dict[str, Any]]: ...


class OrganizeCancelled(RuntimeError):
	pass


def cancellation_requested() -> bool:
	path = os.environ.get("MY_NOTES_CANCEL_FILE")
	return bool(path) and Path(path).is_file()


def raise_if_cancelled(check: Callable[[], bool] = cancellation_requested) -> None:
	if check():
		raise OrganizeCancelled("用户已取消整理")


def log(message: str, level: str = "INFO") -> None:
	now = datetime.now().astimezone().isoformat(timespec="seconds")
	print(f"[{now}] [{level}] {message}", flush=True)


def load_json(path: Path) -> Any:
	with path.open("r", encoding="utf-8") as file:
		return json.load(file)


def load_captured_entries(path: Path) -> list[dict[str, Any]]:
	"""Read and normalize legacy rows plus modern insert/update events."""
	captured_entries: list[dict[str, Any]] = []
	seen_event_ids: set[str] = set()
	with path.open("r", encoding="utf-8") as file:
		for line_number, line in enumerate(file, start=1):
			if not line.strip():
				continue
			try:
				entry = json.loads(line)
			except json.JSONDecodeError as error:
				raise ValueError(f"原始增量文件第 {line_number} 行不是有效 JSON") from error
			if not isinstance(entry, dict) or not REQUIRED_CAPTURED_KEYS <= entry.keys():
				raise ValueError(f"原始增量文件第 {line_number} 行缺少必要的英文字段")
			entry_id = entry["id"]
			if not isinstance(entry_id, str) or not entry_id:
				raise ValueError(f"原始增量文件第 {line_number} 行的 id 无效")
			normalized = dict(entry)
			normalized.setdefault("event_type", "insert")
			normalized.setdefault("record_id", entry_id)
			normalized.setdefault("event_id", f"legacy:{line_number}:{entry_id}")
			if normalized["event_type"] not in {"insert", "update"}:
				raise ValueError(f"原始增量文件第 {line_number} 行包含不支持的 event_type")
			event_id = normalized["event_id"]
			if not isinstance(event_id, str) or not event_id or event_id in seen_event_ids:
				raise ValueError(f"原始增量文件第 {line_number} 行的 event_id 无效或重复")
			seen_event_ids.add(event_id)
			captured_entries.append(normalized)
	return captured_entries


def load_user_actions(path: Path) -> list[dict[str, Any]]:
	"""Read and validate append-only hide/restore events."""
	if not path.exists():
		return []
	user_actions: list[dict[str, Any]] = []
	seen_event_ids: set[str] = set()
	with path.open("r", encoding="utf-8") as file:
		for line_number, line in enumerate(file, start=1):
			if not line.strip():
				continue
			try:
				action = json.loads(line)
			except json.JSONDecodeError as error:
				raise ValueError(f"人工操作文件第 {line_number} 行不是有效 JSON") from error
			if not isinstance(action, dict) or not REQUIRED_USER_ACTION_KEYS <= action.keys():
				raise ValueError(f"人工操作文件第 {line_number} 行缺少必要的英文字段")
			event_id = action["event_id"]
			if not isinstance(event_id, str) or not event_id or event_id in seen_event_ids:
				raise ValueError(f"人工操作文件第 {line_number} 行的 event_id 无效或重复")
			if action["event_type"] not in {"hide", "restore"}:
				raise ValueError(f"人工操作文件第 {line_number} 行包含不支持的 event_type")
			for key in ("record_id", "occurred_at", "actor"):
				if not isinstance(action[key], str) or not action[key]:
					raise ValueError(f"人工操作文件第 {line_number} 行的 {key} 无效")
			seen_event_ids.add(event_id)
			user_actions.append(action)
	return user_actions


def fold_user_actions(actions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
	"""Fold hide/restore events into the latest visibility state per record."""
	visibility: dict[str, dict[str, Any]] = {}
	for action in actions:
		visibility[str(action["record_id"])] = {
			"hidden": action["event_type"] == "hide",
			"occurred_at": action["occurred_at"],
			"event_id": action["event_id"],
		}
	return visibility


def apply_user_actions(
	entries: list[dict[str, Any]],
	visibility: dict[str, dict[str, Any]],
) -> bool:
	"""Project user-owned visibility onto entries without removing records."""
	entries_by_id = {
		entry.get("id"): entry
		for entry in entries
		if isinstance(entry, dict) and isinstance(entry.get("id"), str)
	}
	unknown_ids = visibility.keys() - entries_by_id.keys()
	if unknown_ids:
		raise ValueError(f"人工操作引用了未知记录：{', '.join(sorted(unknown_ids))}")
	changed = False
	for record_id, state in visibility.items():
		entry = entries_by_id[record_id]
		hidden = bool(state["hidden"])
		if entry.get("hidden", False) != hidden:
			entry["hidden"] = hidden
			changed = True
		if hidden:
			if entry.get("hidden_at") != state["occurred_at"]:
				entry["hidden_at"] = state["occurred_at"]
				changed = True
		elif "hidden_at" in entry:
			entry.pop("hidden_at")
			changed = True
	return changed


def fold_capture_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Fold append-only insert/update events into latest logical record snapshots."""
	records: dict[str, dict[str, Any]] = {}
	order: list[str] = []
	aliases: dict[str, str] = {}
	for event in events:
		event_type = event["event_type"]
		event_record_id = str(event["record_id"])
		if event_type == "insert":
			if event_record_id in records or event_record_id in aliases:
				raise ValueError(f"重复 insert record_id：{event_record_id}")
			records[event_record_id] = {
				"id": event_record_id,
				"text": event["text"],
				"record_time": event["record_time"],
				"project": event["project"],
				"source_file": event["source_file"],
				"event_type": "insert",
				"needs_analysis": True,
			}
			order.append(event_record_id)
			continue

		canonical_id = aliases.get(event_record_id)
		if canonical_id is None and event_record_id in records:
			canonical_id = event_record_id
		if canonical_id is None:
			previous_hash = event.get("previous_text_sha256")
			matches = [
				record_id
				for record_id, record in records.items()
				if record["source_file"] == event["source_file"]
				and text_sha256(str(record["text"])) == previous_hash
			]
			if len(matches) != 1:
				raise ValueError(
					f"update {event['event_id']} 无法唯一匹配 previous_text_sha256"
				)
			canonical_id = matches[0]
			aliases[event_record_id] = canonical_id
		record = records[canonical_id]
		previous_hash = event.get("previous_text_sha256")
		if previous_hash != text_sha256(str(record["text"])):
			raise ValueError(f"update {event['event_id']} 的 previous_text_sha256 不连续")
		record.update(
			{
				"text": event["text"],
				"record_time": event["record_time"],
				"project": event["project"],
				"source_file": event["source_file"],
				"event_type": "update",
				"needs_analysis": True,
			}
		)
	return [records[record_id] for record_id in order]


def save_entries(path: Path, entries: list[dict[str, Any]]) -> None:
	"""Atomically replace the derived entries file after each success."""
	temporary_path = path.with_suffix(".tmp")
	with temporary_path.open("w", encoding="utf-8") as file:
		json.dump(entries, file, ensure_ascii=False, indent=2)
		file.write("\n")
	temporary_path.replace(path)


def text_sha256(text: str) -> str:
	return hashlib.sha256(text.encode("utf-8")).hexdigest()


def backend_fingerprint(settings: dict[str, Any]) -> str:
	"""Return an opaque cache identity that never embeds endpoint, model, or key."""
	canonical = json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
	return "model-backend-v1:" + text_sha256(canonical)


def describe_model_backend(
	project_settings: dict[str, Any],
	model_settings: dict[str, Any],
) -> dict[str, str]:
	"""Describe result identity without connection details or secrets."""
	provider = model_settings["provider"]
	if provider == "ollama":
		model = str(project_settings["ollama"]["model"]).strip()
		label = f"本地 {model}"
		identity = {"provider": provider, "model": model}
	else:
		variant = model_settings["cloud_variant"]
		model = str(model_settings[variant]["model"])
		label = f"云端 {model}"
		identity = {"provider": provider, "variant": variant, "model": model}
	return {"id": backend_fingerprint(identity), "label": label, "provider": provider, "model": model}


def describe_legacy_model_backend(
	project_settings: dict[str, Any],
	model_settings: dict[str, Any],
) -> dict[str, str]:
	"""Describe the pre-v2 fingerprint for existing records from the current config."""
	current = describe_model_backend(project_settings, model_settings)
	if model_settings["provider"] == "ollama":
		ollama = project_settings["ollama"]
		identity = {
			"provider": "ollama",
			"base_url": str(ollama["base_url"]).rstrip("/"),
			"model": str(ollama["model"]),
			"prompt_version": 1,
		}
	else:
		variant = model_settings["cloud_variant"]
		identity = {"provider": "cloud", "variant": variant, "settings": model_settings[variant], "prompt_version": 1}
	return {"id": backend_fingerprint(identity), "label": current["label"], "provider": current["provider"], "model": current["model"]}


def create_model_adapter(
	project_settings: dict[str, Any],
	config_store: ExternalModelConfigStore | None = None,
) -> tuple[OllamaAdapter, str, str]:
	"""Create the configured backend while preserving Ollama as the safe default."""
	store = config_store or ExternalModelConfigStore(project_dir=PROJECT_DIR)
	model_settings = store.load_model_settings()
	backend = describe_model_backend(project_settings, model_settings)
	provider = model_settings["provider"]
	if provider == "ollama":
		ollama = project_settings["ollama"]
		base_url = str(ollama["base_url"]).strip()
		model = str(ollama["model"]).strip()
		timeout = float(ollama.get("timeout_seconds", 120))
		if not base_url.strip():
			raise ValueError("Ollama Base URL 不能为空")
		if not model.strip():
			raise ValueError("Ollama 模型名称不能为空")
		if not math.isfinite(timeout) or timeout <= 0:
			raise ValueError("Ollama 请求超时必须大于 0 秒")
		adapter = OllamaAdapter(base_url=base_url, model=model, timeout_seconds=timeout)
		try:
			store.register_model_backend(backend)
		except (OSError, ValueError, json.JSONDecodeError):
			log("无法更新模型来源历史；本次整理仍会继续。", "WARNING")
		return (
			adapter,
			backend["id"],
			"本地 Ollama",
		)

	api_key = store.load_api_key()
	variant = model_settings["cloud_variant"]
	cloud = model_settings[variant]
	transport = create_cloud_transport(model_settings, api_key)
	adapter = OllamaAdapter(
		base_url="https://configured-cloud.invalid",
		model="configured-cloud-model",
		timeout_seconds=transport.timeout_seconds,
		transport=transport,
	)
	try:
		store.register_model_backend(backend)
	except (OSError, ValueError, json.JSONDecodeError):
		log("无法更新模型来源历史；本次整理仍会继续。", "WARNING")
	return (
		adapter,
		backend["id"],
		"云端 API",
	)


def create_cloud_transport(model_settings: dict[str, Any], api_key: str):
	"""Create one validated cloud transport from non-secret settings plus a key."""
	variant = model_settings["cloud_variant"]
	cloud = model_settings[variant]
	if variant == STANDARD_OPENAI:
		return StandardOpenAITransport(
			base_url=str(cloud["base_url"]),
			api_key=api_key,
			model=str(cloud["model"]),
			timeout_seconds=float(cloud.get("timeout_seconds", 120)),
			ca_bundle_path=str(cloud.get("ca_bundle_path", "")),
		)
	raise ValueError("不支持的云端模型变体")


def has_valid_display_cache(entry: dict[str, Any]) -> bool:
	text = entry.get("text")
	markdown = entry.get("display_markdown")
	if not isinstance(text, str) or not isinstance(markdown, str):
		return False
	if entry.get("display_text_sha256") != text_sha256(text):
		return False
	try:
		validate_display_markdown(text, markdown)
	except LLMError:
		return False
	return True


def format_display_entries(
	entries: list[dict[str, Any]],
	formatter: NoteFormatter,
	entries_path: Path,
	now_provider: Callable[[], str] | None = None,
	backend_id: str | None = None,
	target_ids: set[str] | None = None,
	force: bool = False,
	cancel_check: Callable[[], bool] = lambda: False,
) -> tuple[int, int, int]:
	"""Generate and cache strictly validated display Markdown for stale entries."""
	now_provider = now_provider or (
		lambda: datetime.now().astimezone().isoformat(timespec="seconds")
	)
	success_count = 0
	failure_count = 0
	skipped_count = 0
	def needs_processing(entry: dict[str, Any]) -> bool:
		is_target = target_ids is None or entry.get("id") in target_ids
		return is_target and (force or not has_valid_display_cache(entry))

	pending_count = sum(needs_processing(entry) for entry in entries)
	current_index = 0
	for entry in entries:
		raise_if_cancelled(cancel_check)
		if not needs_processing(entry):
			skipped_count += 1
			continue
		current_index += 1
		text = entry.get("text")
		if not isinstance(text, str):
			failure_count += 1
			continue
		summary = text.replace("\n", " ")[:80]
		log(f"正在排版第 {current_index}/{pending_count} 条：{summary}")
		started_at = time.monotonic()
		try:
			markdown = formatter.format_markdown(text)
			validate_display_markdown(text, markdown)
			raise_if_cancelled(cancel_check)
		except (LLMError, OSError, ValueError, TypeError) as error:
			elapsed = time.monotonic() - started_at
			entry["display_markdown"] = text
			entry["display_text_sha256"] = text_sha256(text)
			entry["display_generated_at"] = now_provider()
			entry.pop("display_backend", None)
			save_entries(entries_path, entries)
			failure_count += 1
			log(
				f"记录 {entry.get('id', '<unknown>')} 排版失败（{elapsed:.1f} 秒），"
				f"保留原文显示：{error}",
				"ERROR",
			)
			continue
		entry["display_markdown"] = markdown
		entry["display_text_sha256"] = text_sha256(text)
		entry["display_generated_at"] = now_provider()
		if backend_id is not None:
			entry["display_backend"] = backend_id
		save_entries(entries_path, entries)
		success_count += 1
		elapsed = time.monotonic() - started_at
		log(f"排版完成并通过逐字校验；耗时 {elapsed:.1f} 秒。")
	return success_count, failure_count, skipped_count


TABLE_CACHE_VERSION = 1


def has_valid_table_cache(entry: dict[str, Any]) -> bool:
	text = entry.get("text")
	structures = entry.get("display_tables")
	if not isinstance(text, str) or not isinstance(structures, list):
		return False
	if entry.get("display_tables_version") != TABLE_CACHE_VERSION:
		return False
	if entry.get("display_tables_text_sha256") != text_sha256(text):
		return False
	expected_ids = [candidate.candidate_id for candidate in detect_table_candidates(text)]
	if not all(isinstance(structure, dict) for structure in structures):
		return False
	returned_ids = sorted(structure.get("candidate_id") for structure in structures)
	return returned_ids == expected_ids


def identify_table_entries(
	entries: list[dict[str, Any]],
	identifier: TableIdentifier,
	entries_path: Path,
	now_provider: Callable[[], str] | None = None,
	backend_id: str | None = None,
	target_ids: set[str] | None = None,
	force: bool = False,
	cancel_check: Callable[[], bool] = lambda: False,
) -> tuple[int, int, int]:
	"""Cache coordinate-only table structures; never store generated cell text."""
	now_provider = now_provider or (
		lambda: datetime.now().astimezone().isoformat(timespec="seconds")
	)
	success_count = 0
	failure_count = 0
	skipped_count = 0
	def needs_processing(entry: dict[str, Any]) -> bool:
		is_target = target_ids is None or entry.get("id") in target_ids
		return is_target and (force or not has_valid_table_cache(entry))

	pending_count = sum(needs_processing(entry) for entry in entries)
	current_index = 0
	for entry in entries:
		raise_if_cancelled(cancel_check)
		if not needs_processing(entry):
			skipped_count += 1
			continue
		current_index += 1
		text = entry.get("text")
		if not isinstance(text, str):
			failure_count += 1
			continue
		candidates = detect_table_candidates(text)
		log(f"正在识别表格第 {current_index}/{pending_count} 条：发现 {len(candidates)} 个候选。")
		started_at = time.monotonic()
		try:
			structures = identifier.identify_tables(text) if candidates else []
			expected_ids = [candidate.candidate_id for candidate in candidates]
			returned_ids = sorted(structure.get("candidate_id") for structure in structures)
			if returned_ids != expected_ids:
				raise LLMError("表格坐标没有完整覆盖全部候选。")
			raise_if_cancelled(cancel_check)
		except (LLMError, OSError, ValueError, TypeError) as error:
			entry.pop("display_tables", None)
			entry.pop("display_tables_text_sha256", None)
			entry.pop("display_tables_generated_at", None)
			entry.pop("display_tables_version", None)
			entry.pop("table_backend", None)
			save_entries(entries_path, entries)
			failure_count += 1
			log(f"表格坐标识别失败，页面保留疑似原文块：{error}", "ERROR")
			continue
		entry["display_tables"] = structures
		entry["display_tables_text_sha256"] = text_sha256(text)
		entry["display_tables_generated_at"] = now_provider()
		entry["display_tables_version"] = TABLE_CACHE_VERSION
		if backend_id is not None:
			entry["table_backend"] = backend_id
		save_entries(entries_path, entries)
		success_count += 1
		elapsed = time.monotonic() - started_at
		log(f"表格坐标已缓存：{len(structures)} 个候选；耗时 {elapsed:.1f} 秒。")
	return success_count, failure_count, skipped_count


def make_structured_entry(
	captured_entry: dict[str, Any],
	result: AnalysisResult,
	now: str,
	backend_id: str | None = None,
) -> dict[str, Any]:
	"""Keep captured facts immutable and add only derived AI fields."""
	entry = {
		"id": captured_entry["id"],
		"text": captured_entry["text"],
		"record_time": captured_entry["record_time"],
		"event_time": result.event_time,
		"project": captured_entry["project"],
		"category": result.category,
		"status": result.status,
		"source_file": captured_entry["source_file"],
		"user_edited": False,
		"created_at": now,
		"updated_at": now,
	}
	if backend_id is not None:
		entry["analysis_backend"] = backend_id
	return entry


def clear_display_caches(entry: dict[str, Any]) -> None:
	for key in DISPLAY_CACHE_KEYS:
		entry.pop(key, None)


def ensure_processing_succeeded(
	classification_failures: int,
	formatting_failures: int,
	table_failures: int,
) -> None:
	"""Keep the previous HTML when any processing stage is incomplete."""
	if classification_failures + formatting_failures + table_failures:
		raise RuntimeError(
			"本次整理有未完成的记录，已保留上一次有效页面；请查看上方具体错误。"
		)


def ensure_daily_processing_succeeded(
	classification_failures: int,
	formatting_failures: int,
	table_failures: int,
) -> None:
	"""Block incomplete records, but allow safe display fallbacks into the page."""
	ensure_processing_succeeded(classification_failures, 0, 0)
	if formatting_failures + table_failures:
		log("部分展示增强失败，已使用原文显示并继续生成页面。", "WARNING")


def apply_analysis_to_existing(
	entry: dict[str, Any],
	snapshot: dict[str, Any],
	result: AnalysisResult | None,
	now: str,
	backend_id: str | None = None,
) -> None:
	entry.update(
		{
			"text": snapshot["text"],
			"record_time": snapshot["record_time"],
			"project": snapshot["project"],
			"source_file": snapshot["source_file"],
			"updated_at": now,
		}
	)
	if not entry.get("user_edited", False) and result is not None:
		entry.update(
			{
				"event_time": result.event_time,
				"category": result.category,
				"status": result.status,
			}
		)
		if backend_id is not None:
			entry["analysis_backend"] = backend_id
	clear_display_caches(entry)


def reanalyze_existing_entries(
	entries: list[dict[str, Any]],
	target_ids: set[str],
	categories: list[dict[str, Any]],
	analyzer: NoteAnalyzer,
	entries_path: Path,
	now_provider: Callable[[], str] | None = None,
	backend_id: str | None = None,
	backend_label: str = "本地 Ollama",
	cancel_check: Callable[[], bool] = lambda: False,
) -> tuple[int, int, int]:
	"""Reanalyze exact existing IDs while preserving user-edited classifications."""
	validate_categories(categories)
	now_provider = now_provider or (
		lambda: datetime.now().astimezone().isoformat(timespec="seconds")
	)
	targets = [entry for entry in entries if entry.get("id") in target_ids]
	success_count = 0
	failure_count = 0
	skipped_count = len(entries) - len(targets)
	processable = [entry for entry in targets if entry.get("user_edited") is not True]
	skipped_count += len(targets) - len(processable)
	for index, entry in enumerate(processable, start=1):
		raise_if_cancelled(cancel_check)
		text = entry.get("text")
		record_time = entry.get("record_time")
		if not isinstance(text, str) or not isinstance(record_time, str):
			failure_count += 1
			continue
		log(f"正在重新分类第 {index}/{len(processable)} 条：{text.replace(chr(10), ' ')[:80]}")
		log(f"正在调用{backend_label}，请稍候……")
		started_at = time.monotonic()
		try:
			result = analyzer.analyze(text, record_time, categories)
			raise_if_cancelled(cancel_check)
		except (LLMError, OSError, ValueError, TypeError) as error:
			failure_count += 1
			log(f"第 {index}/{len(processable)} 条重新分类失败：{error}", "ERROR")
			continue
		entry.update({
			"event_time": result.event_time,
			"category": result.category,
			"status": result.status,
			"updated_at": now_provider(),
		})
		if backend_id is not None:
			entry["analysis_backend"] = backend_id
		save_entries(entries_path, entries)
		success_count += 1
		log(f"重新分类完成；耗时 {time.monotonic() - started_at:.1f} 秒。")
	return success_count, failure_count, skipped_count


def reprocess_selected_entries(
	entries: list[dict[str, Any]],
	target_ids: set[str],
	components: list[str],
	categories: list[dict[str, Any]],
	analyzer: NoteAnalyzer,
	entries_path: Path,
	backend_id: str | None = None,
	backend_label: str = "本地 Ollama",
	cancel_check: Callable[[], bool] = lambda: False,
) -> dict[str, tuple[int, int, int]]:
	"""Force selected derived fields through the existing validated processors."""
	allowed_components = {"analysis", "display", "tables"}
	if not components or any(component not in allowed_components for component in components):
		raise ValueError("重处理内容无效")
	results: dict[str, tuple[int, int, int]] = {}
	if "analysis" in components:
		raise_if_cancelled(cancel_check)
		results["analysis"] = reanalyze_existing_entries(
			entries,
			target_ids,
			categories,
			analyzer,
			entries_path,
			backend_id=backend_id,
			backend_label=backend_label,
			cancel_check=cancel_check,
		)
	if "display" in components:
		raise_if_cancelled(cancel_check)
		results["display"] = format_display_entries(
			entries,
			analyzer,
			entries_path,
			backend_id=backend_id,
			target_ids=target_ids,
			force=True,
			cancel_check=cancel_check,
		)
	if "tables" in components:
		raise_if_cancelled(cancel_check)
		results["tables"] = identify_table_entries(
			entries,
			analyzer,
			entries_path,
			backend_id=backend_id,
			target_ids=target_ids,
			force=True,
			cancel_check=cancel_check,
		)
	ensure_processing_succeeded(*(
		results.get(component, (0, 0, 0))[1]
		for component in ("analysis", "display", "tables")
	))
	return results


def organize_entries(
	captured_entries: list[dict[str, Any]],
	existing_entries: list[dict[str, Any]],
	categories: list[dict[str, Any]],
	analyzer: NoteAnalyzer,
	entries_path: Path,
	now_provider: Callable[[], str] | None = None,
	backend_id: str | None = None,
	backend_label: str = "本地 Ollama",
	cancel_check: Callable[[], bool] = lambda: False,
) -> tuple[int, int, int]:
	"""Analyze new or updated logical snapshots, saving each success immediately."""
	validate_categories(categories)
	if not isinstance(existing_entries, list):
		raise ValueError("entries.json 的顶层必须是数组")
	existing_by_id = {
		entry["id"]: entry
		for entry in existing_entries
		if isinstance(entry, dict) and isinstance(entry.get("id"), str)
	}
	pending_entries = []
	for snapshot in captured_entries:
		existing = existing_by_id.get(snapshot["id"])
		if existing is None or existing.get("text") != snapshot["text"]:
			pending_entries.append(snapshot)
	skipped_count = len(captured_entries) - len(pending_entries)
	success_count = 0
	failure_count = 0
	total_count = len(pending_entries)
	now_provider = now_provider or (
		lambda: datetime.now().astimezone().isoformat(timespec="seconds")
	)

	for index, captured_entry in enumerate(pending_entries, start=1):
		raise_if_cancelled(cancel_check)
		summary = str(captured_entry["text"]).replace("\n", " ")[:80]
		log(f"正在处理第 {index}/{total_count} 条：{summary}")
		existing = existing_by_id.get(captured_entry["id"])
		if existing is not None and existing.get("user_edited", False):
			apply_analysis_to_existing(existing, captured_entry, None, now_provider(), backend_id)
			save_entries(entries_path, existing_entries)
			success_count += 1
			log("正文已更新；保留人工 category/status/event_time，并清除旧展示缓存。")
			continue
		log(f"正在调用{backend_label}，请稍候……")
		started_at = time.monotonic()
		try:
			result = analyzer.analyze(
				str(captured_entry["text"]),
				str(captured_entry["record_time"]),
				categories,
			)
			raise_if_cancelled(cancel_check)
			elapsed = time.monotonic() - started_at
			now = now_provider()
			existing = existing_by_id.get(captured_entry["id"])
			if existing is None:
				structured_entry = make_structured_entry(captured_entry, result, now, backend_id)
				existing_entries.append(structured_entry)
				existing_by_id[structured_entry["id"]] = structured_entry
			else:
				apply_analysis_to_existing(existing, captured_entry, result, now, backend_id)
			save_entries(entries_path, existing_entries)
			success_count += 1
			log(
				f"LLM 返回：category={result.category}, status={result.status}, "
				f"event_time={result.event_time!r}；耗时 {elapsed:.1f} 秒。"
			)
		except (LLMError, OSError, ValueError, TypeError) as error:
			elapsed = time.monotonic() - started_at
			failure_count += 1
			log(f"第 {index}/{total_count} 条处理失败（{elapsed:.1f} 秒）：{error}", "ERROR")

	return success_count, failure_count, skipped_count


def main() -> None:
	log("笔记整理程序已启动。")
	try:
		settings = load_json(SETTINGS_PATH)
		categories = validate_categories(load_json(CATEGORIES_PATH))
		capture_events = load_captured_entries(CAPTURED_ENTRIES_PATH)
		captured_entries = fold_capture_events(capture_events)
		user_actions = fold_user_actions(load_user_actions(USER_ACTIONS_PATH))
		existing_entries = load_json(ENTRIES_PATH)
		if not isinstance(existing_entries, list):
			raise ValueError("entries.json 的顶层必须是数组")
		analyzer, backend_id, backend_label = create_model_adapter(settings)
	except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
		log(f"启动失败，请检查数据和配置文件：{error}", "ERROR")
		raise SystemExit(1) from error

	existing_by_id = {
		entry.get("id"): entry
		for entry in existing_entries
		if isinstance(entry, dict) and isinstance(entry.get("id"), str)
	}
	pending_count = sum(
		entry["id"] not in existing_by_id
		or existing_by_id[entry["id"]].get("text") != entry["text"]
		for entry in captured_entries
	)
	failure_count = 0
	log(
		f"读取到 {len(captured_entries)} 条原始记录，其中 {pending_count} 条需要整理；"
		f"模型后端：{backend_label}。"
	)
	if pending_count:
		try:
			success_count, failure_count, skipped_count = organize_entries(
				captured_entries,
				existing_entries,
				categories,
				analyzer,
				ENTRIES_PATH,
				backend_id=backend_id,
				backend_label=backend_label,
				cancel_check=cancellation_requested,
			)
		except (KeyboardInterrupt, OrganizeCancelled):
			log("收到停止指令；已经成功保存的记录会保留。", "WARNING")
			raise SystemExit(130)

		log(
			f"整理完成：成功 {success_count} 条，失败 {failure_count} 条，"
			f"跳过已有记录 {skipped_count} 条。"
		)
	else:
		log("没有需要分类的新记录。")

	if apply_user_actions(existing_entries, user_actions):
		save_entries(ENTRIES_PATH, existing_entries)
		log("已应用人工隐藏/恢复状态。")

	stale_display_count = sum(
		not has_valid_display_cache(entry) for entry in existing_entries
	)
	log(f"排版缓存检查：{stale_display_count} 条需要生成或更新。")
	try:
		raise_if_cancelled()
		display_success, display_failure, display_skipped = format_display_entries(
			existing_entries,
			analyzer,
			ENTRIES_PATH,
			backend_id=backend_id,
			cancel_check=cancellation_requested,
		)
	except (KeyboardInterrupt, OrganizeCancelled):
		log("收到停止指令；已经成功保存的排版会保留。", "WARNING")
		raise SystemExit(130)
	log(
		f"排版完成：成功 {display_success} 条，失败 {display_failure} 条，"
		f"复用缓存 {display_skipped} 条。"
	)

	stale_table_count = sum(not has_valid_table_cache(entry) for entry in existing_entries)
	log(f"表格坐标缓存检查：{stale_table_count} 条需要生成或更新。")
	try:
		raise_if_cancelled()
		table_success, table_failure, table_skipped = identify_table_entries(
			existing_entries,
			analyzer,
			ENTRIES_PATH,
			backend_id=backend_id,
			cancel_check=cancellation_requested,
		)
	except (KeyboardInterrupt, OrganizeCancelled):
		log("收到停止指令；已经成功保存的表格坐标会保留。", "WARNING")
		raise SystemExit(130)
	log(
		f"表格坐标完成：成功 {table_success} 条，失败 {table_failure} 条，"
		f"复用缓存 {table_skipped} 条。"
	)
	try:
		ensure_daily_processing_succeeded(failure_count, display_failure, table_failure)
	except RuntimeError as error:
		log(str(error), "ERROR")
		raise SystemExit(1) from error


if __name__ == "__main__":
	main()
