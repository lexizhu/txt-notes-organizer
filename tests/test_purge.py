"""Tests for read-only permanent deletion planning."""

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from organize import text_sha256
from purge import (
	PurgeArtifacts,
	PurgePaths,
	build_purge_plan,
	discover_purge_logs,
	purge_generated_data,
	purge_structured_data,
	recover_incomplete_purges,
)


def captured(event_id, event_type, record_id, text, source_file="项目甲.txt", previous=None):
	item = {
		"event_id": event_id,
		"event_type": event_type,
		"record_id": record_id,
		"id": record_id,
		"text": text,
		"record_time": "2026-08-26T10:00:00+08:00",
		"project": "项目甲",
		"source_file": source_file,
	}
	if previous is not None:
		item["previous_text_sha256"] = text_sha256(previous)
	return item


def entry(record_id, text, hidden=True):
	return {
		"id": record_id,
		"text": text,
		"source_file": "项目甲.txt",
		"hidden": hidden,
	}


def action(event_id, record_id, event_type="hide"):
	return {
		"event_id": event_id,
		"event_type": event_type,
		"record_id": record_id,
		"occurred_at": "2026-08-26T11:00:00+08:00",
		"actor": "user",
	}


class PurgePlanTests(unittest.TestCase):
	def test_duplicate_text_uses_record_ids_without_harming_neighbor(self) -> None:
		events = [
			captured("insert-one", "insert", "one", "相同正文"),
			captured("insert-two", "insert", "two", "相同正文"),
		]
		entries = [entry("one", "相同正文"), entry("two", "相同正文", hidden=False)]
		actions = [action("hide-one", "one"), action("restore-two", "two", "restore")]
		state = {"files": {"项目甲.txt": {"records": [
			{"record_id": "one", "text": "相同正文"},
			{"record_id": "two", "text": "相同正文"},
		]}}}
		originals = copy.deepcopy((events, entries, actions, state))

		plan = build_purge_plan("one", events, entries, actions, state)

		self.assertEqual(plan.captured_event_ids, ("insert-one",))
		self.assertEqual(plan.captured_record_ids, ("one",))
		self.assertEqual(plan.entry_index, 0)
		self.assertEqual(plan.user_action_event_ids, ("hide-one",))
		self.assertEqual(plan.state_record_id, "one")
		self.assertEqual(plan.state_match, "record_id")
		self.assertEqual((events, entries, actions, state), originals)

	def test_migrated_alias_includes_the_whole_event_chain(self) -> None:
		events = [
			captured("legacy-insert", "insert", "legacy-id", "旧正文"),
			captured("migrated-update", "update", "migrated-id", "新正文", previous="旧正文"),
			captured("second-update", "update", "migrated-id", "最终正文", previous="新正文"),
		]
		entries = [entry("legacy-id", "最终正文")]
		actions = [action("hide-legacy", "legacy-id")]
		state = {"files": {"项目甲.txt": {"records": [
			{"record_id": "migrated-id", "text": "最终正文"},
		]}}}

		plan = build_purge_plan("legacy-id", events, entries, actions, state)

		self.assertEqual(
			plan.captured_event_ids,
			("legacy-insert", "migrated-update", "second-update"),
		)
		self.assertEqual(plan.captured_record_ids, ("legacy-id", "migrated-id"))
		self.assertEqual(plan.state_record_id, "migrated-id")
		self.assertEqual(plan.state_match, "record_id")

	def test_unique_legacy_snapshot_can_use_exact_migration_hash(self) -> None:
		events = [captured("legacy-insert", "insert", "legacy-id", "旧版正文")]
		state = {"files": {"项目甲.txt": {"records": [
			{"record_id": "state-random-id", "text": "旧版正文"},
		]}}}

		plan = build_purge_plan(
			"legacy-id",
			events,
			[entry("legacy-id", "旧版正文")],
			[action("hide-legacy", "legacy-id")],
			state,
		)

		self.assertEqual(plan.state_record_id, "state-random-id")
		self.assertEqual(plan.state_match, "migration_exact_hash")

	def test_ambiguous_legacy_snapshot_is_rejected(self) -> None:
		events = [
			captured("insert-one", "insert", "one", "相同正文"),
			captured("insert-two", "insert", "two", "相同正文"),
		]
		state = {"files": {"项目甲.txt": {"records": [
			{"record_id": "random-id", "text": "相同正文"},
		]}}}

		with self.assertRaisesRegex(ValueError, "无法.*唯一定位"):
			build_purge_plan(
				"one",
				events,
				[entry("one", "相同正文"), entry("two", "相同正文")],
				[action("hide-one", "one")],
				state,
			)

	def test_record_already_absent_from_state_needs_no_state_deletion(self) -> None:
		events = [captured("insert-one", "insert", "one", "已从原文件移除的正文")]
		state = {"files": {"项目甲.txt": {"records": [
			{"record_id": "other", "text": "其他正文"},
		]}}}

		plan = build_purge_plan(
			"one",
			events,
			[entry("one", "已从原文件移除的正文")],
			[action("hide-one", "one")],
			state,
		)

		self.assertIsNone(plan.state_record_index)
		self.assertIsNone(plan.state_record_id)
		self.assertEqual(plan.state_match, "not_present")

	def test_visible_record_is_rejected(self) -> None:
		events = [captured("insert-one", "insert", "one", "正文")]
		state = {"files": {"项目甲.txt": {"records": [
			{"record_id": "one", "text": "正文"},
		]}}}

		with self.assertRaisesRegex(ValueError, "只有已隐藏"):
			build_purge_plan("one", events, [entry("one", "正文", hidden=False)], [], state)

	def test_stale_hidden_projection_with_latest_restore_is_rejected(self) -> None:
		events = [captured("insert-one", "insert", "one", "正文")]
		state = {"files": {"项目甲.txt": {"records": [
			{"record_id": "one", "text": "正文"},
		]}}}
		actions = [action("hide-one", "one"), action("restore-one", "one", "restore")]

		with self.assertRaisesRegex(ValueError, "当前有效的隐藏操作"):
			build_purge_plan("one", events, [entry("one", "正文")], actions, state)


