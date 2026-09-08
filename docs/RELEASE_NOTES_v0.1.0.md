# TXT Notes Organizer v0.1.0

> Future release draft / 未来版本草稿 — Updated / 更新于: 2026-09-08. This file is preparation material for a possible `v0.1.0` release, not an announcement of a published version. The current sharing plan is source code from `main` via Code → Download ZIP or Git clone; a tag, GitHub Release, and custom ZIP are optional and deferred. 本文件用于准备未来可能发布的 `v0.1.0`，不是已发布版本的公告。当前计划通过 `main` 分支的 Code → Download ZIP 或 Git clone 分享源码；标签、GitHub Release 和自制 ZIP 均为可选项，暂不办理。

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
2. On the [repository page](https://github.com/lexizhu/txt-notes-organizer), select `main` and choose **Code → Download ZIP**, or clone the repository. Extract the whole folder and keep its files and subdirectories together. No tagged release is required.
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

- Code baseline `ff8ef4a` fixes a test cleanup race by waiting for background workers to finish diagnostics and final writes before removing temporary directories.
- On Python 3.14.2, all 223 unit tests passed locally. Twenty repeated regression rounds (180 test executions) also passed without leftover job workers; test temporary files were cleaned.
- GitHub Actions Tests run #3 for `ff8ef4a` succeeded. The workflow tests the same code on macOS with Python 3.10, 3.12, and 3.14, with `fail-fast: false` so each version can finish independently.
- The unchanged offline demo was previously checked in a browser: nine records displayed, project filtering worked, and read-only controls were verified.
- The earlier custom ZIP was built from `00fe22c`, before the cleanup fix. It is not the current code and must not be offered as the current download. Use `main` instead; producing another custom ZIP is not required for source sharing.
- These results apply to the named code baseline, not to every future revision or real-model end-to-end behavior. This documentation update does not constitute a new CI result or a published Release.

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
2. 在[仓库页面](https://github.com/lexizhu/txt-notes-organizer)选择 `main`，点击 **Code → Download ZIP**，或 clone 仓库。完整解压并保留目录结构，不需要等待带标签的版本发布。
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

- 代码基线 `ff8ef4a` 修复了测试清理竞态：删除临时目录前，等待后台线程完成诊断写入及最终收尾。
- 在 Python 3.14.2 下通过全部 223 项本地单元测试；20 轮重复回归、共 180 次测试也通过，未残留后台任务线程，测试临时文件已清理。
- `ff8ef4a` 对应的 GitHub Actions Tests 第 3 次运行成功。工作流在 macOS 上分别使用 Python 3.10、3.12、3.14 测试同一份代码，并设置 `fail-fast: false`，让各版本独立完成。
- 未改动的离线演示页此前已在浏览器中验证：显示 9 条记录、项目筛选正常、只读控制生效。
- 较早制作的自定义 ZIP 基于修复前的 `00fe22c`，不是当前代码，不能作为当前下载包提供。请通过 `main` 获取源码；公开源码不要求重新制作自定义 ZIP。
- 上述结果对应指定代码基线，不代表所有未来修改或真实模型的完整端到端流程均已验证。本次文档更新也不代表新的 CI 结果或已经创建 Release。

### 许可证与反馈

采用 MIT 许可证，详见 [LICENSE](../LICENSE)；内置 Markdown 渲染器保留其[第三方许可证](../vendor/markdown-it.LICENSE)。

问题和建议可提交到 [GitHub Issues](https://github.com/lexizhu/txt-notes-organizer/issues)，请只使用虚构示例。安全问题请遵循 [SECURITY.md](../SECURITY.md)，不要在公开 issue 中放入私人数据。

## Sharing scope / 分享范围

Before changing repository visibility, inspect source files, commit history and identities, and GitHub-hosted material such as Actions logs and attachments. Repository-local Git credentials and files outside the repository are not uploaded by a normal Git push. Obtain explicit approval before making the repository public; later making it private cannot recall copies already downloaded. Verify that GitHub Private Vulnerability Reporting is enabled for the public repository, as described in the security policy.

改变可见性前，应检查源码、历史提交与作者信息，以及 GitHub 上的 Actions 日志和附件等内容。仓库本地的 Git 凭据配置和仓库外文件不会随正常 Git push 上传。公开前需要明确确认；之后改回私有也无法收回已经下载的副本。按安全政策核对公开仓库是否启用 GitHub 私密漏洞报告功能。

If a tagged release is prepared later, select and verify its exact commit, update these notes and their links, and remove the draft banner from the actual Release body. Custom ZIP attachments and checksums are optional; GitHub provides source archives for tags.

以后需要带标签的版本时，再选定并验证准确提交，更新说明和链接，从实际 Release 正文中移除草稿提示。自定义 ZIP 附件及校验文件是可选项；GitHub 会为标签提供源码压缩包。