"""Tests for organizing captured notes without a real LLM."""

import json
import tempfile
import unittest
from pathlib import Path

from llm_adapter import AnalysisResult, LLMError
from organize import (
    apply_user_actions,
    ensure_daily_processing_succeeded,
    ensure_processing_succeeded,
    fold_capture_events,
    fold_user_actions,
    load_captured_entries,
    load_user_actions,
    organize_entries,
    OrganizeCancelled,
    reanalyze_existing_entries,
    reprocess_selected_entries,
    text_sha256,
)


CATEGORIES = [
    {"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}},
    {"code": "other", "labels": {"zh-CN": "其他", "en": "Other"}},
]


class FakeAnalyzer:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[str] = []

    def analyze(self, text, record_time, categories):
        self.calls.append(text)
        if self.should_fail:
            raise LLMError("测试错误")
        return AnalysisResult(
            category="work",
            status="todo",
            event_time="2026-08-21T09:00:00+08:00",
        )

    def format_markdown(self, text):
        self.calls.append(f"display:{text}")
        return text

    def identify_tables(self, text):
        self.calls.append(f"tables:{text}")
        return []


def captured_entry(entry_id: str, text: str = "明天提交报告") -> dict:
    return {
        "id": entry_id,
        "text": text,
        "record_time": "2026-08-20T10:00:00+08:00",
        "project": "项目甲",
        "source_file": "项目甲.txt",
    }


def event(event_type: str, record_id: str, text: str, previous_text: str | None = None) -> dict:
    result = {
        "event_id": f"event-{event_type}-{len(text)}",
        "event_type": event_type,
        "record_id": record_id,
        "id": record_id,
        "text": text,
        "record_time": "2026-08-25T10:00:00+08:00",
        "project": "项目甲",
        "source_file": "项目甲.txt",
    }
    if previous_text is not None:
        result["previous_text_sha256"] = text_sha256(previous_text)
    return result


def user_action(event_id: str, event_type: str, record_id: str, occurred_at: str) -> dict:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "record_id": record_id,
        "occurred_at": occurred_at,
        "actor": "user",
    }


