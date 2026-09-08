"""Tests for safe LLM-generated display Markdown and its cache."""

import json
import inspect
import tempfile
import unittest
from pathlib import Path

from llm_adapter import (
	LLMError,
	OllamaAdapter,
	apply_line_styles,
	apply_sparse_line_styles,
	normalize_display_markdown,
	validate_display_markdown,
)
from organize import format_display_entries, has_valid_display_cache, text_sha256


class FakeFormatter:
	def __init__(self, result: str | None = None, should_fail: bool = False) -> None:
		self.result = result
		self.should_fail = should_fail
		self.calls: list[str] = []

	def format_markdown(self, text: str) -> str:
		self.calls.append(text)
		if self.should_fail:
			raise LLMError("测试排版失败")
		return self.result if self.result is not None else text


class DisplayMarkdownValidationTests(unittest.TestCase):
	def test_accepts_only_allowed_markers_with_exact_text(self) -> None:
		original = "会议标题\n重点内容\n子项"
		formatted = "# 会议标题\n**重点内容**\n  - 子项"

		self.assertEqual(validate_display_markdown(original, formatted), formatted)

	def test_rejects_changed_character(self) -> None:
		with self.assertRaisesRegex(LLMError, "修改"):
			validate_display_markdown("原始文字", "**润色文字**")

	def test_rejects_added_or_removed_line(self) -> None:
		with self.assertRaisesRegex(LLMError, "行数"):
			validate_display_markdown("第一行\n第二行", "# 第一行")

	def test_rejects_unpaired_bold_marker(self) -> None:
		with self.assertRaisesRegex(LLMError, "未配对"):
			validate_display_markdown("重点", "**重点")

	def test_prompt_forbids_all_content_edits(self) -> None:
		source = inspect.getsource(OllamaAdapter.format_markdown)

		self.assertIn("不得返回或改写原文", source)
		self.assertIn("不确定的行不要返回", source)
		self.assertIn('"minimum": 1', source)
		self.assertIn('"maximum": len(original_lines)', source)
		self.assertIn('"styles"', source)

	def test_sparse_styles_leave_omitted_lines_plain(self) -> None:
		formatted = apply_sparse_line_styles(
			"标题\n普通正文\n重点",
			[{"line": 1, "style": "h1"}, {"line": 3, "style": "bold"}],
		)

		self.assertEqual(formatted, "# 标题\n普通正文\n**重点**")

	def test_sparse_styles_reject_duplicate_or_out_of_range_lines(self) -> None:
		with self.assertRaisesRegex(LLMError, "重复行号"):
			apply_sparse_line_styles(
				"标题\n正文",
				[{"line": 1, "style": "h1"}, {"line": 1, "style": "bold"}],
			)
		with self.assertRaisesRegex(LLMError, "无效行号"):
			apply_sparse_line_styles("标题", [{"line": 2, "style": "h1"}])

	def test_line_styles_are_applied_to_original_text(self) -> None:
		original = "标题\n\n章节\n事项一\n事项二\n子项一\n子项二\n重点"

		formatted = apply_line_styles(
			original,
			["h1", "h2", "h2", "bullet", "bullet", "sub_bullet", "sub_bullet", "bold"],
		)

		self.assertEqual(
			formatted,
			"# 标题\n\n## 章节\n- 事项一\n- 事项二\n   - 子项一\n   - 子项二\n**重点**",
		)

	def test_line_styles_reject_wrong_count(self) -> None:
		with self.assertRaisesRegex(LLMError, "数量"):
			apply_line_styles("第一行\n第二行", ["plain"])

	def test_chinese_section_headings_are_deterministic(self) -> None:
		original = "记录标题\n一、 章节一\n二，章节二\n三. 章节三\n四．章节四"

		formatted = apply_line_styles(original, ["plain"] * 5)

		self.assertEqual(
			formatted,
			"# 记录标题\n## 一、 章节一\n## 二，章节二\n## 三. 章节三\n## 四．章节四",
		)

	def test_numbered_item_never_gets_double_marker(self) -> None:
		original = "标题\n1. 角色定位\n说明一\n说明二"

		formatted = apply_line_styles(original, ["plain", "bullet", "sub_bullet", "sub_bullet"])

		self.assertIn("\n1. 角色定位\n", formatted)
		self.assertNotIn("- 1. 角色定位", formatted)
		self.assertIn("   - 说明一", formatted)

	def test_isolated_numbered_subsection_becomes_heading(self) -> None:
		original = "标题\n\n3. 工具功能\n说明\n\n4． 关键时间线\n说明"

		formatted = apply_line_styles(original, ["plain"] * 7)

		self.assertIn("## 3. 工具功能", formatted)
		self.assertIn("## 4． 关键时间线", formatted)

	def test_consecutive_numbered_lines_remain_ordered_list(self) -> None:
		original = "标题\n\n1. 第一项\n2. 第二项\n3. 第三项"

		formatted = apply_line_styles(original, ["plain"] * 5)

		self.assertIn("\n1. 第一项\n2. 第二项\n3. 第三项", formatted)
		self.assertNotIn("## 1. 第一项", formatted)

	def test_empty_and_isolated_bullets_are_removed(self) -> None:
		original = "标题\n\n孤立内容\n普通内容"

		formatted = apply_line_styles(original, ["bullet", "bullet", "bullet", "plain"])

		self.assertEqual(formatted, "# 标题\n\n孤立内容\n普通内容")

	def test_existing_cache_is_normalized_without_llm(self) -> None:
		original = "标题\n三、章节\n1. 角色定位"
		old_cache = "# 标题\n三、章节\n- 1. 角色定位"

		normalized = normalize_display_markdown(original, old_cache)

		self.assertEqual(normalized, "# 标题\n## 三、章节\n1. 角色定位")


