"""Build validated, read-only plans for permanently purging one record."""

from __future__ import annotations

import copy
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from organize import fold_capture_events, load_captured_entries, load_user_actions, text_sha256


@dataclass(frozen=True)
class PurgePlan:
	record_id: str
	captured_event_indexes: tuple[int, ...]
	captured_event_ids: tuple[str, ...]
	captured_record_ids: tuple[str, ...]
	entry_index: int
	user_action_indexes: tuple[int, ...]
	user_action_event_ids: tuple[str, ...]
	state_source_file: str
	state_record_index: int | None
	state_record_id: str | None
	state_match: str


@dataclass(frozen=True)
class PurgePaths:
	captured_entries: Path
	entries: Path
	user_actions: Path
	watcher_state: Path

	@property
	def data_dir(self) -> Path:
		parents = {path.parent.resolve() for path in self.files().values()}
		if len(parents) != 1:
			raise ValueError("彻底删除的数据文件必须位于同一目录")
		return next(iter(parents))

	def files(self) -> dict[str, Path]:
		return {
			"captured_entries.jsonl": self.captured_entries,
			"entries.json": self.entries,
			"user_actions.jsonl": self.user_actions,
			"watcher_state.json": self.watcher_state,
		}


@dataclass(frozen=True)
class PurgeArtifacts:
	project_dir: Path
	review_html: Path
	log_paths: tuple[Path, ...]


def discover_purge_logs(data_dir: Path) -> tuple[Path, ...]:
	"""Return current and rotated logs that may contain note excerpts."""
	paths: set[Path] = set()
	for pattern in ("local-server.log*", "watcher.log*", "watcher-error.log*"):
		paths.update(path for path in data_dir.glob(pattern) if path.is_file())
	return tuple(sorted(paths))


@dataclass
class _CaptureChain:
	canonical_id: str
	record_ids: set[str]
	event_indexes: list[int]
	event_ids: list[str]
	text: str
	source_file: str


def build_purge_plan(
	record_id: str,
	capture_events: list[dict[str, Any]],
	entries: list[dict[str, Any]],
	user_actions: list[dict[str, Any]],
	watcher_state: dict[str, Any],
) -> PurgePlan:
	"""Return an exact deletion plan without mutating any supplied object."""
	if not isinstance(record_id, str) or not record_id:
		raise ValueError("彻底删除的 record_id 无效")
	entry_matches = [
		(index, entry)
		for index, entry in enumerate(entries)
		if isinstance(entry, dict) and entry.get("id") == record_id
	]
	if len(entry_matches) != 1:
		raise ValueError(f"entries.json 必须唯一包含目标记录：{record_id}")
	entry_index, entry = entry_matches[0]
	if entry.get("hidden") is not True:
		raise ValueError("只有已隐藏的记录才能彻底删除")

	chains = _build_capture_chains(capture_events)
	chain = chains.get(record_id)
	if chain is None:
		raise ValueError(f"捕获事件中找不到目标记录链：{record_id}")
	if entry.get("source_file") != chain.source_file or entry.get("text") != chain.text:
		raise ValueError("entries.json 与捕获事件的目标快照不一致")

	state_source_file, state_record_index, state_record_id, state_match = _locate_state_record(
		chain,
		chains,
		watcher_state,
	)
	action_matches = [
		(index, action)
		for index, action in enumerate(user_actions)
		if isinstance(action, dict) and action.get("record_id") == record_id
	]
	if not action_matches or action_matches[-1][1].get("event_type") != "hide":
		raise ValueError("目标记录缺少当前有效的隐藏操作")
	return PurgePlan(
		record_id=record_id,
		captured_event_indexes=tuple(chain.event_indexes),
		captured_event_ids=tuple(chain.event_ids),
		captured_record_ids=tuple(sorted(chain.record_ids)),
		entry_index=entry_index,
		user_action_indexes=tuple(index for index, _ in action_matches),
		user_action_event_ids=tuple(str(action["event_id"]) for _, action in action_matches),
		state_source_file=state_source_file,
		state_record_index=state_record_index,
		state_record_id=state_record_id,
		state_match=state_match,
	)


