"""Watch text files and capture completed note records."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Sequence


PROJECT_DIR = Path(__file__).resolve().parent
NOTES_DIR = PROJECT_DIR / "notes"
SETTINGS_PATH = PROJECT_DIR / "config" / "settings.json"
CAPTURED_ENTRIES_PATH = PROJECT_DIR / "data" / "captured_entries.jsonl"
STATE_PATH = PROJECT_DIR / "data" / "watcher_state.json"
LOG_PATH = PROJECT_DIR / "data" / "watcher.log"
SCAN_LOCK_PATH = PROJECT_DIR / "data" / "watcher-scan.lock"
LOGGER = logging.getLogger("my-notes-tool.watcher")


@dataclass(frozen=True)
class DiffSettings:
	update_similarity_threshold: float = 0.85
	short_record_similarity_threshold: float = 0.95
	short_record_character_limit: int = 80
	minimum_match_margin: float = 0.10


class IsoFormatter(logging.Formatter):
	def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
		return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")


def configure_logging(log_path: Path, max_bytes: int, backup_count: int) -> None:
	"""Send every message to the terminal and rotating log files."""
	log_path.parent.mkdir(parents=True, exist_ok=True)
	LOGGER.setLevel(logging.INFO)
	LOGGER.propagate = False
	for handler in LOGGER.handlers[:]:
		handler.close()
		LOGGER.removeHandler(handler)

	formatter = IsoFormatter("[%(asctime)s] [%(levelname)s] %(message)s")
	console_handler = logging.StreamHandler(sys.stdout)
	console_handler.setFormatter(formatter)
	LOGGER.addHandler(console_handler)

	file_handler = RotatingFileHandler(
		log_path,
		maxBytes=max_bytes,
		backupCount=backup_count,
		encoding="utf-8",
	)
	file_handler.setFormatter(formatter)
	LOGGER.addHandler(file_handler)


def request_stop(signum: int, frame: Any) -> None:
	"""Route launchd termination through the normal shutdown log path."""
	raise KeyboardInterrupt


def log(message: str, level: str = "INFO") -> None:
	"""Log an event to both the terminal and rotating log file."""
	level_number = getattr(logging, level.upper(), logging.INFO)
	if LOGGER.handlers:
		LOGGER.log(level_number, message)
		return
	now = datetime.now().astimezone().isoformat(timespec="seconds")
	print(f"[{now}] [{level.upper()}] {message}", flush=True)


def split_complete_records(text: str, blank_lines: int) -> tuple[list[str], str]:
	"""Return completed records and the unfinished text at the end."""
	if blank_lines < 1:
		raise ValueError("blank_lines must be at least 1")

	# A blank line may contain spaces or tabs. The first newline ends the
	# content line; each following newline completes one blank line.
	separator = re.compile(
		rf"\r?\n(?:[ \t]*\r?\n){{{blank_lines},}}"
	)
	parts = separator.split(text)
	records = [part.strip("\r\n") for part in parts[:-1] if part.strip()]
	return records, parts[-1]


def load_json(path: Path) -> Any:
	"""Load a JSON file and let the caller explain errors to the user."""
	with path.open("r", encoding="utf-8") as file:
		return json.load(file)


def save_state(path: Path, state: dict[str, Any]) -> None:
	"""Replace the state file atomically to avoid a half-written JSON file."""
	temporary_path = path.with_suffix(".tmp")
	with temporary_path.open("w", encoding="utf-8") as file:
		json.dump(state, file, ensure_ascii=False, indent=2)
		file.write("\n")
	temporary_path.replace(path)


def append_entries(path: Path, entries: list[dict[str, str]]) -> None:
	"""Append immutable captured records as one JSON object per line."""
	with path.open("a", encoding="utf-8") as file:
		for entry in entries:
			file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def file_hash(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


def make_insert_event(note_path: Path, text: str, record_id: str) -> dict[str, str]:
	return {
		"event_id": str(uuid.uuid4()),
		"event_type": "insert",
		"record_id": record_id,
		# Keep id equal to the logical record ID for the current organizer contract.
		"id": record_id,
		"text": text,
		"record_time": datetime.now().astimezone().isoformat(timespec="seconds"),
		"project": note_path.stem,
		"source_file": note_path.name,
	}


def make_update_event(
	note_path: Path,
	old_text: str,
	new_text: str,
	record_id: str,
	similarity: float,
) -> dict[str, str | float]:
	return {
		"event_id": str(uuid.uuid4()),
		"event_type": "update",
		"record_id": record_id,
		"id": record_id,
		"text": new_text,
		"previous_text_sha256": hashlib.sha256(old_text.encode("utf-8")).hexdigest(),
		"similarity": round(similarity, 6),
		"record_time": datetime.now().astimezone().isoformat(timespec="seconds"),
		"project": note_path.stem,
		"source_file": note_path.name,
	}


def text_similarity(left: str, right: str) -> float:
	return SequenceMatcher(None, left, right, autojunk=False).ratio()


def update_threshold(left: str, right: str, settings: DiffSettings) -> float:
	if min(len(left), len(right)) < settings.short_record_character_limit:
		return settings.short_record_similarity_threshold
	return settings.update_similarity_threshold


def match_updates(
	old_records: list[dict[str, str]],
	new_texts: list[str],
	settings: DiffSettings,
) -> dict[int, tuple[int, float]]:
	"""Return unambiguous mutual-best new-index -> (old-index, score) matches."""
	if not old_records or not new_texts:
		return {}
	scores = [
		[text_similarity(old["text"], new_text) for new_text in new_texts]
		for old in old_records
	]
	matches: dict[int, tuple[int, float]] = {}
	used_old: set[int] = set()
	for new_index in range(len(new_texts)):
		ranked_old = sorted(
			((scores[old_index][new_index], old_index) for old_index in range(len(old_records))),
			reverse=True,
		)
		best_score, best_old = ranked_old[0]
		second_score = ranked_old[1][0] if len(ranked_old) > 1 else 0.0
		threshold = update_threshold(old_records[best_old]["text"], new_texts[new_index], settings)
		if best_score < threshold or best_score - second_score < settings.minimum_match_margin:
			continue
		old_best_new = max(
			range(len(new_texts)),
			key=lambda candidate_index: scores[best_old][candidate_index],
		)
		if old_best_new != new_index or best_old in used_old:
			continue
		matches[new_index] = (best_old, best_score)
		used_old.add(best_old)
	return matches


def diff_insert_records(
	old_records: list[dict[str, str]],
	new_texts: list[str],
	note_path: Path,
	settings: DiffSettings | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
	"""Carry stable IDs across exact matches and conservative update matches."""
	settings = settings or DiffSettings()
	old_texts = [record["text"] for record in old_records]
	matcher = SequenceMatcher(None, old_texts, new_texts, autojunk=False)
	new_records: list[dict[str, str]] = []
	events: list[dict[str, Any]] = []
	for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
		if tag == "equal":
			new_records.extend(old_records[old_start:old_end])
		elif tag == "insert":
			for text in new_texts[new_start:new_end]:
				record_id = str(uuid.uuid4())
				new_records.append({"record_id": record_id, "text": text})
				events.append(make_insert_event(note_path, text, record_id))
		elif tag == "replace":
			old_slice = old_records[old_start:old_end]
			new_slice = new_texts[new_start:new_end]
			update_matches = match_updates(old_slice, new_slice, settings)
			for index, text in enumerate(new_slice):
				match = update_matches.get(index)
				if match:
					old_index, similarity = match
					old_record = old_slice[old_index]
					record_id = old_record["record_id"]
					events.append(
						make_update_event(note_path, old_record["text"], text, record_id, similarity)
					)
				else:
					record_id = str(uuid.uuid4())
					events.append(make_insert_event(note_path, text, record_id))
				new_records.append({"record_id": record_id, "text": text})
		# delete opcodes intentionally emit nothing and disappear from the baseline.
	return new_records, events


def capture_file(
	note_path: Path,
	file_state: dict[str, Any] | None,
	blank_lines: int,
	captured_entries_path: Path,
	diff_settings: DiffSettings | None = None,
) -> tuple[dict[str, Any], int]:
	"""Compare all complete records and append insert events at any position."""
	try:
		text = note_path.read_text(encoding="utf-8")
	except UnicodeDecodeError as error:
		log(f"无法读取 {note_path.name}：请将文件保存为 UTF-8。详情：{error}", "ERROR")
		return file_state or {}, 0
	complete_texts, pending_text = split_complete_records(text, blank_lines)

	if file_state and "records" not in file_state:
		# Migrate the deployed offset state by accepting the current file as its
		# baseline. Existing captures must not be duplicated during upgrade.
		migrated_records = [
			{"record_id": str(uuid.uuid4()), "text": record}
			for record in complete_texts
		]
		return {"records": migrated_records, "pending_text": pending_text}, 0

	old_records = list(file_state.get("records", [])) if file_state else []
	new_records, events = diff_insert_records(
		old_records,
		complete_texts,
		note_path,
		diff_settings,
	)
	append_entries(captured_entries_path, events)
	return {"records": new_records, "pending_text": pending_text}, len(events)


def scan_notes(
	notes_dir: Path,
	state_path: Path,
	captured_entries_path: Path,
	blank_lines: int,
	diff_settings: DiffSettings | None = None,
) -> int:
	"""Scan all project text files once and persist their read positions."""
	state = load_json(state_path)
	files_state = state.setdefault("files", {})
	total_captured = 0

	for note_path in sorted(notes_dir.glob("*.txt")):
		relative_name = note_path.name
		previous_state = files_state.get(relative_name)
		try:
			new_state, captured_count = capture_file(
				note_path,
				previous_state,
				blank_lines,
				captured_entries_path,
				diff_settings,
			)
		except OSError as error:
			log(f"读取 {relative_name} 失败：{error}", "ERROR")
			continue

		files_state[relative_name] = new_state
		if captured_count:
			log(f"检测到 {relative_name} 内容变化，记录了 {captured_count} 条 insert/update 事件。")
		elif new_state.get("pending_text") and new_state != previous_state:
			log(f"检测到 {relative_name} 有新增内容，正在等待完整分隔符。")
		total_captured += captured_count

	save_state(state_path, state)
	return total_captured


def scan_notes_with_lock(
	notes_dir: Path,
	state_path: Path,
	captured_entries_path: Path,
	blank_lines: int,
	diff_settings: DiffSettings | None = None,
	lock_path: Path = SCAN_LOCK_PATH,
) -> int:
	"""Serialize manual and background scans around state and event writes."""
	lock_path.parent.mkdir(parents=True, exist_ok=True)
	with lock_path.open("a+", encoding="utf-8") as lock_file:
		fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
		try:
			return scan_notes(
				notes_dir,
				state_path,
				captured_entries_path,
				blank_lines,
				diff_settings,
			)
		finally:
			fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def scan_summary(captured_count: int) -> str:
	if captured_count:
		return f"[扫描] 已检查 notes/，发现 {captured_count} 条新增"
	return "[扫描] 已检查 notes/，无新增"


def read_settings() -> tuple[int, float, bool, int, int, DiffSettings]:
	settings = load_json(SETTINGS_PATH)
	blank_lines = int(settings["record_separator"]["blank_lines"])
	watcher_settings = settings["watcher"]
	poll_interval = float(watcher_settings["poll_interval_seconds"])
	log_empty_scans = watcher_settings.get("log_empty_scans", True)
	logging_settings = watcher_settings["logging"]
	diff_values = watcher_settings.get("diff", {})
	diff_settings = DiffSettings(
		update_similarity_threshold=float(diff_values.get("update_similarity_threshold", 0.85)),
		short_record_similarity_threshold=float(diff_values.get("short_record_similarity_threshold", 0.95)),
		short_record_character_limit=int(diff_values.get("short_record_character_limit", 80)),
		minimum_match_margin=float(diff_values.get("minimum_match_margin", 0.10)),
	)
	max_bytes = int(logging_settings["max_bytes"])
	backup_count = int(logging_settings["backup_count"])
	if (
		blank_lines < 1
		or poll_interval <= 0
		or max_bytes < 1
		or backup_count < 0
		or not 0 <= diff_settings.update_similarity_threshold <= 1
		or not 0 <= diff_settings.short_record_similarity_threshold <= 1
		or diff_settings.short_record_character_limit < 1
		or not 0 <= diff_settings.minimum_match_margin <= 1
	):
		raise ValueError("分隔空行数、扫描间隔和日志大小必须大于 0；归档数量不能小于 0")
	if not isinstance(log_empty_scans, bool):
		raise ValueError("log_empty_scans 必须是布尔值")
	return (
		blank_lines,
		poll_interval,
		log_empty_scans,
		max_bytes,
		backup_count,
		diff_settings,
	)


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="监听或单次扫描 TXT 笔记。")
	parser.add_argument("--once", action="store_true", help="扫描一次后退出。")
	return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
	"""Run note scans on the configured polling schedule."""
	args = parse_args(arguments)
	try:
		(
			blank_lines,
			poll_interval,
			log_empty_scans,
			max_bytes,
			backup_count,
			diff_settings,
		) = read_settings()
		state = load_json(STATE_PATH)
		if not isinstance(state, dict):
			raise ValueError("watcher_state.json 的顶层必须是一个 JSON 对象")
	except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
		log(f"启动失败，请检查配置和状态文件：{error}", "ERROR")
		raise SystemExit(1) from error

	NOTES_DIR.mkdir(parents=True, exist_ok=True)
	CAPTURED_ENTRIES_PATH.parent.mkdir(parents=True, exist_ok=True)
	CAPTURED_ENTRIES_PATH.touch(exist_ok=True)
	configure_logging(LOG_PATH, max_bytes, backup_count)
	if args.once:
		try:
			captured_count = scan_notes_with_lock(
				NOTES_DIR,
				STATE_PATH,
				CAPTURED_ENTRIES_PATH,
				blank_lines,
				diff_settings,
			)
		except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
			log(f"手动扫描失败：{error}", "ERROR")
			raise SystemExit(1) from error
		log(scan_summary(captured_count))
		return
	signal.signal(signal.SIGTERM, request_stop)

	log("笔记监听器已启动。按 Ctrl+C 可以安全停止。")
	log(f"监听目录：{NOTES_DIR}")
	log(
		f"每 {poll_interval:g} 秒扫描一次；连续 {blank_lines} 个空白行结束一条记录；"
		f"无新增扫描日志：{'开启' if log_empty_scans else '关闭'}。"
	)
	log(
		f"日志单文件上限 {max_bytes} 字节，保留 {backup_count} 个归档。"
	)

	next_scan = time.monotonic()
	try:
		while True:
			now = time.monotonic()
			if now >= next_scan:
				try:
					captured_count = scan_notes_with_lock(
						NOTES_DIR,
						STATE_PATH,
						CAPTURED_ENTRIES_PATH,
						blank_lines,
						diff_settings,
					)
					if captured_count or log_empty_scans:
						log(scan_summary(captured_count))
				except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
					log(f"本次扫描失败，将在下个周期重试：{error}", "ERROR")
				next_scan = time.monotonic() + poll_interval

			time.sleep(max(0.1, next_scan - time.monotonic()))
	except KeyboardInterrupt:
		log("收到停止指令，笔记监听器已安全退出。")


if __name__ == "__main__":
	main()