class PurgeTransactionTests(unittest.TestCase):
	def setUp(self) -> None:
		self.temporary_directory = tempfile.TemporaryDirectory()
		self.root = Path(self.temporary_directory.name)
		self.data_dir = self.root / "data"
		self.notes_dir = self.root / "notes"
		self.data_dir.mkdir()
		self.notes_dir.mkdir()
		self.output_dir = self.root / "output"
		self.output_dir.mkdir()
		self.note_path = self.notes_dir / "项目甲.txt"
		self.note_path.write_text("敏感正文\n\n\n\n邻居正文\n\n\n\n", encoding="utf-8")
		self.paths = PurgePaths(
			self.data_dir / "captured_entries.jsonl",
			self.data_dir / "entries.json",
			self.data_dir / "user_actions.jsonl",
			self.data_dir / "watcher_state.json",
		)
		self.review_path = self.output_dir / "review.html"
		self.current_log = self.data_dir / "local-server.log"
		self.rotated_log = self.data_dir / "local-server.log.1"
		self.watcher_log = self.data_dir / "watcher.log"
		self.artifacts = PurgeArtifacts(
			self.root,
			self.review_path,
			(self.current_log, self.rotated_log, self.watcher_log),
		)
		self.events = [
			captured("insert-target", "insert", "target", "敏感正文"),
			captured("insert-neighbor", "insert", "neighbor", "邻居正文"),
		]
		self.entries = [entry("target", "敏感正文"), entry("neighbor", "邻居正文", hidden=False)]
		self.actions = [action("hide-target", "target")]
		self.state = {"files": {"项目甲.txt": {"records": [
			{"record_id": "target", "text": "敏感正文"},
			{"record_id": "neighbor", "text": "邻居正文"},
		]}}}
		self._write_files()

	def tearDown(self) -> None:
		self.temporary_directory.cleanup()

	def _write_files(self) -> None:
		self.paths.captured_entries.write_text(
			"".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self.events),
			encoding="utf-8",
		)
		self.paths.entries.write_text(json.dumps(self.entries, ensure_ascii=False), encoding="utf-8")
		self.paths.user_actions.write_text(
			"".join(json.dumps(item, ensure_ascii=False) + "\n" for item in self.actions),
			encoding="utf-8",
		)
		self.paths.watcher_state.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
		self.review_path.write_text("旧页面包含敏感正文和邻居正文", encoding="utf-8")
		self.current_log.write_text("[INFO] 正在处理：敏感正文\n[INFO] 邻居正文\n", encoding="utf-8")
		self.rotated_log.write_text("[INFO] 旧摘要：敏感正文\n", encoding="utf-8")
		self.watcher_log.write_text("[INFO] target\n[INFO] neighbor\n", encoding="utf-8")

	def _hashes(self) -> dict[str, str]:
		return {
			name: hashlib.sha256(path.read_bytes()).hexdigest()
			for name, path in {
				**self.paths.files(),
				"source.txt": self.note_path,
				"review.html": self.review_path,
				"local-server.log": self.current_log,
				"local-server.log.1": self.rotated_log,
				"watcher.log": self.watcher_log,
			}.items()
		}

	def _real_html_renderer(self, remaining_entries):
		from build_html import build_html

		project_root = Path(__file__).parents[1]
		return build_html(
			remaining_entries,
			[{"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}}],
			"zh-CN",
			4,
			(project_root / "vendor" / "markdown-it.min.js").read_text(encoding="utf-8"),
			(project_root / "output" / "review-mock.html").read_text(encoding="utf-8"),
		)

	def test_success_purges_only_target_and_destroys_backup(self) -> None:
		note_hash = self._hashes()["source.txt"]

		plan = purge_structured_data("target", self.paths)

		self.assertEqual(plan.record_id, "target")
		self.assertNotIn("敏感正文", self.paths.captured_entries.read_text(encoding="utf-8"))
		self.assertNotIn("敏感正文", self.paths.entries.read_text(encoding="utf-8"))
		self.assertNotIn("target", self.paths.user_actions.read_text(encoding="utf-8"))
		self.assertNotIn("敏感正文", self.paths.watcher_state.read_text(encoding="utf-8"))
		self.assertIn("邻居正文", self.paths.captured_entries.read_text(encoding="utf-8"))
		self.assertIn("邻居正文", self.paths.entries.read_text(encoding="utf-8"))
		self.assertEqual(self._hashes()["source.txt"], note_hash)
		self.assertEqual(list(self.data_dir.glob(".purge-transaction-*")), [])

	def test_commit_failure_at_every_replacement_restores_all_files(self) -> None:
		for replacement_number in range(1, 5):
			with self.subTest(replacement_number=replacement_number):
				self._write_files()
				before = self._hashes()

				with self.assertRaisesRegex(OSError, "提交中断"):
					purge_structured_data(
						"target",
						self.paths,
						fail_after_replacements=replacement_number,
					)

				self.assertEqual(self._hashes(), before)
				self.assertEqual(list(self.data_dir.glob(".purge-transaction-*")), [])

	def test_legacy_neighbor_event_keeps_its_original_shape(self) -> None:
		legacy_neighbor = {
			"id": "neighbor",
			"text": "邻居正文",
			"record_time": "2026-08-26T10:00:00+08:00",
			"project": "项目甲",
			"source_file": "项目甲.txt",
		}
		self.events[1] = legacy_neighbor
		self._write_files()

		purge_structured_data("target", self.paths)

		remaining = json.loads(self.paths.captured_entries.read_text(encoding="utf-8"))
		self.assertEqual(remaining, legacy_neighbor)
		self.assertNotIn("event_type", remaining)
		self.assertNotIn("record_id", remaining)

	def test_migrated_alias_transaction_removes_entire_chain_only(self) -> None:
		self.events = [
			captured("legacy-insert", "insert", "target", "旧敏感正文"),
			captured("alias-update", "update", "alias-id", "新敏感正文", previous="旧敏感正文"),
			captured("final-update", "update", "alias-id", "最终敏感正文", previous="新敏感正文"),
			captured("insert-neighbor", "insert", "neighbor", "邻居正文"),
		]
		self.entries = [entry("target", "最终敏感正文"), entry("neighbor", "邻居正文", hidden=False)]
		self.state = {"files": {"项目甲.txt": {"records": [
			{"record_id": "alias-id", "text": "最终敏感正文"},
			{"record_id": "neighbor", "text": "邻居正文"},
		]}}}
		self._write_files()
		self.current_log.write_text("[INFO] 旧敏感正文\n[INFO] 邻居正文\n", encoding="utf-8")
		self.rotated_log.write_text("[INFO] 新敏感正文\n[INFO] 最终敏感正文\n", encoding="utf-8")

		plan = purge_generated_data("target", self.paths, self.artifacts, self._real_html_renderer)

		self.assertEqual(
			plan.captured_event_ids,
			("legacy-insert", "alias-update", "final-update"),
		)
		remaining_events = [
			json.loads(line)
			for line in self.paths.captured_entries.read_text(encoding="utf-8").splitlines()
			if line.strip()
		]
		self.assertEqual([item["event_id"] for item in remaining_events], ["insert-neighbor"])
		self.assertIn("邻居正文", self.paths.watcher_state.read_text(encoding="utf-8"))
		self.assertNotIn("最终敏感正文", self.paths.watcher_state.read_text(encoding="utf-8"))
		self.assertNotIn("旧敏感正文", self.current_log.read_text(encoding="utf-8"))
		self.assertNotIn("新敏感正文", self.rotated_log.read_text(encoding="utf-8"))
		self.assertNotIn("最终敏感正文", self.rotated_log.read_text(encoding="utf-8"))

	def test_startup_recovery_restores_leftover_transaction(self) -> None:
		before = self._hashes()
		transaction_dir = self.data_dir / ".purge-transaction-leftover"
		backup_dir = transaction_dir / "backup"
		backup_dir.mkdir(parents=True)
		for name, path in self.paths.files().items():
			(backup_dir / name).write_bytes(path.read_bytes())
		self.paths.entries.write_text("[]\n", encoding="utf-8")

		recovered = recover_incomplete_purges(self.data_dir)

		self.assertEqual(recovered, 1)
		self.assertEqual(self._hashes(), before)
		self.assertFalse(transaction_dir.exists())

	def test_startup_recovery_finalizes_committed_transaction_without_restoring(self) -> None:
		transaction_dir = self.data_dir / ".purge-transaction-committed"
		backup_dir = transaction_dir / "backup"
		backup_dir.mkdir(parents=True)
		for name, path in self.paths.files().items():
			(backup_dir / name).write_bytes(path.read_bytes())
		(transaction_dir / "manifest.json").write_text(
			json.dumps({"status": "committed", "files": list(self.paths.files())}),
			encoding="utf-8",
		)
		self.paths.entries.write_text("[]\n", encoding="utf-8")

		recovered = recover_incomplete_purges(self.data_dir)

		self.assertEqual(recovered, 1)
		self.assertEqual(json.loads(self.paths.entries.read_text(encoding="utf-8")), [])
		self.assertFalse(transaction_dir.exists())

	def test_full_purge_rebuilds_html_scrubs_logs_and_preserves_open_log_handle(self) -> None:
		note_hash = self._hashes()["source.txt"]
		with self.current_log.open("ab", buffering=0) as active_log:
			inode_before = os.fstat(active_log.fileno()).st_ino

			purge_generated_data("target", self.paths, self.artifacts, self._real_html_renderer)

			inode_after = self.current_log.stat().st_ino
			self.assertEqual(inode_after, inode_before)
			active_log.write("[INFO] 后续日志仍可写入\n".encode())
		self.assertNotIn("敏感正文", self.review_path.read_text(encoding="utf-8"))
		self.assertNotIn('"id": "target"', self.review_path.read_text(encoding="utf-8"))
		self.assertIn("邻居正文", self.review_path.read_text(encoding="utf-8"))
		for log_path in self.artifacts.log_paths:
			content = log_path.read_text(encoding="utf-8")
			self.assertNotIn("敏感正文", content)
			self.assertNotIn("target", content)
		self.assertIn("邻居正文", self.current_log.read_text(encoding="utf-8"))
		self.assertIn("后续日志仍可写入", self.current_log.read_text(encoding="utf-8"))
		self.assertEqual(self._hashes()["source.txt"], note_hash)
		self.assertEqual(list(self.data_dir.glob(".purge-transaction-*")), [])

	def test_full_purge_failure_restores_data_html_logs_and_txt(self) -> None:
		before = self._hashes()

		with self.assertRaisesRegex(OSError, "完整提交中断"):
			purge_generated_data(
				"target",
				self.paths,
				self.artifacts,
				self._real_html_renderer,
				fail_after_replacements=6,
			)

		self.assertEqual(self._hashes(), before)
		self.assertEqual(list(self.data_dir.glob(".purge-transaction-*")), [])

	def test_discovers_current_and_rotated_logs_only(self) -> None:
		(self.data_dir / "local-server.log.1").touch()
		(self.data_dir / "watcher.log.2").touch()
		(self.data_dir / "watcher-error.log.1").touch()
		(self.data_dir / "unrelated.log").touch()

		discovered = {path.name for path in discover_purge_logs(self.data_dir)}

		self.assertIn("local-server.log", discovered)
		self.assertIn("local-server.log.1", discovered)
		self.assertIn("watcher.log", discovered)
		self.assertIn("watcher.log.2", discovered)
		self.assertIn("watcher-error.log.1", discovered)
		self.assertNotIn("unrelated.log", discovered)


if __name__ == "__main__":
	unittest.main()