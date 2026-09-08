"""Detect table candidates and derive validated display coordinates from source text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ParsedRow:
	line_id: int
	raw: str
	cells: tuple[str, ...]
	separators: tuple[str, ...]


@dataclass(frozen=True)
class TableCandidate:
	candidate_id: int
	rows: tuple[ParsedRow, ...]


@dataclass(frozen=True)
class ValidatedTable:
	candidate: TableCandidate
	header_line_id: int
	column_count: int


def parse_delimited_row(line_id: int, raw: str) -> ParsedRow | None:
	separator_matches = list(re.finditer(r"\t+| {2,}", raw))
	if not separator_matches:
		return None
	cells: list[str] = []
	separators: list[str] = []
	start = 0
	for match in separator_matches:
		cells.append(raw[start:match.start()])
		separators.append(match.group(0))
		start = match.end()
	cells.append(raw[start:])
	if len(cells) < 2 or any(not cell.strip() for cell in cells):
		return None
	return ParsedRow(line_id, raw, tuple(cells), tuple(separators))


def detect_table_candidates(text: str) -> list[TableCandidate]:
	candidates: list[TableCandidate] = []
	current_rows: list[ParsedRow] = []
	for line_id, raw in enumerate(text.splitlines(), start=1):
		row = parse_delimited_row(line_id, raw)
		if row:
			current_rows.append(row)
			continue
		if len(current_rows) >= 2:
			candidates.append(TableCandidate(len(candidates) + 1, tuple(current_rows)))
		current_rows = []
	if len(current_rows) >= 2:
		candidates.append(TableCandidate(len(candidates) + 1, tuple(current_rows)))
	return candidates


def candidate_payload(candidates: list[TableCandidate]) -> list[dict[str, Any]]:
	return [
		{
			"candidate_id": candidate.candidate_id,
			"rows": [
				{
					"line_id": row.line_id,
					"cells": [
						{"cell_id": f"{row.line_id}:{index}", "text": cell}
						for index, cell in enumerate(row.cells)
					],
				}
				for row in candidate.rows
			],
		}
		for candidate in candidates
	]


def validate_table_structure(
	candidate: TableCandidate,
	structure: dict[str, Any],
) -> ValidatedTable:
	if structure.get("candidate_id") != candidate.candidate_id:
		raise ValueError("candidate_id 不匹配")
	expected_line_ids = [row.line_id for row in candidate.rows]
	if structure.get("line_ids") != expected_line_ids:
		raise ValueError("line_ids 必须完整、连续且保持原文顺序")
	if expected_line_ids != list(range(expected_line_ids[0], expected_line_ids[-1] + 1)):
		raise ValueError("原文行不连续")
	header_line_id = structure.get("header_line_id")
	if header_line_id not in expected_line_ids:
		raise ValueError("表头行不在表格范围内")
	column_count = structure.get("column_count")
	if not isinstance(column_count, int) or isinstance(column_count, bool) or column_count < 2:
		raise ValueError("column_count 无效")
	for row in candidate.rows:
		if len(row.cells) != column_count:
			raise ValueError(
				f"第 {row.line_id} 行为 {len(row.cells)} 列，与声明的 {column_count} 列不一致"
			)
		if len(row.separators) != column_count - 1:
			raise ValueError(f"第 {row.line_id} 行分隔符数量不一致")
		if any(not re.fullmatch(r"\t+| {2,}", separator) for separator in row.separators):
			raise ValueError(f"第 {row.line_id} 行含不明确分隔符")
		reconstructed = "".join(
			cell + (row.separators[index] if index < len(row.separators) else "")
			for index, cell in enumerate(row.cells)
		)
		if reconstructed != row.raw:
			raise ValueError(f"第 {row.line_id} 行无法由原文单元格无损重建")
	return ValidatedTable(candidate, header_line_id, column_count)


def prepare_table_display(
	text: str,
	structures: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
	"""Return validated coordinate tables and suspected-table fallback ranges."""
	candidates = detect_table_candidates(text)
	structures_by_id = {
		structure.get("candidate_id"): structure
		for structure in structures
		if isinstance(structure, dict)
	} if isinstance(structures, list) else {}
	valid_tables: list[dict[str, Any]] = []
	fallbacks: list[dict[str, Any]] = []
	used_line_ids: set[int] = set()
	for candidate in candidates:
		structure = structures_by_id.get(candidate.candidate_id)
		try:
			if structure is None:
				raise ValueError("没有有效表格坐标")
			table = validate_table_structure(candidate, structure)
			line_ids = [row.line_id for row in candidate.rows]
			if used_line_ids.intersection(line_ids):
				raise ValueError("表格行范围重叠")
			used_line_ids.update(line_ids)
			valid_tables.append(
				{
					"candidate_id": candidate.candidate_id,
					"line_ids": line_ids,
					"header_line_id": table.header_line_id,
					"column_count": table.column_count,
				}
			)
		except ValueError as error:
			fallbacks.append(
				{
					"candidate_id": candidate.candidate_id,
					"line_ids": [row.line_id for row in candidate.rows],
					"reason": str(error),
				}
			)
	return valid_tables, fallbacks