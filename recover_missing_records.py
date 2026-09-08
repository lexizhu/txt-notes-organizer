"""One-time recovery for complete records missed by the append-only watcher."""

from __future__ import annotations

import argparse
import json
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence

from watcher import PROJECT_DIR, split_complete_records


CAPTURED_ENTRIES_PATH = PROJECT_DIR / "data" / "captured_entries.jsonl"


def load_captured(path: Path) -> list[dict[str, Any]]:
	entries: list[dict[str, Any]] = []
	for line in path.read_text(encoding="utf-8").splitlines():
		if line.strip():
			entries.append(json.loads(line))
	return entries


def similarity(left: str, right: str) -> float:
	return SequenceMatcher(None, left.splitlines(), right.splitlines(), autojunk=False).ratio()


def find_missing_records(
	current_records: list[str],
	captured_entries: list[dict[str, Any]],
	source_file: str,
	threshold: float = 0.90,
) -> list[str]:
	"""Return records with no exact or high-similarity captured version."""
	captured_texts = [
		str(entry["text"])
		for entry in captured_entries
		if entry.get("source_file") == source_file
	]
	return [
		record
		for record in current_records
		if not any(similarity(record, captured) >= threshold for captured in captured_texts)
	]


def append_recovered(
	path: Path,
	records: list[str],
	source_file: str,
	record_time: str,
) -> None:
	with path.open("a", encoding="utf-8") as file:
		for record in records:
			entry = {
				"id": str(uuid.uuid4()),
				"text": record,
				"record_time": record_time,
				"project": Path(source_file).stem,
				"source_file": source_file,
			}
			file.write(json.dumps(entry, ensure_ascii=False) + "\n")


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="预览并救回 watcher 漏掉的完整记录。")
	parser.add_argument("source_file", help="notes/ 下的 TXT 文件名")
	parser.add_argument("--record-time", required=True, help="近似捕获时间（ISO 8601）")
	parser.add_argument("--apply", action="store_true", help="确认追加缺失记录；省略时只预览")
	return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
	args = parse_args(arguments)
	note_path = PROJECT_DIR / "notes" / args.source_file
	text = note_path.read_text(encoding="utf-8")
	settings = json.loads((PROJECT_DIR / "config" / "settings.json").read_text(encoding="utf-8"))
	blank_lines = int(settings["record_separator"]["blank_lines"])
	records, pending = split_complete_records(text, blank_lines)
	if pending.strip():
		print("提示：文件末尾还有未完成记录，本工具不会追加它。", flush=True)
	missing = find_missing_records(records, load_captured(CAPTURED_ENTRIES_PATH), args.source_file)
	print(f"当前完整记录 {len(records)} 条；真正缺失 {len(missing)} 条。", flush=True)
	for index, record in enumerate(missing, start=1):
		print(f"  缺失 {index}：{record.splitlines()[0][:100]}", flush=True)
	if not args.apply:
		print("当前为预览模式，没有写入。确认后加 --apply。", flush=True)
		return
	append_recovered(CAPTURED_ENTRIES_PATH, missing, args.source_file, args.record_time)
	print(f"已向 captured_entries.jsonl 追加 {len(missing)} 条恢复记录。", flush=True)


if __name__ == "__main__":
	main()