def purge_structured_data(
	record_id: str,
	paths: PurgePaths,
	*,
	fail_after_replacements: int | None = None,
) -> PurgePlan:
	"""Purge one hidden record transactionally from generated structured data."""
	recover_incomplete_purges(paths.data_dir)
	raw_capture_events = _load_jsonl(paths.captured_entries, "captured_entries.jsonl")
	capture_events = load_captured_entries(paths.captured_entries)
	entries = _load_json_array(paths.entries, "entries.json")
	user_actions = load_user_actions(paths.user_actions)
	watcher_state = _load_json_object(paths.watcher_state, "watcher_state.json")
	plan = build_purge_plan(record_id, capture_events, entries, user_actions, watcher_state)
	prepared = _prepare_purged_values(
		plan,
		raw_capture_events,
		entries,
		user_actions,
		watcher_state,
	)
	_validate_purged_values(plan, prepared, raw_capture_events, entries, user_actions, watcher_state)
	transaction_dir = paths.data_dir / f".purge-transaction-{uuid.uuid4()}"
	backup_dir = transaction_dir / "backup"
	prepared_dir = transaction_dir / "prepared"
	transaction_dir.mkdir()
	backup_dir.mkdir()
	prepared_dir.mkdir()
	manifest = {
		"transaction_id": transaction_dir.name.removeprefix(".purge-transaction-"),
		"record_id": record_id,
		"status": "preparing",
		"files": list(paths.files()),
	}
	_write_json_atomic(transaction_dir / "manifest.json", manifest)
	try:
		for name, source_path in paths.files().items():
			shutil.copy2(source_path, backup_dir / name)
			_fsync_file(backup_dir / name)
		_write_prepared_files(prepared_dir, prepared)
		_validate_prepared_files(prepared_dir, plan, raw_capture_events, entries, user_actions, watcher_state)
		manifest["status"] = "prepared"
		_write_json_atomic(transaction_dir / "manifest.json", manifest)
		manifest["status"] = "committing"
		_write_json_atomic(transaction_dir / "manifest.json", manifest)
		replaced_count = 0
		for name, target_path in paths.files().items():
			shutil.copystat(target_path, prepared_dir / name)
			os.replace(prepared_dir / name, target_path)
			replaced_count += 1
			if fail_after_replacements == replaced_count:
				raise OSError("测试注入：提交中断")
		_validate_live_files(paths, plan, raw_capture_events, entries, user_actions, watcher_state)
		manifest["status"] = "committed"
		_write_json_atomic(transaction_dir / "manifest.json", manifest)
	except Exception:
		_restore_transaction(transaction_dir, paths.data_dir)
		raise
	shutil.rmtree(transaction_dir)
	return plan


