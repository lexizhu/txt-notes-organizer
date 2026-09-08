"""Build a deterministic offline review page from bundled fictional TXT notes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from build_html import build_html, load_json, validate_entries
from llm_adapter import apply_line_styles, validate_categories
from watcher import split_complete_records


PROJECT_DIR = Path(__file__).resolve().parent
NOTES_DIR = PROJECT_DIR / "notes"
OUTPUT_PATH = PROJECT_DIR / "output" / "demo-review.html"
DEMO_TIME = "2026-08-29T09:00:00+00:00"

DEMO_METADATA: dict[tuple[str, int], dict[str, Any]] = {
	("learning-plan.txt", 1): {
		"category": "learning",
		"status": "todo",
		"event_time": "2026-10-10T17:00:00+00:00",
	},
	("learning-plan.txt", 2): {
		"category": "learning",
		"status": "done",
		"event_time": None,
	},
	("meeting-notes.txt", 1): {
		"category": "meeting",
		"status": "none",
		"event_time": "2026-09-02T09:00:00+00:00",
	},
	("meeting-notes.txt", 2): {
		"category": "meeting",
		"status": "none",
		"event_time": None,
	},
	("product-demo.txt", 1): {
		"category": "work",
		"status": "todo",
		"event_time": "2026-09-01T17:00:00+00:00",
	},
	("product-demo.txt", 2): {
		"category": "work",
		"status": "done",
		"event_time": "2026-08-15T09:00:00+00:00",
	},
	("table-examples.txt", 1): {
		"category": "work",
		"status": "todo",
		"event_time": "2026-08-31T09:00:00+00:00",
		"table": {"line_ids": [3, 4, 5, 6], "header_line_id": 3, "column_count": 4},
	},
	("table-examples.txt", 2): {
		"category": "idea",
		"status": "none",
		"event_time": None,
		"table": {"line_ids": [3, 4, 5], "header_line_id": 3, "column_count": 3},
	},
	("table-examples.txt", 3): {
		"category": "other",
		"status": "none",
		"event_time": None,
	},
}


def text_sha256(text: str) -> str:
	return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_demo_entries() -> list[dict[str, Any]]:
	entries: list[dict[str, Any]] = []
	for note_path in sorted(NOTES_DIR.glob("*.txt")):
		records, unfinished_tail = split_complete_records(
			note_path.read_text(encoding="utf-8"),
			3,
		)
		if unfinished_tail.strip():
			raise ValueError(f"Demo note has an unfinished record: {note_path.name}")
		for record_index, text in enumerate(records, start=1):
			key = (note_path.name, record_index)
			metadata = DEMO_METADATA.get(key)
			if metadata is None:
				raise ValueError(f"Missing demo metadata: {key}")
			text_hash = text_sha256(text)
			styles = ["h1", *(["plain"] * (len(text.splitlines()) - 1))]
			entry: dict[str, Any] = {
				"id": f"demo-{note_path.stem}-{record_index}",
				"text": text,
				"record_time": DEMO_TIME,
				"event_time": metadata["event_time"],
				"project": note_path.stem,
				"category": metadata["category"],
				"status": metadata["status"],
				"source_file": note_path.name,
				"user_edited": False,
				"created_at": DEMO_TIME,
				"updated_at": DEMO_TIME,
				"display_markdown": apply_line_styles(text, styles),
				"display_text_sha256": text_hash,
				"display_generated_at": DEMO_TIME,
				"display_backend": "fictional-demo",
			}
			if "table" in metadata:
				entry.update({
					"display_tables": [{"candidate_id": 1, **metadata["table"]}],
					"display_tables_text_sha256": text_hash,
					"display_tables_generated_at": DEMO_TIME,
					"display_tables_version": 1,
					"table_backend": "fictional-demo",
				})
			entries.append(entry)
	if set(DEMO_METADATA) != {
		(note_path.name, index)
		for note_path in sorted(NOTES_DIR.glob("*.txt"))
		for index in range(1, len(split_complete_records(note_path.read_text(encoding="utf-8"), 3)[0]) + 1)
	}:
		raise ValueError("Demo metadata contains an entry not present in the TXT notes")
	return entries


def main() -> None:
	settings = load_json(PROJECT_DIR / "config" / "settings.json")
	categories = validate_categories(load_json(PROJECT_DIR / "config" / "categories.json"))
	entries = validate_entries(build_demo_entries(), {item["code"] for item in categories})
	html = build_html(
		entries,
		categories,
		settings["display"]["language"],
		settings["display"]["collapsed_lines"],
		(PROJECT_DIR / "vendor" / "markdown-it.min.js").read_text(encoding="utf-8"),
		(PROJECT_DIR / "output" / "review-mock.html").read_text(encoding="utf-8"),
	)
	OUTPUT_PATH.write_text(html, encoding="utf-8")
	print(f"Built fictional demo page with {len(entries)} records: {OUTPUT_PATH}")


if __name__ == "__main__":
	main()
