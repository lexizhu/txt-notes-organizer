"""Tests for the offline HTML builder."""

import unittest

from build_html import (
    build_html,
    javascript_json,
    prepare_entries_for_display,
    validate_entries,
)


CATEGORIES = [
    {"code": "work", "labels": {"zh-CN": "工作", "en": "Work"}},
    {"code": "other", "labels": {"zh-CN": "其他", "en": "Other"}},
]


def entry(text: str = "测试笔记") -> dict:
    return {
        "id": "one",
        "text": text,
        "record_time": "2026-08-21T10:00:00+08:00",
        "event_time": None,
        "project": "项目甲",
        "category": "work",
        "status": "todo",
        "source_file": "项目甲.txt",
        "user_edited": False,
        "created_at": "2026-08-21T10:01:00+08:00",
        "updated_at": "2026-08-21T10:01:00+08:00",
    }


TEMPLATE = """<title>笔记回顾 - 交互原型</title>
/* BUILD:MARKDOWN_IT:START */old/* BUILD:MARKDOWN_IT:END */
/* BUILD:CATEGORIES:START */old/* BUILD:CATEGORIES:END */
/* BUILD:ENTRIES:START */old/* BUILD:ENTRIES:END */
/* BUILD:LANGUAGE:START */old/* BUILD:LANGUAGE:END */
/* BUILD:COLLAPSED_LINES:START */old/* BUILD:COLLAPSED_LINES:END */
/* BUILD:FOOTER:START */old/* BUILD:FOOTER:END */
"""
MARKDOWN_IT = "window.markdownit=function(){};"