class DisplayMarkdownCacheTests(unittest.TestCase):
	def test_force_refreshes_only_target_ids(self) -> None:
		entries = [
			{
				"id": "one",
				"text": "目标原文",
				"display_markdown": "目标原文",
				"display_text_sha256": text_sha256("目标原文"),
			},
			{
				"id": "two",
				"text": "保留原文",
				"display_markdown": "保留原文",
				"display_text_sha256": text_sha256("保留原文"),
			},
		]
		formatter = FakeFormatter("**目标原文**")
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "entries.json"
			path.write_text("[]\n", encoding="utf-8")

			counts = format_display_entries(
				entries,
				formatter,
				path,
				target_ids={"one"},
				force=True,
			)
			saved = json.loads(path.read_text(encoding="utf-8"))

		self.assertEqual(counts, (1, 0, 1))
		self.assertEqual(formatter.calls, ["目标原文"])
		self.assertEqual(saved[0]["display_markdown"], "**目标原文**")
		self.assertEqual(saved[1]["display_markdown"], "保留原文")

	def test_valid_hash_cache_skips_formatter(self) -> None:
		text = "原文"
		entry = {
			"id": "one",
			"text": text,
			"display_markdown": "**原文**",
			"display_text_sha256": text_sha256(text),
			"display_generated_at": "2026-08-24T10:00:00+08:00",
		}
		formatter = FakeFormatter()
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "entries.json"
			path.write_text("[]\n", encoding="utf-8")

			counts = format_display_entries([entry], formatter, path)

		self.assertEqual(counts, (0, 0, 1))
		self.assertEqual(formatter.calls, [])
		self.assertTrue(has_valid_display_cache(entry))

	def test_changed_text_hash_regenerates_cache(self) -> None:
		entry = {
			"id": "one",
			"text": "新原文",
			"display_markdown": "旧原文",
			"display_text_sha256": text_sha256("旧原文"),
		}
		formatter = FakeFormatter("**新原文**")
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "entries.json"
			path.write_text("[]\n", encoding="utf-8")

			counts = format_display_entries(
				[entry],
				formatter,
				path,
				now_provider=lambda: "2026-08-24T11:00:00+08:00",
			)
			saved = json.loads(path.read_text(encoding="utf-8"))

		self.assertEqual(counts, (1, 0, 0))
		self.assertEqual(saved[0]["display_markdown"], "**新原文**")
		self.assertEqual(saved[0]["display_text_sha256"], text_sha256("新原文"))

	def test_invalid_formatting_caches_plain_text_fallback(self) -> None:
		entry = {
			"id": "one",
			"text": "原文",
			"display_markdown": "旧内容",
			"display_text_sha256": text_sha256("旧内容"),
			"display_backend": "model-backend-v1:old",
		}
		formatter = FakeFormatter("修改后的文字")
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "entries.json"
			path.write_text("[]\n", encoding="utf-8")

			counts = format_display_entries([entry], formatter, path)
			saved = json.loads(path.read_text(encoding="utf-8"))

		self.assertEqual(counts, (0, 1, 0))
		self.assertEqual(saved[0]["text"], "原文")
		self.assertEqual(saved[0]["display_markdown"], "原文")
		self.assertEqual(saved[0]["display_text_sha256"], text_sha256("原文"))
		self.assertTrue(has_valid_display_cache(saved[0]))
		self.assertNotIn("display_backend", saved[0])


if __name__ == "__main__":
	unittest.main()