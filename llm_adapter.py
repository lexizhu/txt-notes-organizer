"""Define model transports, prompts, and validation for safe note enrichment."""

from __future__ import annotations

import json
import re
import ssl
from pathlib import Path
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit

from table_layout import candidate_payload, detect_table_candidates


VALID_STATUSES = {"todo", "done", "none"}
CATEGORY_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
DISPLAY_LINE_STYLES = {"plain", "h1", "h2", "bullet", "sub_bullet", "bold"}
CHINESE_SECTION_PATTERN = re.compile(r"^\s*[一二三四五六七八九十百]+\s*[、，,.．]\s*\S")
NUMBERED_ITEM_PATTERN = re.compile(r"^\s*(?:\d+|[一二三四五六七八九十百]+)\s*[.．、]\s*\S")


class LLMError(RuntimeError):
    """A user-facing error from the configured language model."""


class JsonTransport(Protocol):
    """Transport one JSON request without owning prompts or result validation."""

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class AnalysisResult:
    category: str
    status: str
    event_time: str | None


def leading_note_event_time(text: str, record_time: str) -> str | None:
    """Resolve a standalone leading MMDD marker using the capture year and timezone."""
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    match = re.fullmatch(r"(\d{2})(\d{2})", first_line)
    if not match:
        return None
    try:
        captured = datetime.fromisoformat(record_time.replace("Z", "+00:00"))
        if captured.tzinfo is None:
            return None
        event_date = captured.replace(
            month=int(match.group(1)),
            day=int(match.group(2)),
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    except ValueError:
        return None
    return event_date.isoformat(timespec="seconds")


def validate_display_markdown(original: str, formatted: str) -> str:
    """Accept only Markdown markers that preserve every original character and line."""
    if not isinstance(formatted, str):
        raise LLMError("display_markdown 必须是字符串。")
    original_lines = original.split("\n")
    formatted_lines = formatted.split("\n")
    if len(original_lines) != len(formatted_lines):
        raise LLMError("排版结果改变了原文行数。")

    for line_number, (original_line, formatted_line) in enumerate(
        zip(original_lines, formatted_lines, strict=True),
        start=1,
    ):
        if formatted_line.count("**") % 2:
            raise LLMError(f"排版结果第 {line_number} 行包含未配对的 **。")
        without_bold = formatted_line.replace("**", "")
        candidates = {without_bold}
        heading_match = re.match(r"^#{1,2} (.*)$", without_bold)
        if heading_match:
            candidates.add(heading_match.group(1))
        list_match = re.match(r"^\s*- (.*)$", without_bold)
        if list_match:
            candidates.add(list_match.group(1))
        if original_line not in candidates:
            raise LLMError(f"排版结果修改了第 {line_number} 行的原文。")
    return formatted


def apply_line_styles(original: str, styles: list[str]) -> str:
    """Deterministically add Markdown markers without accepting model-written text."""
    original_lines = original.split("\n")
    if len(styles) != len(original_lines):
        raise LLMError("排版样式数量与原文行数不一致。")
    styles = normalize_line_styles(original_lines, styles)
    formatted_lines: list[str] = []
    for line_number, (line, style) in enumerate(
        zip(original_lines, styles, strict=True),
        start=1,
    ):
        if style not in DISPLAY_LINE_STYLES:
            raise LLMError(f"第 {line_number} 行包含未知排版样式：{style}")
        if not line or style == "plain":
            formatted_lines.append(line)
        elif style == "h1":
            formatted_lines.append(f"# {line}")
        elif style == "h2":
            formatted_lines.append(f"## {line}")
        elif style == "bullet":
            formatted_lines.append(f"- {line}")
        elif style == "sub_bullet":
            formatted_lines.append(f"   - {line}")
        else:
            formatted_lines.append(f"**{line}**")
    return validate_display_markdown(original, "\n".join(formatted_lines))


def apply_sparse_line_styles(original: str, assignments: list[dict[str, Any]]) -> str:
    """Apply explicit one-based line assignments; omitted lines remain plain."""
    original_lines = original.split("\n")
    styles = ["plain"] * len(original_lines)
    seen_lines: set[int] = set()
    for assignment in assignments:
        if not isinstance(assignment, dict) or set(assignment) != {"line", "style"}:
            raise LLMError("排版样式必须只包含 line 和 style。")
        line = assignment["line"]
        style = assignment["style"]
        if isinstance(line, bool) or not isinstance(line, int) or not 1 <= line <= len(styles):
            raise LLMError("排版样式包含无效行号。")
        if line in seen_lines:
            raise LLMError(f"排版样式包含重复行号：{line}")
        if not isinstance(style, str) or style == "plain" or style not in DISPLAY_LINE_STYLES:
            raise LLMError(f"第 {line} 行包含无效的稀疏排版样式。")
        seen_lines.add(line)
        styles[line - 1] = style
    return apply_line_styles(original, styles)


def normalize_line_styles(original_lines: list[str], styles: list[str]) -> list[str]:
    """Apply deterministic structure rules after the model's advisory labels."""
    normalized = list(styles)
    first_non_empty = next((index for index, line in enumerate(original_lines) if line.strip()), None)
    for index, line in enumerate(original_lines):
        if not line.strip():
            normalized[index] = "plain"
        elif index == first_non_empty:
            normalized[index] = "h1"
        elif CHINESE_SECTION_PATTERN.match(line):
            normalized[index] = "h2"
        elif NUMBERED_ITEM_PATTERN.match(line):
            previous_is_numbered = index > 0 and bool(NUMBERED_ITEM_PATTERN.match(original_lines[index - 1]))
            next_is_numbered = (
                index + 1 < len(original_lines)
                and bool(NUMBERED_ITEM_PATTERN.match(original_lines[index + 1]))
            )
            starts_subsection = (index == 0 or not original_lines[index - 1].strip())
            normalized[index] = (
                "h2"
                if starts_subsection and not previous_is_numbered and not next_is_numbered
                else "plain"
            )

    for style in ("bullet", "sub_bullet"):
        index = 0
        while index < len(normalized):
            if normalized[index] != style:
                index += 1
                continue
            end = index
            while end < len(normalized) and normalized[end] == style and original_lines[end].strip():
                end += 1
            if end - index < 2:
                for item_index in range(index, end):
                    normalized[item_index] = "plain"
            index = max(end, index + 1)
    return normalized


def styles_from_markdown(original: str, formatted: str) -> list[str]:
    """Recover the restricted style labels from an already validated cache."""
    validate_display_markdown(original, formatted)
    styles: list[str] = []
    for line in formatted.split("\n"):
        if line.startswith("## "):
            styles.append("h2")
        elif line.startswith("# "):
            styles.append("h1")
        elif re.match(r"^\s{2,}- ", line):
            styles.append("sub_bullet")
        elif line.startswith("- "):
            styles.append("bullet")
        elif len(line) >= 4 and line.startswith("**") and line.endswith("**"):
            styles.append("bold")
        else:
            styles.append("plain")
    return styles


def normalize_display_markdown(original: str, formatted: str) -> str:
    return apply_line_styles(original, styles_from_markdown(original, formatted))


def validate_categories(categories: Any) -> list[dict[str, Any]]:
    """Validate stable category codes and multilingual display labels."""
    if not isinstance(categories, list) or not categories:
        raise ValueError("categories.json 必须是一个非空数组")

    seen_codes: set[str] = set()
    for category in categories:
        if not isinstance(category, dict):
            raise ValueError("每个分类必须是一个 JSON 对象")
        code = category.get("code")
        labels = category.get("labels")
        if not isinstance(code, str) or not CATEGORY_CODE_PATTERN.fullmatch(code):
            raise ValueError("分类 code 只能使用小写英文、数字和下划线")
        if code in seen_codes:
            raise ValueError(f"分类 code 重复：{code}")
        if not isinstance(labels, dict) or not labels:
            raise ValueError(f"分类 {code} 的 labels 必须是非空对象")
        if not all(
            isinstance(language, str)
            and language
            and isinstance(label, str)
            and label.strip()
            for language, label in labels.items()
        ):
            raise ValueError(f"分类 {code} 的语言代码和显示名称都必须是非空字符串")
        seen_codes.add(code)

    if "other" not in seen_codes:
        raise ValueError("分类配置必须包含固定的 other 分类")
    return categories


class OllamaTransport:
    """Send JSON requests to Ollama without owning business prompts or validation."""

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return json.load(response)
        except HTTPError as error:
            details = error.read().decode("utf-8", errors="replace")[:300]
            if error.code == 404 and "model" in details.lower():
                raise LLMError(
                    f"找不到模型 {self.model}，请运行：ollama pull {self.model}"
                ) from error
            raise LLMError(f"Ollama 请求失败（HTTP {error.code}）。") from error
        except URLError as error:
            raise LLMError("无法连接 Ollama，请确认 Ollama 应用正在运行。") from error
        except TimeoutError as error:
            raise LLMError(f"Ollama 调用超过 {self.timeout_seconds:g} 秒。") from error
        except (OSError, json.JSONDecodeError) as error:
            raise LLMError("读取 Ollama 响应失败。") from error


class StandardOpenAITransport:
    """Send business chat payloads through the standard OpenAI protocol."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 120,
        ca_bundle_path: str = "",
        *,
        _allow_http_for_testing: bool = False,
    ) -> None:
        self.base_url = self._validate_base_url(base_url, _allow_http_for_testing)
        if not isinstance(api_key, str) or not api_key or "\r" in api_key or "\n" in api_key:
            raise ValueError("API Key 必须是非空单行字符串")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("云端模型名称不能为空")
        if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
            raise ValueError("云端请求超时必须大于 0 秒")
        self.api_key = api_key
        self.model = model.strip()
        self.timeout_seconds = float(timeout_seconds)
        self.ssl_context = self._create_ssl_context(ca_bundle_path)

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path != "/api/chat":
            raise ValueError("标准 OpenAI 传输只接受统一聊天请求")
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("云端聊天请求缺少 messages")
        request_payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_with_schema(messages, payload.get("format")),
            "stream": False,
        }
        options = payload.get("options")
        if isinstance(options, dict) and "temperature" in options:
            request_payload["temperature"] = options["temperature"]
        request = Request(
            self.base_url + "/chat/completions",
            data=json.dumps(request_payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        return self._send_request(request)

    def _send_request(self, request: Request) -> dict[str, Any]:
        try:
            request_options = {"timeout": self.timeout_seconds}
            if self.ssl_context is not None:
                request_options["context"] = self.ssl_context
            with urlopen(request, **request_options) as response:
                result = json.load(response)
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("content must be a string")
            return {"message": {"content": content}}
        except HTTPError as error:
            try:
                error.read()
            finally:
                error.close()
            if error.code in {401, 403}:
                raise LLMError("云端 API 拒绝了认证，请检查 API Key。") from error
            if error.code == 400:
                raise LLMError("云端 API 拒绝了请求，请检查模型和请求参数兼容性。") from error
            if error.code == 404:
                raise LLMError("云端 API 地址或模型不存在，请检查设置。") from error
            if error.code == 408:
                raise LLMError("云端 API 请求超时，请稍后重试。") from error
            if error.code == 429:
                raise LLMError("云端 API 请求过于频繁或额度不足，请稍后重试。") from error
            if 500 <= error.code <= 599:
                raise LLMError(f"云端 API 服务暂时不可用（HTTP {error.code}）。") from error
            raise LLMError(f"云端 API 请求失败（HTTP {error.code}）。") from error
        except URLError as error:
            raise LLMError("无法连接云端 API，请检查地址和网络。") from error
        except TimeoutError as error:
            raise LLMError(f"云端 API 调用超过 {self.timeout_seconds:g} 秒。") from error
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise LLMError("云端 API 没有返回有效的 Chat Completions 响应。") from error
        except OSError as error:
            raise LLMError("读取云端 API 响应失败。") from error

    @staticmethod
    def _validate_base_url(base_url: str, allow_http_for_testing: bool) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("云端 API Base URL 不能为空")
        normalized = base_url.strip().rstrip("/")
        if any(ord(character) < 33 for character in normalized):
            raise ValueError("云端 API Base URL 不能包含空白或控制字符")
        parsed = urlsplit(normalized)
        allowed_scheme = parsed.scheme == "https" or (
            allow_http_for_testing
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
        )
        if not allowed_scheme:
            raise ValueError("云端 API Base URL 必须使用 HTTPS")
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("云端 API Base URL 的主机或凭据无效")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("云端 API Base URL 的端口无效") from error
        if parsed.query or parsed.fragment:
            raise ValueError("云端 API Base URL 不能包含查询参数或片段")
        return normalized

    @staticmethod
    def _messages_with_schema(messages: list[Any], schema: Any) -> list[Any]:
        if not isinstance(schema, dict):
            return messages
        schema_message = {
            "role": "system",
            "content": (
                "Return only one JSON value matching this JSON Schema exactly. "
                "Do not use Markdown fences or add explanatory text.\n"
                f"JSON Schema: {json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
            ),
        }
        return [schema_message, *messages]

    @staticmethod
    def _create_ssl_context(ca_bundle_path: str) -> ssl.SSLContext | None:
        if not isinstance(ca_bundle_path, str):
            raise ValueError("CA Certificate File 必须是字符串")
        if not ca_bundle_path.strip():
            return None
        path = Path(ca_bundle_path).expanduser()
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError("CA Certificate File 必须是存在的绝对文件路径")
        try:
            return ssl.create_default_context(cafile=str(path))
        except (OSError, ssl.SSLError) as error:
            raise ValueError("CA Certificate File 不是有效的 PEM 证书文件") from error


class OllamaAdapter:
    """Apply note business rules through an Ollama transport."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 120,
        transport: JsonTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.transport = transport or OllamaTransport(
            self.base_url,
            self.model,
            self.timeout_seconds,
        )

    def analyze(
        self,
        text: str,
        record_time: str,
        categories: list[dict[str, Any]],
    ) -> AnalysisResult:
        categories = validate_categories(categories)
        category_codes = [category["code"] for category in categories]
        category_help = [
            {
                "code": category["code"],
                "labels": category["labels"],
            }
            for category in categories
        ]
        schema = {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": category_codes},
                "status": {"type": "string", "enum": sorted(VALID_STATUSES)},
                "event_time": {"type": ["string", "null"]},
            },
            "required": ["category", "status", "event_time"],
            "additionalProperties": False,
        }
        prompt = (
            "分析下面这条个人笔记。只返回符合 schema 的 JSON。\n"
            f"捕获时间：{record_time}\n"
            f"可选分类：{json.dumps(category_help, ensure_ascii=False)}\n"
            "category 必须返回分类 code。status 必须根据整条记录的主要语义判断，"
            "只能是 todo、done、none："
            "todo 仅用于整条记录主要要求未来执行一个或多个明确行动；"
            "done 仅用于整条记录主要是在明确宣告某个行动已经完成；"
            "none 用于会议纪要、情况说明、知识记录、分析材料和历史陈述。"
            "会议纪要内部即使提到待办、结论、已沟通或其他过去动作，整条记录仍默认 none。"
            "不确定时必须返回 none，绝不能猜测为 done。"
            "event_time 提取笔记中的事件时间，输出带时区的 ISO 8601；"
            "会议纪要应返回被记录会议的发生时间；若开头有独立日期标记，优先使用该日期，"
            "不要误用纪要中提到的未来会议、截止日期或计划时间。"
            "无法可靠判断时返回 null。相对日期以捕获时间为基准。\n"
            f"笔记原文：\n{text}"
        )
        payload = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "messages": [
                {
                    "role": "system",
                    "content": "你是准确的笔记分类器，不要改写原文。",
                },
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": 0},
        }
        response = self._post_json("/api/chat", payload)
        try:
            content = response["message"]["content"]
            result = json.loads(content)
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise LLMError("模型没有返回有效 JSON。") from error
        validated = self._validate_result(result, category_codes)
        explicit_event_time = leading_note_event_time(text, record_time)
        if explicit_event_time is None:
            return validated
        return AnalysisResult(
            category=validated.category,
            status=validated.status,
            event_time=explicit_event_time,
        )

    def format_markdown(self, text: str) -> str:
        """Add a restricted set of Markdown markers without editing the text."""
        original_lines = text.split("\n")
        sparse_styles = sorted(DISPLAY_LINE_STYLES - {"plain"})
        schema = {
            "type": "object",
            "properties": {
                "styles": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "line": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": len(original_lines),
                            },
                            "style": {"type": "string", "enum": sparse_styles},
                        },
                        "required": ["line", "style"],
                        "additionalProperties": False,
                    },
                    "minItems": 0,
                    "maxItems": len(original_lines),
                }
            },
            "required": ["styles"],
            "additionalProperties": False,
        }
        prompt = (
            "你只做逐行结构识别，不得返回或改写原文。只返回符合 schema 的 JSON。\n"
            "硬性要求：\n"
            "1. styles 只列出需要非 plain 样式的行，每项包含一基行号 line 和 style。\n"
            "2. line 不得重复或越界；style 只能是 h1、h2、bullet、sub_bullet、bold。\n"
            "3. 空行、普通行和不确定的行不要返回，程序会自动保持 plain。\n"
            "4. h1 仅用于整条记录总标题；h2 用于章节标题；bullet/sub_bullet 用于列表；"
            "bold 仅用于需要整体强调的短行。\n"
            "5. 禁止返回原文、解释、总结或任何额外字段。\n"
            f"带行号的原文：{json.dumps(list(enumerate(original_lines, start=1)), ensure_ascii=False)}"
        )
        payload = {
            "model": self.model,
            "stream": False,
            "think": False,
            "format": schema,
            "messages": [
                {
                    "role": "system",
                    "content": "只为每行选择样式枚举，绝不输出或改写原文。",
                },
                {"role": "user", "content": prompt},
            ],
            "options": {"temperature": 0},
        }
        response = self._post_json("/api/chat", payload)
        try:
            content = response["message"]["content"]
            result = json.loads(content)
            styles = result["styles"]
            if not isinstance(styles, list) or not all(
                isinstance(style, dict) for style in styles
            ):
                raise TypeError("styles must be an array of objects")
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise LLMError("模型没有返回有效排版 JSON。") from error
        return apply_sparse_line_styles(text, styles)

    def identify_tables(self, text: str) -> list[dict[str, Any]]:
        """Return table coordinates only; all cell text remains in the original note."""
        candidates = detect_table_candidates(text)
        if not candidates:
            return []
        candidate_ids = [candidate.candidate_id for candidate in candidates]
        schema = {
            "type": "object",
            "properties": {
                "tables": {
                    "type": "array",
                    "minItems": len(candidates),
                    "maxItems": len(candidates),
                    "items": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "integer", "enum": candidate_ids},
                            "line_ids": {"type": "array", "items": {"type": "integer"}},
                            "header_line_id": {"type": "integer"},
                            "column_count": {"type": "integer", "minimum": 2},
                        },
                        "required": [
                            "candidate_id",
                            "line_ids",
                            "header_line_id",
                            "column_count",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["tables"],
            "additionalProperties": False,
        }
        prompt = (
            "这些候选已由程序确认每行含 Tab 或连续空格分隔。判断多行是否表达同一组列。"
            "只返回坐标结构，禁止返回、改写或补充正文。"
            "每个候选都必须返回；即使某行列数不一致也不得省略，程序会严格校验并回退。"
            "column_count 采用表头列数，header_line_id 采用表头原文行号。\n"
            f"候选数据：{json.dumps(candidate_payload(candidates), ensure_ascii=False)}"
        )
        response = self._post_json(
            "/api/chat",
            {
                "model": self.model,
                "stream": False,
                "think": False,
                "format": schema,
                "messages": [
                    {"role": "system", "content": "只返回表格坐标，不输出正文。"},
                    {"role": "user", "content": prompt},
                ],
                "options": {"temperature": 0},
            },
        )
        try:
            content = response["message"]["content"]
            result = json.loads(content)
            structures = result["tables"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise LLMError("模型没有返回有效的表格坐标 JSON。") from error
        if not isinstance(structures, list) or not all(
            isinstance(structure, dict) for structure in structures
        ):
            raise LLMError("tables 必须是对象数组。")
        returned_ids = sorted(structure.get("candidate_id") for structure in structures)
        if returned_ids != candidate_ids:
            raise LLMError("表格坐标没有完整覆盖全部候选。")
        return structures

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Compatibility boundary for existing callers and tests."""
        return self.transport.post_json(path, payload)

    @staticmethod
    def _validate_result(result: Any, category_codes: list[str]) -> AnalysisResult:
        if not isinstance(result, dict):
            raise LLMError("模型结果必须是 JSON 对象。")
        category = result.get("category")
        status = result.get("status")
        event_time = result.get("event_time")
        if category not in category_codes:
            category = "other"
        if status not in VALID_STATUSES:
            raise LLMError(f"模型返回了未知 status：{status}")
        if event_time is not None:
            if not isinstance(event_time, str):
                raise LLMError("event_time 必须是 ISO 8601 字符串或 null。")
            try:
                parsed_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
            except ValueError as error:
                raise LLMError(f"event_time 不是有效的 ISO 8601：{event_time}") from error
            if parsed_time.tzinfo is None:
                raise LLMError("event_time 必须包含时区。")
        return AnalysisResult(category=category, status=status, event_time=event_time)