def purge_generated_data(
	record_id: str,
	paths: PurgePaths,
	artifacts: PurgeArtifacts,
	html_renderer: Callable[[list[dict[str, Any]]], str],
	*,
	fail_after_replacements: int | None = None,
) -> PurgePlan:
	"""Purge structured data, generated HTML, and log excerpts in one transaction."""
	project_dir = artifacts.project_dir.resolve()
	if paths.data_dir.parent.resolve() != project_dir:
		raise ValueError("彻底删除项目目录与数据目录不匹配")
	recover_incomplete_purges(paths.data_dir)
	raw_capture_events = _load_jsonl(paths.captured_entries, "captured_entries.jsonl")
	capture_events = load_captured_entries(paths.captured_entries)
	entries = _load_json_array(paths.entries, "entries.json")
	user_actions = load_user_actions(paths.user_actions)
	watcher_state = _load_json_object(paths.watcher_state, "watcher_state.json")
	plan = build_purge_plan(record_id, capture_events, entries, user_actions, watcher_state)
	target_text = str(entries[plan.entry_index]["text"])
	prepared_values = _prepare_purged_values(
		plan,
		raw_capture_events,
		entries,
		user_actions,
		watcher_state,
	)
	_validate_purged_values(
		plan,
		prepared_values,
		raw_capture_events,
		entries,
		user_actions,
		watcher_state,
	)
	prepared_bytes = _serialize_structured_values(prepared_values)
	review_html = html_renderer(prepared_values["entries.json"])
	record_id_marker = f'"id": {json.dumps(record_id, ensure_ascii=False)}'
	if record_id_marker in review_html or target_text in review_html:
		raise ValueError("重建页面仍包含目标记录")
	prepared_bytes[_relative_key(artifacts.review_html, project_dir)] = review_html.encode("utf-8")
	historical_texts = [
		str(raw_capture_events[index].get("text", ""))
		for index in plan.captured_event_indexes
	]
	log_tokens = _log_redaction_tokens(record_id, historical_texts)
	log_keys: set[str] = set()
	for log_path in artifacts.log_paths:
		if not log_path.is_file():
			continue
		key = _relative_key(log_path, project_dir)
		log_keys.add(key)
		prepared_bytes[key] = _scrub_log_bytes(log_path.read_bytes(), log_tokens)
		if any(token in prepared_bytes[key] for token in log_tokens):
			raise ValueError(f"日志净化后仍包含目标摘要：{key}")

	target_paths = {
		**{_relative_key(path, project_dir): path for path in paths.files().values()},
		_relative_key(artifacts.review_html, project_dir): artifacts.review_html,
		**{
			_relative_key(path, project_dir): path
			for path in artifacts.log_paths
			if path.is_file()
		},
	}
	if set(prepared_bytes) != set(target_paths):
		raise ValueError("彻底删除准备文件与目标文件集合不一致")
	transaction_dir = paths.data_dir / f".purge-transaction-{uuid.uuid4()}"
	backup_dir = transaction_dir / "backup"
	prepared_dir = transaction_dir / "prepared"
	backup_dir.mkdir(parents=True)
	prepared_dir.mkdir()
	manifest = {
		"transaction_id": transaction_dir.name.removeprefix(".purge-transaction-"),
		"record_id": record_id,
		"status": "preparing",
		"targets": sorted(target_paths),
		"in_place": sorted(log_keys),
	}
	_write_json_atomic(transaction_dir / "manifest.json", manifest)
	try:
		for key, source_path in target_paths.items():
			backup_path = backup_dir / key
			backup_path.parent.mkdir(parents=True, exist_ok=True)
			shutil.copy2(source_path, backup_path)
			_fsync_file(backup_path)
		for key, content in prepared_bytes.items():
			prepared_path = prepared_dir / key
			prepared_path.parent.mkdir(parents=True, exist_ok=True)
			_write_bytes(prepared_path, content)
		manifest["status"] = "prepared"
		_write_json_atomic(transaction_dir / "manifest.json", manifest)
		manifest["status"] = "committing"
		_write_json_atomic(transaction_dir / "manifest.json", manifest)
		replaced_count = 0
		for key, target_path in target_paths.items():
			prepared_path = prepared_dir / key
			if key in log_keys:
				_rewrite_in_place(target_path, prepared_path.read_bytes())
			else:
				shutil.copystat(target_path, prepared_path)
				os.replace(prepared_path, target_path)
			replaced_count += 1
			if fail_after_replacements == replaced_count:
				raise OSError("测试注入：完整提交中断")
		_validate_live_files(paths, plan, raw_capture_events, entries, user_actions, watcher_state)
		if record_id_marker in artifacts.review_html.read_text(encoding="utf-8"):
			raise ValueError("正式页面仍包含目标 record_id")
		for key in log_keys:
			content = target_paths[key].read_bytes()
			if any(token in content for token in log_tokens):
				raise ValueError(f"正式日志仍包含目标摘要：{key}")
		manifest["status"] = "committed"
		_write_json_atomic(transaction_dir / "manifest.json", manifest)
	except Exception:
		_restore_transaction(transaction_dir, paths.data_dir)
		raise
	shutil.rmtree(transaction_dir)
	return plan


