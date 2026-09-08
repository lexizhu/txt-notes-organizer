# TXT Notes Organizer v0.1.0

> Draft / 草稿 — Planned release date / 计划发布日期: 2026-09-08. The tag, public release, and downloadable release artifact have not yet been created or verified. 标签、公开发布和正式下载包尚未创建或完成验证。

## English

### Overview

TXT Notes Organizer is a local-first macOS tool for capturing, organizing, and reviewing plain-text notes. Keep writing in ordinary TXT files, use AI-assisted classification and formatting, and review the results in a self-contained offline HTML page. This is an early release, not a standalone macOS application bundle.

### Features

- Capture complete records separated by three blank lines, without modifying the original TXT files.
- Use local Ollama by default to identify categories, statuses, event times, formatting, and table structures.
- Optionally connect to a standard OpenAI-compatible Chat Completions API after explicit privacy and cost confirmation.
- Review notes offline with project, category, and status filters and time sorting.
- Hide and restore records, or permanently remove generated copies through the local service. Original TXT content is not deleted.
- Preview one-time historical reprocessing and safely cancel processing after the current model request returns.
- Review recent sanitized task diagnostics in local service mode.
- Try four fictional TXT files containing nine records, a pre-generated demo page, and six separate interface-preview mock records in the build template.

### Requirements and getting started

