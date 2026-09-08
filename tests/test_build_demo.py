"""Tests for the deterministic fictional demo page."""

import json
import unittest
from pathlib import Path

from build_demo import build_demo_entries
from build_html import build_html, load_json, prepare_entries_for_display, validate_entries
from llm_adapter import validate_categories


class BuildDemoTests(unittest.TestCase):
	def test_demo_entries_match_txt_records_and_cover_tables(self) -> None:
		entries = build_demo_entries()

		self.assertEqual(len(entries), 9)
		self.assertEqual(len({entry["id"] for entry in entries}), 9)
		self.assertTrue(all(entry["display_backend"] == "fictional-demo" for entry in entries))
		self.assertEqual(sum("display_tables" in entry for entry in entries), 2)
		self.assertEqual({entry["status"] for entry in entries}, {"todo", "done", "none"})

	def test_demo_build_has_structured_tables_and_raw_fallback_without_writing_data(self) -> None:
		root = Path(__file__).parents[1]
		data_paths = [
			root / "data" / "captured_entries.jsonl",
			root / "data" / "user_actions.jsonl",
			root / "data" / "entries.json",
			root / "data" / "watcher_state.json",
		]
		before = {path: path.read_bytes() for path in data_paths}
		settings = load_json(root / "config" / "settings.json")
		categories = validate_categories(load_json(root / "config" / "categories.json"))
		entries = validate_entries(build_demo_entries(), {item["code"] for item in categories})
		prepared = prepare_entries_for_display(entries)

		html = build_html(
			entries,
			categories,
			settings["display"]["language"],
			settings["display"]["collapsed_lines"],
			(root / "vendor" / "markdown-it.min.js").read_text(encoding="utf-8"),
			(root / "output" / "review-mock.html").read_text(encoding="utf-8"),
		)

		self.assertEqual(sum(bool(entry["_display_tables"]) for entry in prepared), 2)
		self.assertEqual(sum(bool(entry["_display_table_fallbacks"]) for entry in prepared), 1)
		self.assertEqual(html.count('"id": "demo-'), 9)
		self.assertIn("Fictional product launch checklist", html)
		self.assertNotIn("virtual-secret", html)
		self.assertEqual({path: path.read_bytes() for path in data_paths}, before)
		self.assertEqual(json.loads((root / "data" / "entries.json").read_text()), [])
		self.assertTrue(
			(root / "output" / "demo-review.html").read_text(encoding="utf-8") == html,
			"Bundled demo page is stale; regenerate it with python3 build_demo.py.",
		)


if __name__ == "__main__":
	unittest.main()