def recover_incomplete_purges(data_dir: Path) -> int:
	"""Restore every leftover purge transaction, then remove its sensitive backup."""
	recovered = 0
	for transaction_dir in sorted(data_dir.glob(".purge-transaction-*")):
		if not transaction_dir.is_dir():
			continue
		manifest_path = transaction_dir / "manifest.json"
		status = None
		if manifest_path.is_file():
			try:
				manifest = _load_json_object(manifest_path, "purge manifest")
				status = manifest.get("status")
			except (OSError, ValueError, json.JSONDecodeError):
				status = None
		if status == "committed":
			shutil.rmtree(transaction_dir)
		else:
			_restore_transaction(transaction_dir, data_dir)
		recovered += 1
	return recovered


def _prepare_purged_values(
	plan: PurgePlan,
	raw_capture_events: list[dict[str, Any]],
	entries: list[dict[str, Any]],
	user_actions: list[dict[str, Any]],
	watcher_state: dict[str, Any],
) -> dict[str, Any]:
	capture_indexes = set(plan.captured_event_indexes)
	action_indexes = set(plan.user_action_indexes)
	remaining_capture = [
		copy.deepcopy(event)
		for index, event in enumerate(raw_capture_events)
		if index not in capture_indexes
	]
	remaining_entries = [
		copy.deepcopy(entry)
		for index, entry in enumerate(entries)
		if index != plan.entry_index
	]
	remaining_actions = [
		copy.deepcopy(action)
		for index, action in enumerate(user_actions)
		if index not in action_indexes
	]
	remaining_state = copy.deepcopy(watcher_state)
	if plan.state_record_index is not None:
		records = remaining_state["files"][plan.state_source_file]["records"]
		record = records[plan.state_record_index]
		if record.get("record_id") != plan.state_record_id:
			raise ValueError("watcher_state.json 在准备事务前已发生变化")
		del records[plan.state_record_index]
	return {
		"captured_entries.jsonl": remaining_capture,
		"entries.json": remaining_entries,
		"user_actions.jsonl": remaining_actions,
		"watcher_state.json": remaining_state,
	}


def _validate_purged_values(
	plan: PurgePlan,
	prepared: dict[str, Any],
	original_capture: list[dict[str, Any]],
	original_entries: list[dict[str, Any]],
	original_actions: list[dict[str, Any]],
	original_state: dict[str, Any],
) -> None:
	remaining_capture = prepared["captured_entries.jsonl"]
	remaining_entries = prepared["entries.json"]
	remaining_actions = prepared["user_actions.jsonl"]
	remaining_state = prepared["watcher_state.json"]
	if len(remaining_capture) != len(original_capture) - len(plan.captured_event_indexes):
		raise ValueError("捕获事件删除数量与计划不一致")
	if len(remaining_entries) != len(original_entries) - 1:
		raise ValueError("entries.json 删除数量与计划不一致")
	if len(remaining_actions) != len(original_actions) - len(plan.user_action_indexes):
		raise ValueError("人工事件删除数量与计划不一致")
	if any(entry.get("id") == plan.record_id for entry in remaining_entries):
		raise ValueError("entries.json 仍包含目标 record_id")
	if any(action.get("record_id") == plan.record_id for action in remaining_actions):
		raise ValueError("user_actions.jsonl 仍包含目标 record_id")
	remaining_event_ids = {str(event.get("event_id")) for event in remaining_capture}
	if remaining_event_ids.intersection(plan.captured_event_ids):
		raise ValueError("captured_entries.jsonl 仍包含目标 event_id")
	if plan.state_record_id is not None:
		state_records = remaining_state["files"][plan.state_source_file]["records"]
		if any(record.get("record_id") == plan.state_record_id for record in state_records):
			raise ValueError("watcher_state.json 仍包含目标快照")
	_validate_non_target_fingerprints(plan, prepared, original_capture, original_entries, original_actions, original_state)


