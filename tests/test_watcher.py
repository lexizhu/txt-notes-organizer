"""Tests for the note watcher."""

import fcntl
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from watcher import (
    DiffSettings,
    LOGGER,
    configure_logging,
	diff_insert_records,
    log,
    scan_notes,
    scan_notes_with_lock,
    scan_summary,
    split_complete_records,
	text_similarity,
)


class SplitCompleteRecordsTests(unittest.TestCase):
    def test_keeps_one_or_two_blank_lines_inside_record(self) -> None:
        text = "第一段\n\n第二段\n\n\n第三段"

        records, pending = split_complete_records(text, blank_lines=3)

        self.assertEqual(records, [])
        self.assertEqual(pending, text)

    def test_splits_at_three_blank_lines(self) -> None:
        text = "第一条\n\n记录内段落\n\n\n\n第二条"

        records, pending = split_complete_records(text, blank_lines=3)

        self.assertEqual(records, ["第一条\n\n记录内段落"])
        self.assertEqual(pending, "第二条")

    def test_whitespace_only_lines_count_as_blank(self) -> None:
        text = "完成记录\n \n\t\n  \n下一条"

        records, pending = split_complete_records(text, blank_lines=3)

        self.assertEqual(records, ["完成记录"])
        self.assertEqual(pending, "下一条")

    def test_multiple_completed_records_leave_unfinished_tail(self) -> None:
        text = "一\n\n\n\n二\n\n\n\n还没写完"

        records, pending = split_complete_records(text, blank_lines=3)

        self.assertEqual(records, ["一", "二"])
        self.assertEqual(pending, "还没写完")

    def test_rejects_invalid_blank_line_count(self) -> None:
        with self.assertRaises(ValueError):
            split_complete_records("内容", blank_lines=0)


class ScanLockTests(unittest.TestCase):
    def test_manual_scan_waits_for_existing_scan_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes_dir = root / "notes"
            notes_dir.mkdir()
            (notes_dir / "项目甲.txt").write_text("完整记录\n\n\n\n", encoding="utf-8")
            state_path = root / "watcher_state.json"
            state_path.write_text("{}\n", encoding="utf-8")
            captured_path = root / "captured_entries.jsonl"
            captured_path.touch()
            lock_path = root / "watcher-scan.lock"
            result: list[int] = []

            with lock_path.open("a+", encoding="utf-8") as held_lock:
                fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX)
                thread = threading.Thread(
                    target=lambda: result.append(
                        scan_notes_with_lock(
                            notes_dir,
                            state_path,
                            captured_path,
                            3,
                            lock_path=lock_path,
                        )
                    )
                )
                thread.start()
                time.sleep(0.05)
                self.assertTrue(thread.is_alive())
                self.assertEqual(captured_path.read_text(encoding="utf-8"), "")
                fcntl.flock(held_lock.fileno(), fcntl.LOCK_UN)

            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(result, [1])
            self.assertEqual(len(captured_path.read_text(encoding="utf-8").splitlines()), 1)