class OrganizeEntriesTests(unittest.TestCase):
    def test_processing_failure_prevents_rebuilding_last_valid_page(self) -> None:
        ensure_processing_succeeded(0, 0, 0)

        with self.assertRaisesRegex(RuntimeError, "保留上一次有效页面"):
            ensure_processing_succeeded(0, 1, 0)

    def test_daily_processing_allows_display_fallbacks_but_not_missing_analysis(self) -> None:
        ensure_daily_processing_succeeded(0, 1, 1)

        with self.assertRaisesRegex(RuntimeError, "保留上一次有效页面"):
            ensure_daily_processing_succeeded(1, 0, 0)

    def test_missing_user_actions_file_loads_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            loaded = load_user_actions(Path(directory) / "user_actions.jsonl")

        self.assertEqual(loaded, [])

    def test_load_and_fold_user_actions_uses_last_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user_actions.jsonl"
            actions = [
                user_action("hide-one", "hide", "one", "2026-08-25T10:00:00+08:00"),
                user_action("restore-one", "restore", "one", "2026-08-25T11:00:00+08:00"),
                user_action("hide-two", "hide", "two", "2026-08-25T12:00:00+08:00"),
            ]
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in actions) + "\n",
                encoding="utf-8",
            )

            visibility = fold_user_actions(load_user_actions(path))

        self.assertFalse(visibility["one"]["hidden"])
        self.assertTrue(visibility["two"]["hidden"])

    def test_duplicate_user_action_event_id_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "user_actions.jsonl"
            actions = [
                user_action("same", "hide", "one", "2026-08-25T10:00:00+08:00"),
                user_action("same", "restore", "one", "2026-08-25T11:00:00+08:00"),
            ]
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in actions) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "event_id 无效或重复"):
                load_user_actions(path)

    def test_hide_survives_text_update_until_explicit_restore(self) -> None:
        entry = {
            "id": "one",
            "text": "旧正文",
            "category": "other",
            "status": "none",
            "user_edited": False,
        }
        hidden_state = fold_user_actions(
            [user_action("hide-one", "hide", "one", "2026-08-25T10:00:00+08:00")]
        )

        self.assertTrue(apply_user_actions([entry], hidden_state))
        entry["text"] = "大幅改写后的正文"
        self.assertFalse(apply_user_actions([entry], hidden_state))
        self.assertTrue(entry["hidden"])
        self.assertEqual(entry["hidden_at"], "2026-08-25T10:00:00+08:00")

        restored_state = fold_user_actions(
            [
                user_action("hide-one", "hide", "one", "2026-08-25T10:00:00+08:00"),
                user_action("restore-one", "restore", "one", "2026-08-25T11:00:00+08:00"),
            ]
        )
        self.assertTrue(apply_user_actions([entry], restored_state))
        self.assertFalse(entry["hidden"])
        self.assertNotIn("hidden_at", entry)
        self.assertEqual(entry["text"], "大幅改写后的正文")

    def test_reanalysis_does_not_reset_hidden_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries_path = Path(directory) / "entries.json"
            existing = {
                "id": "one",
                "text": "旧正文",
                "category": "other",
                "status": "none",
                "event_time": None,
                "user_edited": False,
                "hidden": True,
                "hidden_at": "2026-08-25T10:00:00+08:00",
            }

            counts = organize_entries(
                [captured_entry("one", "新正文")],
                [existing],
                CATEGORIES,
                FakeAnalyzer(),
                entries_path,
            )

        self.assertEqual(counts, (1, 0, 0))
        self.assertEqual(existing["text"], "新正文")
        self.assertTrue(existing["hidden"])
        self.assertEqual(existing["hidden_at"], "2026-08-25T10:00:00+08:00")

    def test_user_action_for_unknown_record_is_rejected(self) -> None:
        visibility = fold_user_actions(
            [user_action("hide-missing", "hide", "missing", "2026-08-25T10:00:00+08:00")]
        )

        with self.assertRaisesRegex(ValueError, "未知记录：missing"):
            apply_user_actions([], visibility)

    def test_loader_allows_multiple_events_for_one_logical_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            items = [
                event("insert", "one", "旧正文"),
                event("update", "one", "更新后的正文", previous_text="旧正文"),
            ]
            path.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
                encoding="utf-8",
            )

            loaded = load_captured_entries(path)

        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["record_id"], loaded[1]["record_id"])
    def test_fold_insert_update_keeps_one_logical_record(self) -> None:
        events = [
            event("insert", "one", "旧正文"),
            event("update", "one", "新正文", previous_text="旧正文"),
        ]

        snapshots = fold_capture_events(events)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["id"], "one")
        self.assertEqual(snapshots[0]["text"], "新正文")

    def test_fold_migrated_record_id_aliases_by_previous_hash(self) -> None:
        events = [
            event("insert", "legacy-id", "旧正文"),
            event("update", "migrated-random-id", "新正文", previous_text="旧正文"),
            event("update", "migrated-random-id", "更新正文", previous_text="新正文"),
        ]

        snapshots = fold_capture_events(events)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["id"], "legacy-id")
        self.assertEqual(snapshots[0]["text"], "更新正文")

    def test_fold_rejects_ambiguous_previous_hash(self) -> None:
        events = [
            event("insert", "one", "相同正文"),
            event("insert", "two", "相同正文"),
            event("update", "random", "新正文", previous_text="相同正文"),
        ]

        with self.assertRaisesRegex(ValueError, "无法唯一匹配"):
            fold_capture_events(events)

    def test_update_reanalyzes_and_clears_display_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries_path = Path(directory) / "entries.json"
            existing = {
                "id": "one",
                "text": "旧正文",
                "category": "other",
                "status": "none",
                "event_time": None,
                "user_edited": False,
                "display_markdown": "旧正文",
                "display_text_sha256": text_sha256("旧正文"),
            }
            analyzer = FakeAnalyzer()

            counts = organize_entries(
                [captured_entry("one", "新正文")],
                [existing],
                CATEGORIES,
                analyzer,
                entries_path,
                now_provider=lambda: "2026-08-25T11:00:00+08:00",
            )

            self.assertEqual(counts, (1, 0, 0))
            self.assertEqual(existing["text"], "新正文")
            self.assertEqual(existing["category"], "work")
            self.assertEqual(existing["status"], "todo")
            self.assertNotIn("display_markdown", existing)

    def test_user_edited_update_protects_ai_fields_but_updates_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries_path = Path(directory) / "entries.json"
            existing = {
                "id": "one",
                "text": "旧正文",
                "category": "other",
                "status": "none",
                "event_time": "2026-08-01T00:00:00+08:00",
                "user_edited": True,
                "display_tables": [],
            }

            analyzer = FakeAnalyzer()
            counts = organize_entries(
                [captured_entry("one", "新正文")],
                [existing],
                CATEGORIES,
                analyzer,
                entries_path,
            )

            self.assertEqual(counts, (1, 0, 0))
            self.assertEqual(existing["text"], "新正文")
            self.assertEqual(existing["category"], "other")
            self.assertEqual(existing["status"], "none")
            self.assertEqual(existing["event_time"], "2026-08-01T00:00:00+08:00")
            self.assertNotIn("display_tables", existing)
            self.assertEqual(analyzer.calls, [])
    def test_writes_english_keys_and_internal_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries_path = Path(directory) / "entries.json"
            entries_path.write_text("[]\n", encoding="utf-8")
            analyzer = FakeAnalyzer()

            counts = organize_entries(
                [captured_entry("one")],
                [],
                CATEGORIES,
                analyzer,
                entries_path,
                now_provider=lambda: "2026-08-20T11:00:00+08:00",
            )

            saved = json.loads(entries_path.read_text(encoding="utf-8"))
            self.assertEqual(counts, (1, 0, 0))
            self.assertEqual(saved[0]["category"], "work")
            self.assertEqual(saved[0]["status"], "todo")
            self.assertFalse(saved[0]["user_edited"])
            self.assertNotIn("labels", saved[0])

    def test_skips_existing_user_edited_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries_path = Path(directory) / "entries.json"
            existing = {
                "id": "one",
                "text": "明天提交报告",
                "category": "other",
                "user_edited": True,
            }
            analyzer = FakeAnalyzer()

            counts = organize_entries(
                [captured_entry("one")],
                [existing],
                CATEGORIES,
                analyzer,
                entries_path,
            )

            self.assertEqual(counts, (0, 0, 1))
            self.assertEqual(analyzer.calls, [])
            self.assertEqual(existing["category"], "other")

    def test_reanalysis_targets_exact_ids_and_preserves_user_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries_path = Path(directory) / "entries.json"
            entries = [
                {**captured_entry("one"), "category": "other", "status": "none", "event_time": None, "user_edited": False},
                {**captured_entry("two"), "category": "other", "status": "done", "event_time": None, "user_edited": True},
                {**captured_entry("three"), "category": "other", "status": "none", "event_time": None, "user_edited": False},
            ]
            analyzer = FakeAnalyzer()

            counts = reanalyze_existing_entries(
                entries,
                {"one", "two"},
                CATEGORIES,
                analyzer,
                entries_path,
                now_provider=lambda: "2026-08-27T18:00:00+08:00",
                backend_id="backend-new",
            )

            self.assertEqual(counts, (1, 0, 2))
            self.assertEqual(analyzer.calls, ["明天提交报告"])
            self.assertEqual(entries[0]["category"], "work")
            self.assertEqual(entries[0]["analysis_backend"], "backend-new")
            self.assertEqual(entries[1]["category"], "other")
            self.assertEqual(entries[2]["category"], "other")
            self.assertEqual(len(json.loads(entries_path.read_text(encoding="utf-8"))), 3)

    def test_combined_reprocess_runs_only_selected_components_and_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries_path = Path(directory) / "entries.json"
            entries = [
                {**captured_entry("one", "目标正文"), "category": "other", "status": "none", "event_time": None, "user_edited": False},
                {**captured_entry("two", "范围外正文"), "category": "other", "status": "none", "event_time": None, "user_edited": False},
            ]
            analyzer = FakeAnalyzer()

            results = reprocess_selected_entries(
                entries,
                {"one"},
                ["analysis", "display"],
                CATEGORIES,
                analyzer,
                entries_path,
                backend_id="backend-new",
            )

            self.assertEqual(results["analysis"][:2], (1, 0))
            self.assertEqual(results["display"][:2], (1, 0))
            self.assertEqual(analyzer.calls, ["目标正文", "display:目标正文"])
            saved = json.loads(entries_path.read_text(encoding="utf-8"))
            self.assertEqual(saved[0]["analysis_backend"], "backend-new")
            self.assertEqual(saved[0]["display_backend"], "backend-new")
            self.assertNotIn("analysis_backend", saved[1])

    def test_failure_does_not_write_partial_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries_path = Path(directory) / "entries.json"
            entries_path.write_text("[]\n", encoding="utf-8")

            counts = organize_entries(
                [captured_entry("one")],
                [],
                CATEGORIES,
                FakeAnalyzer(should_fail=True),
                entries_path,
            )

            self.assertEqual(counts, (0, 1, 0))
            self.assertEqual(json.loads(entries_path.read_text(encoding="utf-8")), [])

    def test_cancel_after_model_response_does_not_save_current_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entries_path = Path(directory) / "entries.json"
            entries_path.write_text("[]\n", encoding="utf-8")
            analyzer = FakeAnalyzer()

            with self.assertRaises(OrganizeCancelled):
                organize_entries(
                    [captured_entry("one")],
                    [],
                    CATEGORIES,
                    analyzer,
                    entries_path,
                    cancel_check=lambda: bool(analyzer.calls),
                )

            self.assertEqual(analyzer.calls, ["明天提交报告"])
            self.assertEqual(json.loads(entries_path.read_text(encoding="utf-8")), [])


if __name__ == "__main__":
    unittest.main()