"""Build one fully offline review.html from the JSON source data."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from llm_adapter import (
	LLMError,
	VALID_STATUSES,
	normalize_display_markdown,
	validate_categories,
	validate_display_markdown,
)
from table_layout import prepare_table_display


PROJECT_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = PROJECT_DIR / "config" / "settings.json"
CATEGORIES_PATH = PROJECT_DIR / "config" / "categories.json"
ENTRIES_PATH = PROJECT_DIR / "data" / "entries.json"
TEMPLATE_PATH = PROJECT_DIR / "output" / "review-mock.html"
OUTPUT_PATH = PROJECT_DIR / "output" / "review.html"
MARKDOWN_IT_PATH = PROJECT_DIR / "vendor" / "markdown-it.min.js"
REQUIRED_ENTRY_KEYS = {
	"id",
	"text",
	"record_time",
	"event_time",
	"project",
	"category",
	"status",
	"source_file",
	"user_edited",
	"created_at",
	"updated_at",
}


def log(message: str, level: str = "INFO") -> None:
	now = datetime.now().astimezone().isoformat(timespec="seconds")
	print(f"[{now}] [{level}] {message}", flush=True)


def load_json(path: Path) -> Any:
	with path.open("r", encoding="utf-8") as file:
		return json.load(file)


def validate_entries(entries: Any, category_codes: set[str]) -> list[dict[str, Any]]:
	"""Validate the stable English-key data contract used by the page."""
	if not isinstance(entries, list):
		raise ValueError("entries.json 的顶层必须是数组")

	seen_ids: set[str] = set()
	for index, entry in enumerate(entries, start=1):
		if not isinstance(entry, dict) or not REQUIRED_ENTRY_KEYS <= entry.keys():
			raise ValueError(f"entries.json 第 {index} 条缺少必要的英文字段")
		entry_id = entry["id"]
		if not isinstance(entry_id, str) or not entry_id:
			raise ValueError(f"entries.json 第 {index} 条的 id 无效")
		if entry_id in seen_ids:
			raise ValueError(f"entries.json 存在重复 id：{entry_id}")
		if not isinstance(entry["text"], str) or not isinstance(entry["project"], str):
			raise ValueError(f"entries.json 第 {index} 条的 text/project 必须是字符串")
		if entry["category"] not in category_codes:
			raise ValueError(f"entries.json 第 {index} 条包含未知 category")
		if entry["status"] not in VALID_STATUSES:
			raise ValueError(f"entries.json 第 {index} 条包含未知 status")
		if not isinstance(entry["user_edited"], bool):
			raise ValueError(f"entries.json 第 {index} 条的 user_edited 必须是布尔值")
		hidden = entry.get("hidden", False)
		if not isinstance(hidden, bool):
			raise ValueError(f"entries.json 第 {index} 条的 hidden 必须是布尔值")
		if hidden:
			validate_time(entry.get("hidden_at"), f"第 {index} 条的 hidden_at")
		elif "hidden_at" in entry:
			raise ValueError(f"entries.json 第 {index} 条未隐藏时不能包含 hidden_at")
		for key in ("record_time", "created_at", "updated_at"):
			validate_time(entry[key], f"第 {index} 条的 {key}")
		if entry["event_time"] is not None:
			validate_time(entry["event_time"], f"第 {index} 条的 event_time")
		seen_ids.add(entry_id)
	return entries


def validate_time(value: Any, field_name: str) -> None:
	if not isinstance(value, str):
		raise ValueError(f"{field_name} 必须是 ISO 8601 字符串")
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
	except ValueError as error:
		raise ValueError(f"{field_name} 不是有效的 ISO 8601 时间") from error
	if parsed.tzinfo is None:
		raise ValueError(f"{field_name} 必须包含时区")


def javascript_json(value: Any) -> str:
	"""Serialize JSON without allowing note text to terminate the script tag."""
	return (
		json.dumps(value, ensure_ascii=False, indent=2)
		.replace("<", "\\u003c")
		.replace(">", "\\u003e")
		.replace("&", "\\u0026")
		.replace("\u2028", "\\u2028")
		.replace("\u2029", "\\u2029")
	)


def prepare_entries_for_display(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Copy entries and remove any stale or invalid Markdown cache from the page payload."""
	prepared = copy.deepcopy(entries)
	for entry in prepared:
		text = entry.get("text")
		markdown = entry.get("display_markdown")
		expected_hash = entry.get("display_text_sha256")
		valid = isinstance(text, str) and isinstance(markdown, str)
		if valid:
			actual_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
			valid = expected_hash == actual_hash
		if valid:
			try:
				validate_display_markdown(text, markdown)
				entry["display_markdown"] = normalize_display_markdown(text, markdown)
			except LLMError:
				valid = False
		if not valid:
			entry.pop("display_markdown", None)
			entry.pop("display_text_sha256", None)
			entry.pop("display_generated_at", None)
		if isinstance(text, str):
			tables, fallbacks = prepare_table_display(text, entry.get("display_tables"))
			entry["_display_tables"] = tables
			entry["_display_table_fallbacks"] = fallbacks
	return prepared