def _validate_non_target_fingerprints(
	plan: PurgePlan,
	prepared: dict[str, Any],
	original_capture: list[dict[str, Any]],
	original_entries: list[dict[str, Any]],
	original_actions: list[dict[str, Any]],
	original_state: dict[str, Any],
) -> None:
	def fingerprint(value: Any) -> str:
		return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

	expected_capture = [event for index, event in enumerate(original_capture) if index not in set(plan.captured_event_indexes)]
	expected_entries = [entry for index, entry in enumerate(original_entries) if index != plan.entry_index]
	expected_actions = [action for index, action in enumerate(original_actions) if index not in set(plan.user_action_indexes)]
	expected_state = copy.deepcopy(original_state)
	if plan.state_record_index is not None:
		del expected_state["files"][plan.state_source_file]["records"][plan.state_record_index]
	for name, expected in {
		"captured_entries.jsonl": expected_capture,
		"entries.json": expected_entries,
		"user_actions.jsonl": expected_actions,
		"watcher_state.json": expected_state,
	}.items():
		if fingerprint(prepared[name]) != fingerprint(expected):
			raise ValueError(f"{name} 中非目标数据发生变化")


def _write_prepared_files(directory: Path, prepared: dict[str, Any]) -> None:
	_write_jsonl(directory / "captured_entries.jsonl", prepared["captured_entries.jsonl"])
	_write_json_atomic(directory / "entries.json", prepared["entries.json"])
	_write_jsonl(directory / "user_actions.jsonl", prepared["user_actions.jsonl"])
	_write_json_atomic(directory / "watcher_state.json", prepared["watcher_state.json"])


def _validate_prepared_files(
	directory: Path,
	plan: PurgePlan,
	original_capture: list[dict[str, Any]],
	original_entries: list[dict[str, Any]],
	original_actions: list[dict[str, Any]],
	original_state: dict[str, Any],
) -> None:
	prepared = {
		"captured_entries.jsonl": _load_jsonl(directory / "captured_entries.jsonl", "captured_entries.jsonl"),
		"entries.json": _load_json_array(directory / "entries.json", "entries.json"),
		"user_actions.jsonl": _load_jsonl(directory / "user_actions.jsonl", "user_actions.jsonl"),
		"watcher_state.json": _load_json_object(directory / "watcher_state.json", "watcher_state.json"),
	}
	_validate_purged_values(plan, prepared, original_capture, original_entries, original_actions, original_state)
	remaining_events = load_captured_entries(directory / "captured_entries.jsonl")
	if remaining_events:
		fold_capture_events(remaining_events)


def _validate_live_files(
	paths: PurgePaths,
	plan: PurgePlan,
	original_capture: list[dict[str, Any]],
	original_entries: list[dict[str, Any]],
	original_actions: list[dict[str, Any]],
	original_state: dict[str, Any],
) -> None:
	prepared = {
		"captured_entries.jsonl": _load_jsonl(paths.captured_entries, "captured_entries.jsonl"),
		"entries.json": _load_json_array(paths.entries, "entries.json"),
		"user_actions.jsonl": _load_jsonl(paths.user_actions, "user_actions.jsonl"),
		"watcher_state.json": _load_json_object(paths.watcher_state, "watcher_state.json"),
	}
	_validate_purged_values(plan, prepared, original_capture, original_entries, original_actions, original_state)


def _restore_transaction(transaction_dir: Path, data_dir: Path) -> None:
	backup_dir = transaction_dir / "backup"
	if not backup_dir.is_dir():
		raise RuntimeError(f"彻底删除事务缺少备份，无法自动恢复：{transaction_dir.name}")
	manifest: dict[str, Any] = {}
	manifest_path = transaction_dir / "manifest.json"
	if manifest_path.is_file():
		try:
			manifest = _load_json_object(manifest_path, "purge manifest")
		except (OSError, ValueError, json.JSONDecodeError):
			manifest = {}
	in_place = set(manifest.get("in_place", []))
	project_dir = data_dir.parent
	for backup_path in sorted(backup_dir.rglob("*")):
		if not backup_path.is_file():
			continue
		relative = backup_path.relative_to(backup_dir)
		key = relative.as_posix()
		target_path = project_dir / relative if len(relative.parts) > 1 else data_dir / relative
		if key in in_place and target_path.exists():
			_rewrite_in_place(target_path, backup_path.read_bytes())
			continue
		target_path.parent.mkdir(parents=True, exist_ok=True)
		restore_path = target_path.with_name(f".{target_path.name}.purge-restore-{uuid.uuid4()}")
		shutil.copy2(backup_path, restore_path)
		_fsync_file(restore_path)
		os.replace(restore_path, target_path)
	shutil.rmtree(transaction_dir)


