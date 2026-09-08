"""Tests for coordinate-only table cache generation."""

import json
import tempfile
import unittest
from pathlib import Path

from llm_adapter import LLMError
from organize import has_valid_table_cache, identify_table_entries, text_sha256


class FakeIdentifier:
	def __init__(self, structures=None, should_fail=False) -> None:
		self.structures = structures or []
		self.should_fail = should_fail
		self.calls: list[str] = []

	def identify_tables(self, text: str):
		self.calls.append(text)
		if self.should_fail:
			raise LLMError("测试失败")
		return self.structures


class TableCacheTests(unittest.TestCase):
	def test_force_refreshes_only_target_ids(self) -> None:
		structures = [{
			"candidate_id": 1,
			"line_ids": [1, 2],
			"header_line_id": 1,
			"column_count": 2,
		}]
		entries = [
			{
				"id": "one",
				"text": "A\tB\n1\t2",
				"display_tables": structures,
				"display_tables_text_sha256": text_sha256("A\tB\n1\t2"),
				"display_tables_version": 1,
			},
			{"id": "two", "text": "普通正文", "display_tables": [],
			 "display_tables_text_sha256": text_sha256("普通正文"),
			 "display_tables_version": 1},
		]
		identifier = FakeIdentifier(structures)
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "entries.json"
			path.write_text("[]\n", encoding="utf-8")

			counts = identify_table_entries(
				entries,
				identifier,
				path,
				target_ids={"one"},
				force=True,
			)
			saved = json.loads(path.read_text(encoding="utf-8"))

		self.assertEqual(counts, (1, 0, 1))
		self.assertEqual(identifier.calls, ["A\tB\n1\t2"])
		self.assertEqual(len(saved), 2)
		self.assertEqual(saved[1]["display_tables"], [])

	def test_no_candidates_cache_empty_without_llm_call(self) -> None:
		entry = {"id": "one", "text": "普通正文"}
		identifier = FakeIdentifier()
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "entries.json"
			path.write_text("[]\n", encoding="utf-8")

			counts = identify_table_entries([entry], identifier, path)

		self.assertEqual(counts, (1, 0, 0))
		self.assertEqual(identifier.calls, [])
		self.assertTrue(has_valid_table_cache(entry))

	def test_candidate_coordinates_are_cached_without_cell_text(self) -> None:
		entry = {
			"id": "one",
			"text": "A\tB\n1\t2",
			"table_backend": "model-backend-v1:old",
		}
		structures = [{
			"candidate_id": 1,
			"line_ids": [1, 2],
			"header_line_id": 1,
			"column_count": 2,
		}]
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "entries.json"
			path.write_text("[]\n", encoding="utf-8")

			counts = identify_table_entries([entry], FakeIdentifier(structures), path)
			saved = json.loads(path.read_text(encoding="utf-8"))[0]

		self.assertEqual(counts, (1, 0, 0))
		self.assertEqual(saved["display_tables"], structures)
		self.assertNotIn("cells", saved["display_tables"][0])
		self.assertTrue(has_valid_table_cache(saved))

	def test_incomplete_response_is_not_cached(self) -> None:
		entry = {"id": "one", "text": "A\tB\n1\t2"}
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "entries.json"
			path.write_text("[]\n", encoding="utf-8")

			counts = identify_table_entries([entry], FakeIdentifier([]), path)
			saved = json.loads(path.read_text(encoding="utf-8"))[0]

		self.assertEqual(counts, (0, 1, 0))
		self.assertNotIn("display_tables", saved)
		self.assertNotIn("table_backend", saved)


if __name__ == "__main__":
	unittest.main()