def replace_marked(template: str, name: str, content: str) -> str:
	start_marker = f"/* BUILD:{name}:START */"
	end_marker = f"/* BUILD:{name}:END */"
	if template.count(start_marker) != 1 or template.count(end_marker) != 1:
		raise ValueError(f"HTML 模板缺少唯一标记：{name}")
	before, remainder = template.split(start_marker, 1)
	_, after = remainder.split(end_marker, 1)
	return before + start_marker + "\n" + content + "\n    " + end_marker + after


def build_html(
	entries: list[dict[str, Any]],
	categories: list[dict[str, Any]],
	language: str,
	collapsed_lines: int,
	markdown_it_source: str,
	template: str,
) -> str:
	if not isinstance(language, str) or not language:
		raise ValueError("display.language 必须是非空字符串")
	if not isinstance(collapsed_lines, int) or isinstance(collapsed_lines, bool) or collapsed_lines < 1:
		raise ValueError("display.collapsed_lines 必须是大于 0 的整数")
	if not isinstance(markdown_it_source, str) or not markdown_it_source.strip():
		raise ValueError("markdown-it 本地库为空")
	if "</script" in markdown_it_source.lower():
		raise ValueError("markdown-it 本地库包含不安全的 script 结束标记")
	entries_for_display = prepare_entries_for_display(entries)
	template = replace_marked(template, "MARKDOWN_IT", markdown_it_source)
	template = replace_marked(
		template,
		"CATEGORIES",
		"    const categories = " + javascript_json(categories) + ";",
	)
	template = replace_marked(
		template,
		"ENTRIES",
		"    const entries = " + javascript_json(entries_for_display) + ";",
	)
	template = replace_marked(
		template,
		"LANGUAGE",
		"    const language = " + javascript_json(language) + ";",
	)
	template = replace_marked(
		template,
		"COLLAPSED_LINES",
		f"    const collapsedLines = {collapsed_lines};",
	)
	template = replace_marked(
		template,
		"FOOTER",
		"由 entries.json 生成 · 数据已内嵌，可离线只读使用",
	)
	return template.replace("笔记回顾 - 交互原型", "笔记回顾")


def main() -> None:
	log("HTML 生成程序已启动。")
	try:
		settings = load_json(SETTINGS_PATH)
		categories = validate_categories(load_json(CATEGORIES_PATH))
		category_codes = {category["code"] for category in categories}
		entries = validate_entries(load_json(ENTRIES_PATH), category_codes)
		display_settings = settings.get("display", {})
		language = display_settings.get("language", "zh-CN")
		collapsed_lines = display_settings.get("collapsed_lines", 4)
		template = TEMPLATE_PATH.read_text(encoding="utf-8")
		markdown_it_source = MARKDOWN_IT_PATH.read_text(encoding="utf-8")
		output = build_html(
			entries,
			categories,
			language,
			collapsed_lines,
			markdown_it_source,
			template,
		)
		OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
		temporary_path = OUTPUT_PATH.with_suffix(".tmp")
		temporary_path.write_text(output, encoding="utf-8")
		temporary_path.replace(OUTPUT_PATH)
	except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
		log(f"生成失败，请检查数据、配置和模板：{error}", "ERROR")
		raise SystemExit(1) from error

	log(f"已生成 {OUTPUT_PATH}，共嵌入 {len(entries)} 条记录。")
	log("可以直接双击 review.html，或运行：open output/review.html")


if __name__ == "__main__":
	main()