def _load_jsonl(path: Path, label: str) -> list[dict[str, Any]]:
	items: list[dict[str, Any]] = []
	with path.open("r", encoding="utf-8", newline="") as file:
		for line_number, line in enumerate(file, 1):
			if not line.strip():
				continue
			try:
				item = json.loads(line)
			except json.JSONDecodeError as error:
				raise ValueError(f"{label} 第 {line_number} 行不是有效 JSON") from error
			if not isinstance(item, dict):
				raise ValueError(f"{label} 第 {line_number} 行必须是对象")
			items.append(item)
	return items


def _load_json_array(path: Path, label: str) -> list[dict[str, Any]]:
	with path.open("r", encoding="utf-8") as file:
		value = json.load(file)
	if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
		raise ValueError(f"{label} 顶层必须是对象数组")
	return value


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
	with path.open("r", encoding="utf-8") as file:
		value = json.load(file)
	if not isinstance(value, dict):
		raise ValueError(f"{label} 顶层必须是对象")
	return value


def _write_jsonl(path: Path, items: list[dict[str, Any]]) -> None:
	with path.open("w", encoding="utf-8", newline="\n") as file:
		for item in items:
			file.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
			file.write("\n")
		file.flush()
		os.fsync(file.fileno())


def _serialize_structured_values(prepared: dict[str, Any]) -> dict[str, bytes]:
	def json_bytes(value: Any) -> bytes:
		return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

	def jsonl_bytes(items: list[dict[str, Any]]) -> bytes:
		return "".join(
			json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n"
			for item in items
		).encode("utf-8")

	return {
		"data/captured_entries.jsonl": jsonl_bytes(prepared["captured_entries.jsonl"]),
		"data/entries.json": json_bytes(prepared["entries.json"]),
		"data/user_actions.jsonl": jsonl_bytes(prepared["user_actions.jsonl"]),
		"data/watcher_state.json": json_bytes(prepared["watcher_state.json"]),
	}


def _relative_key(path: Path, project_dir: Path) -> str:
	try:
		return path.resolve().relative_to(project_dir).as_posix()
	except ValueError as error:
		raise ValueError(f"彻底删除目标不在项目目录内：{path}") from error


def _log_redaction_tokens(record_id: str, texts: list[str]) -> tuple[bytes, ...]:
	tokens = {record_id.encode("utf-8")}
	for text in texts:
		normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", " ")
		summary = normalized[:80]
		if summary:
			tokens.add(summary.encode("utf-8"))
	return tuple(sorted(tokens, key=len, reverse=True))


def _scrub_log_bytes(content: bytes, tokens: tuple[bytes, ...]) -> bytes:
	result = content
	for token in tokens:
		result = result.replace(token, b" " * len(token))
	return result


def _write_bytes(path: Path, content: bytes) -> None:
	with path.open("wb") as file:
		file.write(content)
		file.flush()
		os.fsync(file.fileno())


def _rewrite_in_place(path: Path, content: bytes) -> None:
	with path.open("r+b") as file:
		file.seek(0)
		file.write(content)
		file.truncate()
		file.flush()
		os.fsync(file.fileno())


def _write_json_atomic(path: Path, value: Any) -> None:
	temporary_path = path.with_name(f".{path.name}.tmp-{uuid.uuid4()}")
	with temporary_path.open("w", encoding="utf-8", newline="\n") as file:
		json.dump(value, file, ensure_ascii=False, indent=2)
		file.write("\n")
		file.flush()
		os.fsync(file.fileno())
	os.replace(temporary_path, path)


def _fsync_file(path: Path) -> None:
	with path.open("rb") as file:
		os.fsync(file.fileno())