1. Use macOS with Python 3.10 or later. The Python application uses only the standard library; no Python package installation is required.
2. Download the source archive for `v0.1.0` from [GitHub Releases](https://github.com/lexizhu/txt-notes-organizer/releases) once published, and extract the whole folder. Keep its files and subdirectories together.
3. For an immediate preview, open `output/demo-review.html`. This requires no Python, Ollama, or local server.
4. For the recommended local workflow, install [Ollama](https://ollama.com/download/mac) and download `qwen3:4b` using `ollama pull qwen3:4b`. Python, Ollama, and model weights are not included in the archive.
5. Keep Ollama running, double-click `Open Notes Tool.command`, and click **整理 (Organize)** in the browser. Alternatively, run `python3 launch_tool.py` from the extracted project folder.

See the [README](../README.md) for macOS first-launch guidance, note separators, cloud configuration, and troubleshooting. The current interface is primarily Chinese.

### Privacy and limitations

- Local Ollama is the default. Enabling cloud processing sends selected note content to the configured provider and may incur charges; review that provider's retention and privacy terms.
- Model settings and API keys are stored outside the project under `~/Library/Application Support/My Notes Tool Public/`. Enter keys only in the local settings interface, never in issues, screenshots, or source files.
- The local HTTP service binds to `127.0.0.1`. Offline pages are read-only; mutations require the local service.
- Generated HTML embeds note data, including any retained hidden records. Hiding a record is not redaction. Do not publish pages generated from private notes or commit runtime data back to GitHub.
- Permanent removal does not erase original TXT files, backups, cloud history, browser caches, or filesystem snapshots.
- Windows and Linux are not supported or tested. Background capture polls every 300 seconds by default; clicking Organize scans immediately.
- AI accuracy and processing time depend on the model and hardware. Cancellation waits for an in-flight request to return rather than forcibly stopping it.
- The service uses port 8765 by default; an existing instance may prevent another copy from starting. Closing the browser does not stop the service.

### Verification status

- Baseline commit `521a863`: 217 local unit tests passed on Python 3.14.2; its GitHub Actions Tests workflow also completed successfully.
- The baseline offline demo displayed nine records, project filtering worked, and read-only controls were verified in a browser.
- On 2026-09-08, a temporary 66-file candidate ZIP including the release documentation was extracted and passed all 217 unit tests on Python 3.14.2 in about 32 seconds. Archive integrity, extracted file contents, launcher executable permissions, empty runtime data, and fictional demo records were checked.
- Candidate testing left the source project unchanged; the temporary ZIP, extracted copy, and test directories were removed. This verification summary was added afterward. A final archive built from the release commit, its checksum, and CI for that commit remain pending.
- Live model end-to-end validation is not claimed by these unit-test results.

### License and feedback

MIT; see [LICENSE](../LICENSE). The bundled Markdown renderer retains its [third-party license](../vendor/markdown-it.LICENSE).

Use [GitHub Issues](https://github.com/lexizhu/txt-notes-organizer/issues) for bugs and suggestions with fictional examples only. For security issues, follow [SECURITY.md](../SECURITY.md); do not post private data in a public issue.

## 中文

### 简介

TXT Notes Organizer 是一个本地优先的 macOS 纯文本笔记整理工具。继续使用普通 TXT 文件记录，让 AI 辅助分类和排版，再通过独立 HTML 页面离线回顾。这是早期版本，以源码形式提供，不是独立的 macOS 应用安装包。

### 主要功能

- 以连续三个空白行区分完整记录，捕获过程不修改原始 TXT。
- 默认使用本地 Ollama 识别分类、状态、事件时间、排版和表格结构。
- 可选标准 OpenAI-compatible Chat Completions API，启用前需确认隐私与费用。
- 离线按项目、分类、状态筛选，并按时间排序回顾。
- 在本地服务中隐藏、恢复记录或彻底删除生成副本；不删除原始 TXT 内容。
- 预览一次性历史重处理范围，并在当前模型请求返回后安全取消处理。
- 在本地服务模式中查看近期脱敏任务诊断。
- 包含 4 份虚构 TXT、共 9 条演示记录和预生成演示页；构建模板另保留 6 条虚构 mock 记录用于界面展示。

### 环境要求与快速开始

1. 使用 macOS 和 Python 3.10 或更高版本。Python 程序只依赖标准库，无需安装额外 Python 包。
2. 正式发布后，在 [GitHub Releases](https://github.com/lexizhu/txt-notes-organizer/releases) 下载 `v0.1.0` 的源码压缩包，完整解压并保留目录结构。
3. 只想预览时，直接打开 `output/demo-review.html`，不需要 Python、Ollama 或本地服务。
4. 推荐本地使用：安装 [Ollama](https://ollama.com/download/mac)，并通过 `ollama pull qwen3:4b` 下载默认模型。压缩包不包含 Python、Ollama 或模型权重。
5. 保持 Ollama 运行，双击 `Open Notes Tool.command` 或 `打开笔记工具.command`，在浏览器中点击“整理”。也可以在解压后的项目目录执行 `python3 launch_tool.py`。

macOS 首次运行提示、记录分隔规则、云端配置与故障排查详见 [README](../README.md)。当前界面以中文为主。

### 隐私边界与已知限制

- 默认本地 Ollama；启用云端会将选中的笔记内容发送给配置的服务商，并可能产生费用。请先了解服务商的数据保留与隐私政策。
- 模型配置与 API Key 存在项目外的 `~/Library/Application Support/My Notes Tool Public/`。密钥只在本地设置界面输入，不要放入 issue、截图或源码。
- HTTP 服务仅监听 `127.0.0.1`。离线页面只读，修改操作需要本地服务。
- 生成的 HTML 内嵌笔记数据，也可能包含保留的隐藏记录；隐藏不等于脱敏。不要公开由私人笔记生成的页面，也不要将运行数据提交回 GitHub。
- 彻底删除不会清理原始 TXT、备份、云端历史、浏览器缓存或文件系统快照。
- Windows 和 Linux 尚未支持或测试。后台默认每 300 秒扫描一次；点击“整理”会立即扫描。
- AI 准确度和处理速度取决于模型与设备。取消需要等待当前请求返回，不会强制终止该请求。
- 默认端口为 8765，已有实例可能阻止另一份副本启动；关闭浏览器不会停止服务。

### 验证状态

- 基线提交 `521a863`：在 Python 3.14.2 下通过 217 项本地单元测试；对应 GitHub Actions Tests 工作流也已成功完成。
- 基线离线演示页已在浏览器中验证：显示 9 条记录、项目筛选正常、只读控制生效。
- 2026-09-08，包含发布文档的 66 文件临时候选 ZIP 解压后，在 Python 3.14.2 下通过全部 217 项单元测试，耗时约 32 秒。已核对压缩包完整性、解压文件内容、启动脚本可执行权限、空运行数据和虚构演示记录。
- 候选验证没有改写原项目；临时 ZIP、解压副本和测试目录均已清理。本验证摘要在测试后补充。从发布提交构建的正式包、其校验值以及该提交的 CI 结果仍待验证。
- 上述单元测试结果不代表已经验证真实模型的完整端到端使用流程。

### 许可证与反馈

采用 MIT 许可证，详见 [LICENSE](../LICENSE)；内置 Markdown 渲染器保留其[第三方许可证](../vendor/markdown-it.LICENSE)。

问题和建议可提交到 [GitHub Issues](https://github.com/lexizhu/txt-notes-organizer/issues)，请只使用虚构示例。安全问题请遵循 [SECURITY.md](../SECURITY.md)，不要在公开 issue 中放入私人数据。

## Maintainer publication gate / 维护者发布前确认

This section is a preparation checklist, not evidence of completed publication. Update the verification paragraphs with final results, resolve documentation links for the GitHub Release body, and remove this checklist and the draft banner from the published release text only after the applicable checks pass.

本节是准备清单，不表示发布已经完成。完成适用检查后，更新上方验证结果，将 GitHub Release 正文中的文档链接改为可访问的版本链接，再从正式发布正文中移除本清单和草稿提示。

- [ ] Confirm the actual release date and final commit. / 确认实际发布日期和最终提交。
- [ ] Run tests against the final candidate and verify CI. / 测试最终候选版本并核对 CI。
- [ ] Build, extract, inspect, and test the release archive; record its checksum. / 构建、解压、检查并测试发布包，记录校验值。
- [ ] Inspect public-facing CI logs and enable GitHub Private Vulnerability Reporting when available for the public repository. / 检查将公开的 CI 日志，并在公开仓库中启用 GitHub 私密漏洞报告功能。
- [ ] Obtain explicit approval before making the repository public or publishing the tag and Release. / 公开仓库、发布标签及 Release 前取得明确确认。