class BuildHtmlTests(unittest.TestCase):
    def test_template_embeds_offline_notebook_favicon(self) -> None:
        import base64
        import struct
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")
        favicon = text.split('<link rel="icon"', 1)[1].split(">", 1)[0]
        href = favicon.split('href="', 1)[1].split('"', 1)[0]

        self.assertIn('type="image/png"', favicon)
        self.assertIn('id="page-icon"', favicon)
        self.assertIn('sizes="32x32"', favicon)
        self.assertTrue(href.startswith("data:image/png;base64,"))
        self.assertNotIn('href="http://', favicon)
        self.assertNotIn('href="https://', favicon)
        png = base64.b64decode(href.split(",", 1)[1], validate=True)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (32, 32))

    def test_http_mode_uses_versioned_favicon_without_breaking_offline_fallback(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")

        self.assertIn('window.location.protocol === "http:"', text)
        self.assertIn('["127.0.0.1", "localhost"].includes(window.location.hostname)', text)
        self.assertIn('document.querySelector("#page-icon").href = "/favicon.ico?v=notebook-v3"', text)
        self.assertIn('href="data:image/png;base64,', text)

    def test_template_keeps_compact_hanging_list_css(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")
        self.assertIn("list-style-position: outside", text)
        self.assertIn(".entry-text ul > li { line-height: 1.35; }", text)
        self.assertIn(".entry-text li { margin: 0;", text)
        self.assertNotIn("counter-reset: ordered-item", text)

    def test_event_time_near_to_far_sorts_newest_first(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")

        self.assertIn('<option value="event-desc">事件时间：近到远</option>', text)
        self.assertIn('if (sortOrder.value === "event-desc")', text)
        self.assertIn("return right.event_time.localeCompare(left.event_time);", text)

    def test_template_contains_local_actions_and_hidden_view(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")

        self.assertIn('id="organize-button"', text)
        self.assertIn('id="hidden-toggle"', text)
        self.assertIn("/api/organize", text)
        self.assertIn("/api/records/${encodeURIComponent(entry.id)}/${action}", text)
        self.assertIn('entry.hidden === true', text)
        self.assertIn('hiddenView ? "× 关闭隐藏区"', text)
        self.assertIn('`已隐藏的记录 (${hiddenTotal})`', text)
        self.assertIn('"退出隐藏区并回到主页面"', text)
        self.assertIn('localStorage.removeItem(legacyProjectLayoutStorageKey)', text)
        self.assertIn('my-notes-tool.project-tabs.v2', text)

    def test_template_contains_local_diagnostics_center(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")

        self.assertIn('id="diagnostics-button"', text)
        self.assertIn('id="diagnostics-dialog"', text)
        self.assertIn('window.fetch("/api/diagnostics"', text)
        self.assertIn("session.capabilities?.diagnostics === true", text)
        self.assertIn('id="diagnostics-copy"', text)
        self.assertIn("no note text, secrets, URLs, or full paths included", text)

    def test_permanent_delete_exists_only_in_interactive_hidden_render_branch(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")
        render_entry = text.split("function renderEntry(entry, index) {", 1)[1].split(
            "function setServiceStatus", 1
        )[0]

        self.assertIn("if (hiddenView && interactiveMode && apiToken && purgeEnabled)", render_entry)
        self.assertIn('purge.textContent = "彻底删除"', render_entry)
        self.assertNotIn('purge.textContent = "彻底删除"', text.split("function renderEntry", 1)[0])

    def test_permanent_delete_dialog_has_required_warning_and_safe_default(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")

        self.assertIn("此操作将永久删除该记录，不可恢复。", text)
        self.assertIn("本工具不会自动修改你的原始文件", text)
        self.assertIn("请你立即手动去原 TXT 里删除这条内容", text)
        self.assertIn("否则下次点击整理时，它会重新出现", text)
        self.assertIn('id="purge-cancel"', text)
        self.assertIn('id="purge-confirm"', text)
        self.assertIn("purgeCancel.focus()", text)
        self.assertIn('id="purge-reminder"', text)

    def test_template_contains_detailed_organize_progress(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")

        self.assertIn('id="organize-progress"', text)
        self.assertIn('class="progress-spinner"', text)
        self.assertIn('role="progressbar"', text)
        self.assertIn('data-progress-stage="classifying"', text)
        self.assertIn('data-progress-stage="rebuilding"', text)
        self.assertIn('organizeButton.textContent = "正在整理…"', text)
        self.assertIn("updateOrganizeProgress(job)", text)
        self.assertIn("job.progress?.message", text)
        self.assertIn(".progress-step.is-current {", text)
        self.assertIn("border: 2px solid #6f91b8;", text)
        self.assertIn("box-shadow: 0 5px 12px rgba(54, 95, 145, 0.2);", text)
        self.assertIn(".progress-step.is-done { color: #65788f; background: #edf2f7; }", text)
        self.assertIn('id="progress-dismiss"', text)
        self.assertIn('id="progress-cancel"', text)
        self.assertIn("取消整理", text)
        self.assertIn("正在安全取消", text)
        self.assertIn('job.status === "cancelled"', text)
        self.assertIn('/cancel`', text)
        self.assertIn("下次会继续未完成部分", text)
        self.assertIn("重新处理已取消；下次请重新计算范围", text)
        self.assertIn('stage === "rebuilding"', text)
        self.assertIn("页面重建已经开始，请等待完成", text)
        self.assertIn("预计最多还需约", text)
        self.assertIn("已超过预计上限", text)
        self.assertIn("cancel_wait_seconds", text)
        self.assertIn("window.setTimeout(resolve, 2000)", text)
        self.assertIn('id="progress-elapsed"', text)
        self.assertIn("window.setInterval(refreshElapsedTime, 1000)", text)
        self.assertIn("逐条处理中", text)
        self.assertIn("本条已等待超过 3 分钟", text)

    def test_static_mode_disables_writes_but_keeps_hidden_view_available(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")

        static_branch = text.split("if (!interactiveMode) {", 1)[1].split("return;", 1)[0]
        self.assertIn("organizeButton.disabled = true", static_branch)
        self.assertIn("hiddenToggle.disabled = false", static_branch)
        self.assertIn('setServiceStatus("离线只读模式")', static_branch)

    def test_model_settings_ui_preserves_privacy_and_release_boundaries(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")

        self.assertIn('id="model-settings-button"', text)
        self.assertIn('id="model-settings-dialog"', text)
        self.assertIn('id="ollama-model-status"', text)
        self.assertIn('id="reprocess-target-model"', text)
        self.assertIn("本地 Ollama", text)
        self.assertIn("推荐 · 隐私优先", text)
        self.assertIn("笔记内容会发送到你配置的服务商，并可能产生费用", text)
        self.assertIn("切换模型不会自动上传或重新处理历史笔记", text)
        self.assertIn("通常只需填写标有“必填”的项目", text)
        self.assertIn('class="required-mark">必填</span>', text)
        self.assertIn('id="api-key-required">首次配置必填</span>', text)
        self.assertIn("https://api.openai.com/v1", text)
        self.assertIn("gpt-4.1-mini", text)
        self.assertIn('id="openai-ca-bundle"', text)
        self.assertIn("例如 sk-...；请勿在聊天或截图中分享", text)
        self.assertIn('autocomplete="new-password"', text)
        self.assertIn(".settings-grid[hidden] { display: none; }", text)
        self.assertIn('cloud_variant: "standard_openai"', text)
        self.assertIn('window.fetch("/api/model-settings"', text)
        self.assertIn('apiRequest("/api/model-settings/test"', text)
        self.assertIn("session.capabilities?.model_settings === true", text)
        self.assertIn("modelSettingsButton.disabled = !modelSettingsEnabled", text)
        self.assertIn("正在使用虚拟内容测试连接", text)
        self.assertIn("未发送任何真实笔记", text)
        self.assertIn("if (cloudApiKey.value) payload.api_key = cloudApiKey.value", text)
        self.assertIn("modelSettingsForm.reportValidity()", text)
        self.assertIn('cloudApiKey.required = isCloud && !modelSettingsState?.api_key_configured', text)
        self.assertNotIn("cloudApiKey.value = modelSettingsState", text)
        self.assertIn(".settings-grid { grid-template-columns: 1fr; }", text)

        static_branch = text.split("if (!interactiveMode) {", 1)[1].split("return;", 1)[0]
        self.assertIn("modelSettingsButton.disabled = true", static_branch)

    def test_reprocess_plan_ui_is_preview_only_and_explains_risk(self) -> None:
        from pathlib import Path

        template = Path(__file__).parents[1] / "output" / "review-mock.html"
        text = template.read_text(encoding="utf-8")

        self.assertIn('id="reprocess-plan-section" hidden', text)
        self.assertIn("先计算范围和预计成本，不会调用模型或修改记录", text)
        self.assertIn('name="reprocess-mode" value="full" checked', text)
        self.assertIn('name="reprocess-mode" value="repair"', text)
        self.assertIn('id="reprocess-project-list"', text)
        self.assertIn('id="reprocess-project-invert"', text)
        self.assertIn('id="reprocess-date-from" type="date"', text)
        self.assertIn('name="reprocess-visibility" value="hidden"', text)
        self.assertIn('id="reprocess-source-list"', text)
        self.assertIn('id="reprocess-allow-current"', text)
        self.assertNotIn('name="reprocess-component"', text)
        self.assertIn('apiRequest("/api/reprocess/plan"', text)
        self.assertIn("plan.protected_user_edits", text)
        self.assertIn("工作量：分类 ${plan.work_items.analysis} 条", text)
        self.assertIn("正文将发送到当前云端服务，并可能产生费用", text)
        self.assertIn("当前仅完成计算，尚未执行", text)
        self.assertIn("session.capabilities?.reprocess_plan === true", text)
        self.assertIn('id="reprocess-confirmation" hidden', text)
        self.assertIn("我已核对上方范围，确认执行这一次重处理", text)
        self.assertIn("所选历史正文将发送到当前云端服务，并可能产生费用", text)
        self.assertIn('apiRequest("/api/reprocess"', text)
        self.assertIn("currentReprocessPlan.plan_id", text)
        self.assertIn("!reprocessExecuteEnabled || !plan.has_work", text)
        self.assertIn("invalidateReprocessPlan()", text)
        self.assertIn('window.fetch("/api/reprocess/options"', text)
        self.assertIn("estimated_input_tokens", text)
        self.assertIn("不含固定提示词、schema 和输出 tokens", text)
        self.assertIn("applyReprocessProjectSearch()", text)
        self.assertIn("applyReprocessSourceSearch()", text)
        self.assertIn("previewVersion !== reprocessPreviewVersion", text)
        self.assertIn('modelSettingsDialog.addEventListener("close", invalidateReprocessPlan)', text)
        self.assertIn("session.capabilities?.reprocess_execute === true", text)
        self.assertIn("if (modelSettingsDirty)", text)
        self.assertIn("请先保存上方模型设置", text)
        self.assertIn("配置需要修正：", text)

    def test_embeds_data_and_language(self) -> None:
        result = build_html([entry()], CATEGORIES, "en", 4, MARKDOWN_IT, TEMPLATE)

        self.assertIn('const language = "en";', result)
        self.assertIn("const collapsedLines = 4;", result)
        self.assertIn('"category": "work"', result)
        self.assertIn("可离线只读使用", result)
        self.assertIn(MARKDOWN_IT, result)
        self.assertNotIn("交互原型", result)

    def test_escapes_script_terminator_in_note_text(self) -> None:
        dangerous_text = "before </script><script>alert(1)</script> after"

        result = build_html([entry(dangerous_text)], CATEGORIES, "zh-CN", 4, MARKDOWN_IT, TEMPLATE)

        self.assertNotIn(dangerous_text, result)
        self.assertIn("\\u003c/script\\u003e", result)

    def test_empty_entries_generate_valid_page(self) -> None:
        result = build_html([], CATEGORIES, "zh-CN", 4, MARKDOWN_IT, TEMPLATE)

        self.assertIn("const entries = [];", result)

    def test_rejects_unknown_category(self) -> None:
        invalid = entry()
        invalid["category"] = "missing"

        with self.assertRaisesRegex(ValueError, "category"):
            validate_entries([invalid], {"work", "other"})

    def test_validates_hidden_projection_fields(self) -> None:
        hidden = entry()
        hidden["hidden"] = True
        hidden["hidden_at"] = "2026-08-26T10:00:00+08:00"

        self.assertEqual(validate_entries([hidden], {"work", "other"}), [hidden])

        invalid = entry()
        invalid["hidden"] = "yes"
        with self.assertRaisesRegex(ValueError, "hidden 必须是布尔值"):
            validate_entries([invalid], {"work", "other"})

        stale = entry()
        stale["hidden_at"] = "2026-08-26T10:00:00+08:00"
        with self.assertRaisesRegex(ValueError, "未隐藏时不能包含 hidden_at"):
            validate_entries([stale], {"work", "other"})

    def test_javascript_json_escapes_html_characters(self) -> None:
        self.assertEqual(javascript_json("<&>"), '"\\u003c\\u0026\\u003e"')

    def test_rejects_invalid_collapsed_lines(self) -> None:
        with self.assertRaisesRegex(ValueError, "collapsed_lines"):
            build_html([], CATEGORIES, "zh-CN", 0, MARKDOWN_IT, TEMPLATE)

    def test_invalid_display_cache_is_removed_from_page_copy(self) -> None:
        source = entry()
        source["display_markdown"] = "修改后的内容"
        source["display_text_sha256"] = "wrong-hash"

        prepared = prepare_entries_for_display([source])

        self.assertNotIn("display_markdown", prepared[0])
        self.assertIn("display_markdown", source)

    def test_valid_display_cache_is_preserved(self) -> None:
        import hashlib

        source = entry("标题\n正文")
        source["display_markdown"] = "# 标题\n**正文**"
        source["display_text_sha256"] = hashlib.sha256(
            source["text"].encode("utf-8")
        ).hexdigest()

        prepared = prepare_entries_for_display([source])

        self.assertEqual(prepared[0]["display_markdown"], "# 标题\n**正文**")

    def test_table_candidates_without_coordinates_are_page_fallbacks(self) -> None:
        source = entry("A\tB\n1\t2")

        prepared = prepare_entries_for_display([source])

        self.assertEqual(prepared[0]["_display_tables"], [])
        self.assertEqual(prepared[0]["_display_table_fallbacks"][0]["line_ids"], [1, 2])

    def test_valid_table_coordinates_are_embedded_without_cells(self) -> None:
        source = entry("A\tB\n1\t2")
        source["display_tables"] = [{
            "candidate_id": 1,
            "line_ids": [1, 2],
            "header_line_id": 1,
            "column_count": 2,
        }]

        prepared = prepare_entries_for_display([source])

        self.assertEqual(len(prepared[0]["_display_tables"]), 1)
        self.assertNotIn("cells", prepared[0]["_display_tables"][0])


if __name__ == "__main__":
    unittest.main()