def _build_capture_chains(events: list[dict[str, Any]]) -> dict[str, _CaptureChain]:
	chains: dict[str, _CaptureChain] = {}
	aliases: dict[str, str] = {}
	for index, event in enumerate(events):
		event_type = event.get("event_type")
		event_record_id = event.get("record_id")
		event_id = event.get("event_id")
		if event_type not in {"insert", "update"}:
			raise ValueError(f"捕获事件 {event_id!r} 的 event_type 无效")
		if not isinstance(event_record_id, str) or not event_record_id:
			raise ValueError(f"捕获事件 {event_id!r} 的 record_id 无效")
		if not isinstance(event_id, str) or not event_id:
			raise ValueError("捕获事件缺少有效 event_id")
		if event_type == "insert":
			if event_record_id in chains or event_record_id in aliases:
				raise ValueError(f"重复 insert record_id：{event_record_id}")
			chains[event_record_id] = _CaptureChain(
				canonical_id=event_record_id,
				record_ids={event_record_id},
				event_indexes=[index],
				event_ids=[event_id],
				text=str(event["text"]),
				source_file=str(event["source_file"]),
			)
			continue

		canonical_id = aliases.get(event_record_id)
		if canonical_id is None and event_record_id in chains:
			canonical_id = event_record_id
		if canonical_id is None:
			previous_hash = event.get("previous_text_sha256")
			matches = [
				candidate_id
				for candidate_id, candidate in chains.items()
				if candidate.source_file == event.get("source_file")
				and text_sha256(candidate.text) == previous_hash
			]
			if len(matches) != 1:
				raise ValueError(f"update {event_id} 无法唯一匹配 previous_text_sha256")
			canonical_id = matches[0]
			aliases[event_record_id] = canonical_id
		chain = chains[canonical_id]
		if event.get("previous_text_sha256") != text_sha256(chain.text):
			raise ValueError(f"update {event_id} 的 previous_text_sha256 不连续")
		chain.record_ids.add(event_record_id)
		chain.event_indexes.append(index)
		chain.event_ids.append(event_id)
		chain.text = str(event["text"])
		chain.source_file = str(event["source_file"])
	return chains


def _locate_state_record(
	target: _CaptureChain,
	chains: dict[str, _CaptureChain],
	watcher_state: dict[str, Any],
) -> tuple[str, int | None, str | None, str]:
	files = watcher_state.get("files")
	if not isinstance(files, dict):
		raise ValueError("watcher_state.json 缺少 files 对象")
	file_state = files.get(target.source_file)
	if not isinstance(file_state, dict) or not isinstance(file_state.get("records"), list):
		raise ValueError(f"watcher_state.json 找不到来源文件：{target.source_file}")
	records = file_state["records"]
	direct = [
		(index, record)
		for index, record in enumerate(records)
		if isinstance(record, dict) and record.get("record_id") in target.record_ids
	]
	if len(direct) == 1:
		index, record = direct[0]
		return target.source_file, index, str(record["record_id"]), "record_id"
	if len(direct) > 1:
		raise ValueError("watcher_state.json 中目标 record_id 命中多条快照")

	target_hash = text_sha256(target.text)
	hash_candidates = [
		(index, record)
		for index, record in enumerate(records)
		if isinstance(record, dict) and text_sha256(str(record.get("text", ""))) == target_hash
	]
	chains_with_same_snapshot = [
		chain
		for chain in chains.values()
		if chain.source_file == target.source_file and text_sha256(chain.text) == target_hash
	]
	if not hash_candidates:
		return target.source_file, None, None, "not_present"
	if len(hash_candidates) != 1 or len(chains_with_same_snapshot) != 1:
		raise ValueError("迁移快照无法通过来源文件和精确正文哈希唯一定位")
	index, record = hash_candidates[0]
	state_record_id = record.get("record_id")
	if not isinstance(state_record_id, str) or not state_record_id:
		raise ValueError("迁移快照缺少有效 record_id")
	return target.source_file, index, state_record_id, "migration_exact_hash"