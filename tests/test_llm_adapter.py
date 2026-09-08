"""Tests for the LLM adapter validation boundary."""

import unittest
from unittest.mock import patch

from llm_adapter import (
    LLMError,
    OllamaAdapter,
    OllamaTransport,
    leading_note_event_time,
    validate_categories,
)


CATEGORIES = [
    {"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}},
    {"code": "other", "labels": {"zh-CN": "其他", "en": "Other"}},
]


class CategoryValidationTests(unittest.TestCase):
    def test_accepts_code_and_labels_structure(self) -> None:
        self.assertEqual(validate_categories(CATEGORIES), CATEGORIES)

    def test_requires_other_fallback(self) -> None:
        with self.assertRaisesRegex(ValueError, "other"):
            validate_categories([CATEGORIES[0]])

    def test_rejects_unstable_code(self) -> None:
        categories = [
            {"code": "Work Item", "labels": {"en": "Work"}},
            CATEGORIES[1],
        ]
        with self.assertRaisesRegex(ValueError, "code"):
            validate_categories(categories)


class ResultValidationTests(unittest.TestCase):
    def test_accepts_stable_english_values(self) -> None:
        result = OllamaAdapter._validate_result(
            {
                "category": "work",
                "status": "todo",
                "event_time": "2026-08-21T09:00:00+08:00",
            },
            ["work", "other"],
        )

        self.assertEqual(result.category, "work")
        self.assertEqual(result.status, "todo")

    def test_unknown_category_falls_back_to_other(self) -> None:
        result = OllamaAdapter._validate_result(
            {"category": "unknown", "status": "none", "event_time": None},
            ["work", "other"],
        )

        self.assertEqual(result.category, "other")

    def test_rejects_localized_status(self) -> None:
        with self.assertRaisesRegex(LLMError, "status"):
            OllamaAdapter._validate_result(
                {"category": "work", "status": "待办", "event_time": None},
                ["work", "other"],
            )

    def test_event_time_requires_timezone(self) -> None:
        with self.assertRaisesRegex(LLMError, "时区"):
            OllamaAdapter._validate_result(
                {
                    "category": "work",
                    "status": "none",
                    "event_time": "2026-08-21T09:00:00",
                },
                ["work", "other"],
            )

    def test_prompt_uses_strict_record_level_status_semantics(self) -> None:
        source = __import__("inspect").getsource(OllamaAdapter.analyze)

        self.assertIn("会议纪要", source)
        self.assertIn("不确定时必须返回 none", source)
        self.assertIn("绝不能猜测为 done", source)
        self.assertIn("未来会议、截止日期或计划时间", source)

    def test_standalone_leading_mmdd_uses_capture_year_and_timezone(self) -> None:
        self.assertEqual(
            leading_note_event_time(
                "0903\n主题：为下周二会议做准备",
                "2026-09-03T16:02:14+08:00",
            ),
            "2026-09-03T00:00:00+08:00",
        )

    def test_non_date_or_invalid_leading_line_is_ignored(self) -> None:
        self.assertIsNone(leading_note_event_time("主题 0903\n正文", "2026-09-03T16:02:14+08:00"))
        self.assertIsNone(leading_note_event_time("1332\n正文", "2026-09-03T16:02:14+08:00"))


class OllamaRequestTests(unittest.TestCase):
    def test_invalid_model_response_does_not_echo_sensitive_content(self) -> None:
        class InvalidTransport:
            def post_json(self, path, payload):
                return {"message": {"content": "private-note-body secret-value"}}

        adapter = OllamaAdapter("http://localhost:11434", "test-model", transport=InvalidTransport())
        for operation in (
            lambda: adapter.analyze("正文", "2026-09-03T10:00:00+08:00", CATEGORIES),
            lambda: adapter.format_markdown("正文"),
        ):
            with self.assertRaises(LLMError) as raised:
                operation()
            self.assertNotIn("private-note-body", str(raised.exception))
            self.assertNotIn("secret-value", str(raised.exception))

    def test_timeout_becomes_clear_llm_error(self) -> None:
        adapter = OllamaAdapter("http://localhost:11434", "test-model", timeout_seconds=300)

        with patch("llm_adapter.urlopen", side_effect=TimeoutError), self.assertRaisesRegex(
            LLMError,
            "Ollama 调用超过 300 秒",
        ):
            adapter._post_json("/api/chat", {"model": "test-model"})

    def test_adapter_keeps_legacy_properties_and_delegates_requests(self) -> None:
        class RecordingTransport:
            def __init__(self) -> None:
                self.calls = []

            def post_json(self, path, payload):
                self.calls.append((path, payload))
                return {"message": {"content": '{"category":"work","status":"none","event_time":null}'}}

        transport = RecordingTransport()
        adapter = OllamaAdapter(
            "http://localhost:11434/",
            "test-model",
            timeout_seconds=300,
            transport=transport,
        )

        result = adapter.analyze("测试正文", "2026-08-27T10:00:00+08:00", CATEGORIES)

        self.assertEqual(adapter.base_url, "http://localhost:11434")
        self.assertEqual(adapter.model, "test-model")
        self.assertEqual(adapter.timeout_seconds, 300)
        self.assertEqual(result.category, "work")
        self.assertEqual(transport.calls[0][0], "/api/chat")
        payload = transport.calls[0][1]
        self.assertEqual(payload["model"], "test-model")
        self.assertFalse(payload["stream"])
        self.assertEqual(payload["options"], {"temperature": 0})
        self.assertIn("会议纪要", payload["messages"][1]["content"])

    def test_leading_note_date_overrides_future_model_event(self) -> None:
        class FutureEventTransport:
            def post_json(self, path, payload):
                return {"message": {"content": '{"category":"other","status":"none","event_time":"2026-09-08T00:00:00+08:00"}'}}

        adapter = OllamaAdapter("http://localhost:11434", "test-model", transport=FutureEventTransport())

        result = adapter.analyze(
            "0903\n主题：为下周二会议做准备",
            "2026-09-03T16:02:14+08:00",
            CATEGORIES,
        )

        self.assertEqual(result.event_time, "2026-09-03T00:00:00+08:00")

    def test_transport_normalizes_base_url_without_changing_request_contract(self) -> None:
        transport = OllamaTransport("http://localhost:11434/", "test-model", 300)

        self.assertEqual(transport.base_url, "http://localhost:11434")
        self.assertEqual(transport.model, "test-model")
        self.assertEqual(transport.timeout_seconds, 300)

    def test_format_markdown_keeps_ollama_payload_and_local_text_protection(self) -> None:
        class FormattingTransport:
            def __init__(self) -> None:
                self.call = None

            def post_json(self, path, payload):
                self.call = (path, payload)
                return {"message": {"content": '{"styles":[{"line":1,"style":"h1"}]}'}}

        transport = FormattingTransport()
        adapter = OllamaAdapter("http://localhost:11434", "test-model", transport=transport)

        formatted = adapter.format_markdown("标题\n正文")

        self.assertEqual(formatted, "# 标题\n正文")
        path, payload = transport.call
        self.assertEqual(path, "/api/chat")
        self.assertEqual(payload["model"], "test-model")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"], {"temperature": 0})
        style_schema = payload["format"]["properties"]["styles"]
        self.assertEqual(style_schema["minItems"], 0)
        self.assertEqual(style_schema["items"]["properties"]["line"]["maximum"], 2)
        self.assertNotIn("plain", style_schema["items"]["properties"]["style"]["enum"])

    def test_identify_tables_keeps_ollama_payload_and_coordinate_result(self) -> None:
        class TableTransport:
            def __init__(self) -> None:
                self.call = None

            def post_json(self, path, payload):
                self.call = (path, payload)
                return {
                    "message": {
                        "content": json.dumps({
                            "tables": [{
                                "candidate_id": 1,
                                "line_ids": [1, 2],
                                "header_line_id": 1,
                                "column_count": 2,
                            }]
                        })
                    }
                }

        import json

        transport = TableTransport()
        adapter = OllamaAdapter("http://localhost:11434", "test-model", transport=transport)

        tables = adapter.identify_tables("A\tB\n1\t2")

        self.assertEqual(tables[0]["line_ids"], [1, 2])
        path, payload = transport.call
        self.assertEqual(path, "/api/chat")
        self.assertEqual(payload["model"], "test-model")
        self.assertFalse(payload["stream"])
        self.assertFalse(payload["think"])
        self.assertEqual(payload["options"], {"temperature": 0})
        self.assertEqual(payload["format"]["properties"]["tables"]["minItems"], 1)


if __name__ == "__main__":
    unittest.main()