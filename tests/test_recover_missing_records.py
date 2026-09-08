"""Tests for one-time missed-record recovery."""

import unittest

from recover_missing_records import find_missing_records


class RecoverMissingRecordsTests(unittest.TestCase):
	def test_high_similarity_edit_is_not_duplicated(self) -> None:
		current = ["虚构会议：Sample Beta\n\n示例正文不变", "虚构会议三\n\n全新示例内容"]
		captured = [
			{
				"text": "虚构会议：Sample Alpha\n\n示例正文不变",
				"source_file": "项目.txt",
			}
		]

		missing = find_missing_records(current, captured, "项目.txt", threshold=0.60)

		self.assertEqual(missing, ["虚构会议三\n\n全新示例内容"])

	def test_other_source_file_does_not_hide_record(self) -> None:
		missing = find_missing_records(
			["相同正文"],
			[{"text": "相同正文", "source_file": "另一个项目.txt"}],
			"项目.txt",
		)

		self.assertEqual(missing, ["相同正文"])


if __name__ == "__main__":
	unittest.main()