class ScanNotesTests(unittest.TestCase):
    def test_captures_only_complete_appended_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes_dir = root / "notes"
            notes_dir.mkdir()
            state_path = root / "watcher_state.json"
            state_path.write_text("{}\n", encoding="utf-8")
            captured_path = root / "captured_entries.jsonl"
            captured_path.touch()
            note_path = notes_dir / "项目甲.txt"
            note_path.write_text("第一条\n\n记录内排版\n\n\n\n未完成", encoding="utf-8")

            first_count = scan_notes(notes_dir, state_path, captured_path, 3)
            with note_path.open("a", encoding="utf-8") as file:
                file.write("记录\n\n\n\n")
            second_count = scan_notes(notes_dir, state_path, captured_path, 3)

            entries = [
                json.loads(line)
                for line in captured_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(first_count, 1)
            self.assertEqual(second_count, 1)
            self.assertEqual(
                [entry["text"] for entry in entries],
                ["第一条\n\n记录内排版", "未完成记录"],
            )
            self.assertEqual(entries[0]["project"], "项目甲")
            self.assertIn("record_time", entries[0])
            self.assertIn("id", entries[0])
            self.assertEqual(entries[0]["event_type"], "insert")
            self.assertEqual(entries[0]["record_id"], entries[0]["id"])

    def _assert_insertion_position(self, position: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes_dir = root / "notes"
            notes_dir.mkdir()
            state_path = root / "watcher_state.json"
            state_path.write_text("{}\n", encoding="utf-8")
            captured_path = root / "captured_entries.jsonl"
            captured_path.touch()
            note_path = notes_dir / "项目甲.txt"
            separator = "\n\n\n\n"
            base_records = ["第一条", "第二条"]
            note_path.write_text(separator.join(base_records) + separator, encoding="utf-8")

            self.assertEqual(scan_notes(notes_dir, state_path, captured_path, 3), 2)
            before_lines = captured_path.read_text(encoding="utf-8").splitlines()
            if position == "beginning":
                updated_records = ["新增条目", *base_records]
            elif position == "middle":
                updated_records = [base_records[0], "新增条目", base_records[1]]
            else:
                updated_records = [*base_records, "新增条目"]
            note_path.write_text(separator.join(updated_records) + separator, encoding="utf-8")

            self.assertEqual(scan_notes(notes_dir, state_path, captured_path, 3), 1)
            self.assertEqual(scan_notes(notes_dir, state_path, captured_path, 3), 0)
            after_lines = captured_path.read_text(encoding="utf-8").splitlines()
            new_events = [json.loads(line) for line in after_lines[len(before_lines):]]
            self.assertEqual(len(new_events), 1)
            self.assertEqual(new_events[0]["text"], "新增条目")
            self.assertEqual(new_events[0]["event_type"], "insert")

    def test_captures_insert_at_beginning_once(self) -> None:
        self._assert_insertion_position("beginning")

    def test_captures_insert_in_middle_once(self) -> None:
        self._assert_insertion_position("middle")

    def test_captures_insert_at_end_once(self) -> None:
        self._assert_insertion_position("end")

    def test_legacy_state_migrates_without_duplicate_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes_dir = root / "notes"
            notes_dir.mkdir()
            note_path = notes_dir / "项目甲.txt"
            note_path.write_text("已有记录\n\n\n\n", encoding="utf-8")
            state_path = root / "watcher_state.json"
            state_path.write_text(
                json.dumps({"files": {"项目甲.txt": {"byte_offset": 1, "prefix_sha256": "old", "pending_text": ""}}}),
                encoding="utf-8",
            )
            captured_path = root / "captured_entries.jsonl"
            captured_path.touch()

            self.assertEqual(scan_notes(notes_dir, state_path, captured_path, 3), 0)
            self.assertEqual(captured_path.read_text(encoding="utf-8"), "")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["files"]["项目甲.txt"]["records"][0]["text"], "已有记录")

    def test_light_edit_emits_update_once_with_stable_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            notes_dir = root / "notes"
            notes_dir.mkdir()
            state_path = root / "watcher_state.json"
            state_path.write_text("{}\n", encoding="utf-8")
            captured_path = root / "captured_entries.jsonl"
            captured_path.touch()
            note_path = notes_dir / "项目甲.txt"
            original = "会议记录\n" + "这是稳定的会议正文。" * 20
            updated = original.replace("稳定", "更新", 1)
            note_path.write_text(original + "\n\n\n\n", encoding="utf-8")
            self.assertEqual(scan_notes(notes_dir, state_path, captured_path, 3), 1)
            first_event = json.loads(captured_path.read_text(encoding="utf-8").splitlines()[0])

            note_path.write_text(updated + "\n\n\n\n", encoding="utf-8")
            self.assertEqual(scan_notes(notes_dir, state_path, captured_path, 3), 1)
            self.assertEqual(scan_notes(notes_dir, state_path, captured_path, 3), 0)
            events = [json.loads(line) for line in captured_path.read_text(encoding="utf-8").splitlines()]
            update_event = events[-1]
            self.assertEqual(update_event["event_type"], "update")
            self.assertEqual(update_event["record_id"], first_event["record_id"])
            self.assertEqual(update_event["text"], updated)
            self.assertGreaterEqual(update_event["similarity"], 0.85)

    def test_short_record_requires_high_similarity(self) -> None:
        old = [{"record_id": "one", "text": "明天提交报告"}]
        new = ["明天取消报告"]

        _, events = diff_insert_records(old, new, Path("短记录.txt"), DiffSettings())

        self.assertEqual(events[0]["event_type"], "insert")

    def test_large_rewrite_becomes_insert_not_update(self) -> None:
        old = [{"record_id": "one", "text": "A" * 200}]
        new = ["B" * 200]

        _, events = diff_insert_records(old, new, Path("重写.txt"), DiffSettings())

        self.assertEqual(events[0]["event_type"], "insert")
        self.assertNotEqual(events[0]["record_id"], "one")

    def test_ambiguous_similar_candidates_do_not_auto_update(self) -> None:
        base = "会议正文" * 30
        old = [
            {"record_id": "one", "text": base + "甲"},
            {"record_id": "two", "text": base + "乙"},
        ]
        new = [base + "丙"]

        _, events = diff_insert_records(old, new, Path("歧义.txt"), DiffSettings())

        self.assertEqual(events[0]["event_type"], "insert")

    def test_insert_and_update_in_same_scan(self) -> None:
        old_text = "原记录" + "正文" * 50
        updated_text = old_text + "补充"
        old = [{"record_id": "one", "text": old_text}]

        _, events = diff_insert_records(
            old,
            [updated_text, "全新记录"],
            Path("混合.txt"),
            DiffSettings(short_record_similarity_threshold=0.90),
        )

        self.assertEqual([event["event_type"] for event in events], ["update", "insert"])

    def test_real_previous_edit_similarity_exceeds_threshold(self) -> None:
        old = "虚构会议：Sample Alpha 项目首次说明\n日期：2026年8月12日\n" + "示例正文" * 100
        new = "虚构会议：Sample Beta 项目首次说明\n日期：2026年8月12日\n" + "示例正文" * 100

        self.assertGreater(text_similarity(old, new), 0.85)


class LoggingTests(unittest.TestCase):
    def tearDown(self) -> None:
        for handler in LOGGER.handlers[:]:
            handler.close()
            LOGGER.removeHandler(handler)

    def test_rotates_file_and_keeps_recent_scan_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "watcher.log"
            configure_logging(log_path, max_bytes=180, backup_count=2)

            for index in range(12):
                log(f"重要事件 {index}：" + "x" * 35)
            log(scan_summary(0))

            for handler in LOGGER.handlers:
                handler.flush()
            combined_logs = "".join(
                path.read_text(encoding="utf-8")
                for path in sorted(Path(directory).glob("watcher.log*"))
            )
            self.assertIn("[扫描] 已检查 notes/，无新增", combined_logs)
            self.assertIn("重要事件 11", combined_logs)
            self.assertTrue((Path(directory) / "watcher.log.1").exists())
            self.assertLessEqual(len(list(Path(directory).glob("watcher.log*"))), 3)

    def test_scan_summary_reports_new_count(self) -> None:
        self.assertEqual(scan_summary(0), "[扫描] 已检查 notes/，无新增")
        self.assertEqual(scan_summary(3), "[扫描] 已检查 notes/，发现 3 条新增")


if __name__ == "__main__":
    unittest.main()