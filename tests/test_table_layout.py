"""Tests for production-safe table display coordinates."""

import unittest

from table_layout import detect_table_candidates, prepare_table_display


class TableLayoutTests(unittest.TestCase):
	def test_valid_coordinates_are_preserved_without_cell_text(self) -> None:
		text = "维度\t现状\t未来\n载体\t静态\t动态"
		structures = [{
			"candidate_id": 1,
			"line_ids": [1, 2],
			"header_line_id": 1,
			"column_count": 3,
		}]

		tables, fallbacks = prepare_table_display(text, structures)

		self.assertEqual(len(tables), 1)
		self.assertEqual(fallbacks, [])
		self.assertNotIn("cells", tables[0])

	def test_invalid_coordinates_become_suspected_fallback(self) -> None:
		text = "#\t事项\t责任方\t时间\n1\t任务\t明天"
		structures = [{
			"candidate_id": 1,
			"line_ids": [1, 2],
			"header_line_id": 1,
			"column_count": 4,
		}]

		tables, fallbacks = prepare_table_display(text, structures)

		self.assertEqual(tables, [])
		self.assertEqual(fallbacks[0]["line_ids"], [1, 2])
		self.assertIn("列不一致", fallbacks[0]["reason"])

	def test_candidate_without_model_coordinates_falls_back(self) -> None:
		text = "A  B\n1  2"

		tables, fallbacks = prepare_table_display(text, [])

		self.assertEqual(tables, [])
		self.assertEqual(len(fallbacks), 1)
		self.assertEqual(len(detect_table_candidates(text)), 1)


if __name__ == "__main__":
	unittest.main()