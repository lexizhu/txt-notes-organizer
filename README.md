# TXT Notes Organizer

> **中文说明见本文末尾。** A concise Chinese guide is available at the end of this README.

A local-first macOS note organizer that watches plain-text files, enriches complete notes with a local AI model, and builds a self-contained offline review page.

The repository includes fictional demo notes so you can try lists, dates, statuses, formatting, and tables without using personal data.

This project was built with a human-directed, AI-assisted development process: the product direction, safety boundaries, tests, and iterative reviews were led by a human, with AI assisting implementation and documentation. Treat it as a practical starting point rather than a universal workflow. You are welcome to adapt the code, prompts, categories, UI, and processing rules to fit your own needs.

For architecture, data flow, safety boundaries, and design trade-offs, see [System Design](docs/SYSTEM_DESIGN.md).

## Release Status

- Distribution: source code from the `main` branch; no tagged GitHub Release is currently published
- `v0.1.0` is a future version target, not a required download or installation step
- Supported and tested platform: macOS
- CI matrix targets: Python 3.10, 3.12, and 3.14
- Windows and Linux are not currently supported or tested
- This is an early public release; review the privacy boundaries and keep backups of source TXT files

## Highlights

- Write notes in ordinary UTF-8 `.txt` files.
- Capture record time in the background without changing the source notes.
- Use local Ollama by default for classification, status, event time, formatting, and table detection.
- Optionally use a standard OpenAI-compatible Chat Completions API.
- Review everything in one generated HTML file that works offline.
- Filter by project, category, status, and time.
- Hide, restore, and permanently remove generated records through the local interface.
- Preview and confirm one-time reprocessing before historical notes are sent to a model.
- Safely cancel organization after the current model request returns, without publishing a partial review page.
- Run with the Python standard library only. No Python package installation is required.

## Requirements

- macOS
- Python 3.10 or later
- [Ollama](https://ollama.com/) for the recommended local workflow
- The configured Ollama model; the default is `qwen3:4b`

Check your environment:

```bash
python3 --version
ollama --version
ollama list
```

Download the default model if needed:

```bash
ollama pull qwen3:4b
```

## Quick Start

On the [repository page](https://github.com/lexizhu/txt-notes-organizer), select the `main` branch and choose **Code → Download ZIP**, or clone the repository. Extract the entire folder before running the tool. This downloads the current branch snapshot, not a fixed tagged release; a separate GitHub Release is not required. This is a Python source distribution, not a standalone macOS app. Python is required for the full application; Ollama and its model must be installed separately for the recommended local workflow.

If you only want to preview the interface, open `output/demo-review.html`. This pre-generated page needs no Python, Ollama, or local service.

To run the complete application:

1. Download or clone this repository, extract it if needed, and keep the whole folder together.
2. Install Python 3.10 or later. Open Terminal in the project folder and verify:

  ```bash
  python3 --version
  ```

3. Install [Ollama for macOS](https://ollama.com/download/mac), then open the Ollama application. Verify that its command is available:

  ```bash
  ollama --version
  ```

4. Check whether the default model is already installed:

  ```bash
  ollama list
  ```

  If `qwen3:4b` is not listed, download it once:

  ```bash
  ollama pull qwen3:4b
  ```

  The model is managed by Ollama and is not included in this repository.

5. In Finder, double-click `Open Notes Tool.command`. The launcher starts the loopback service and opens `http://localhost:8765/`.
6. In the browser, click **Organize**.
7. Keep Ollama running and wait for the progress panel to finish. The page refreshes automatically after a successful run.

On the first launch, macOS may block a downloaded command. In Finder, Control-click `Open Notes Tool.command`, choose **Open**, and confirm once. If Terminal reports a permission error, run:

```bash
cd "/path/to/my-notes-tool-public"
chmod +x "Open Notes Tool.command" "打开笔记工具.command"
./"Open Notes Tool.command"
```

You can also launch from Terminal:

```bash
python3 launch_tool.py
```

The first run processes nine fictional records from the four files in `notes/`. With the default `qwen3:4b` model, an individual classification may take tens of seconds to several minutes depending on the Mac and current system load. Processing all demo records can therefore take several minutes. The progress panel shows the current record, elapsed time, and safe cancellation controls.

To inspect the finished interface immediately without starting Ollama or waiting for model processing, open `output/demo-review.html` directly.

If the page opens but organization cannot connect to Ollama, confirm that the Ollama application is running and check its local API:

```bash
curl --silent http://localhost:11434/api/tags
```

If the configured model is missing, run `ollama pull qwen3:4b` and try **Organize** again.

## Demo Notes

The bundled files are fictional and safe for screenshots or walkthroughs:

- `notes/product-demo.txt`: bullets, nested bullets, future tasks, and a completed release note.
- `notes/meeting-notes.txt`: decisions, risks, numbered actions, and a past meeting summary.
- `notes/learning-plan.txt`: headings, numbered learning steps, nested topics, and relative dates.
- `notes/table-examples.txt`: regular multi-column tables and an intentionally irregular fallback example.

Open `output/demo-review.html` to inspect a pre-generated fictional result without running a model. `output/review-mock.html` is the build template and intentionally includes six fictional mock records for interface previews. During a build, these mock records are replaced in the generated page with the target data; they are not imported into the user's records. Never put real personal or company information in the template. `output/review.html` is the user's generated review page and ships with an empty data set.

Delete these files before adding personal notes if you do not want demo records. To start with completely empty generated data, stop the app and replace the four files in `data/` with these values:

```text
data/captured_entries.jsonl   empty file
data/user_actions.jsonl       empty file
data/entries.json              []
data/watcher_state.json        {"files": {}}
```

Then run:

```bash
python3 build_html.py
```

## Writing Notes

Each `.txt` filename becomes a project name. For example:

```text
notes/
├── personal-planning.txt
├── reading-list.txt
└── work-notes.txt
```

End each record with **three blank lines**. One or two blank lines may be used inside a record.

```text
Prepare the fictional demo by next Friday.

- Capture a desktop screenshot
- Capture a mobile screenshot



This starts the next record.



```

The watcher ignores an unfinished tail until the full separator is present.

## Daily Use

- Add or edit complete records in `notes/*.txt`.
- Click **Organize** to scan changes, process only new or changed records, and rebuild the page.
- Use project tabs and filters to review results.
- Use **Hidden** to restore records or remove generated copies.

Switching models does not automatically reprocess old notes. Model Settings displays the effective local model, while the reorganization section separately names the target model and treats original model sources only as a scope filter. **Reorganize existing notes** provides two explicit modes:

- **Full reorganization** selects records by multiple projects, inclusive record-date range, visibility, and original analysis-model source. It reruns analysis and formatting with the current model, and calls table recognition only for records with table candidates.
- **Repair degraded results** reruns only missing or degraded formatting and table stages. This saves tokens but may intentionally leave different stages of one note associated with different models.

The current model source remains visible but is disabled by default to avoid redundant work. It can be explicitly enabled when correcting an earlier result. Model sources are grouped by provider and model/deployment; API keys, endpoints, timeouts, and prompt or formatting protocol revisions do not create a new source or imply that existing satisfactory results need reprocessing.

Before confirmation, the app shows record counts, calls by stage, actual table-candidate calls, characters to be sent, and a broad local input-token estimate. The estimate covers note content and table-candidate data only; it excludes fixed prompts, schemas, and output tokens. It is for comparison only and does not represent provider billing. User-edited classification, status, and event-time fields remain protected.

While organization or historical reprocessing is running, the progress panel provides **Cancel organization**. Cancellation is cooperative: the current model request is allowed to return, then its unfinished result is discarded before the next record or stage starts. Previously completed records remain saved, and no new review page is published. The panel estimates the maximum remaining wait from the active model timeout and updates the elapsed time. Cancellation is unavailable once final page rebuilding begins.

During daily organization, classification failures prevent publication of an incomplete page. Formatting or table-recognition failures fall back to the original text and still rebuild the page. A formatting fallback is cached so the next daily run does not repeat the same model request.

Open **Diagnostics** to review the five most recent organization tasks and copy a sanitized summary. Diagnostic reports persist locally across service restarts and exclude note text, credentials, model responses, URLs, and full paths.

## Model Settings

### Local Ollama

Local Ollama is the default and recommended option. Note content stays on the machine running Ollama.

The default model settings are in `config/settings.json`:

```json
{
  "ollama": {
    "base_url": "http://localhost:11434",
    "model": "qwen3:4b",
    "timeout_seconds": 300
  }
}
```

### Standard OpenAI-Compatible API

Open **Model Settings**, choose **Cloud API**, and provide:

- **Base URL**: required, for example `https://api.example.com/v1`
- **Model**: required, using the exact model identifier from your provider
- **Timeout**: optional; the default is 120 seconds
- **CA Certificate File**: optional; normally leave blank
- **API Key**: required on first setup and entered only in the local interface

The app uses `POST {base_url}/chat/completions` with `Authorization: Bearer <key>`.

Cloud use sends selected note content to the configured provider and may incur charges. The interface requires explicit privacy and cost confirmation. Connection testing sends fixed fictional content, never your notes.

## Data and Privacy

The source of truth remains local:

- `notes/*.txt`: original notes; the app never edits them.
- `data/captured_entries.jsonl`: append-only capture events.
- `data/user_actions.jsonl`: append-only hide and restore events.
- `data/entries.json`: derived structured records and validated caches.
- `data/watcher_state.json`: watcher snapshots and unfinished tails.
- `data/diagnostics.json`: the five most recent sanitized organization reports.
- `output/review.html`: generated self-contained review page.

Model settings and API keys are stored outside the repository under:

```text
~/Library/Application Support/My Notes Tool Public/
```

The directory is created with mode `0700`; files use mode `0600`. API keys are never returned by the local API, embedded in HTML, included in backend fingerprints, or written to logs.

Do not commit personal notes, generated data, logs, API keys, certificate files, or screenshots containing private information.

## Offline and Local Service Modes

- `output/review.html`: offline read-only review.
- `http://localhost:8765/`: full local operation mode.

The service binds only to `127.0.0.1`, uses a random startup token, validates Host and Origin, and does not enable CORS.

Closing the browser window does not stop the local HTTP service. To stop the service currently listening on port `8765`:

```bash
service_pid="$(lsof -nP -t -iTCP:8765 -sTCP:LISTEN | head -n 1)"
if [ -n "$service_pid" ]; then
  kill "$service_pid"
fi
```

This stops only the HTTP service. A watcher installed as a LaunchAgent continues running until it is uninstalled:

```bash
python3 uninstall_watcher.py
```

Uninstalling the watcher does not delete notes or generated data. Run `python3 install_watcher.py` to enable it again.

## Background Watcher

Install the per-user macOS LaunchAgent:

```bash
python3 install_watcher.py
```

Uninstall it without deleting notes or data:

```bash
python3 uninstall_watcher.py
```

The watcher scans every 300 seconds by default. Configuration is in `config/settings.json`.

The clean repository intentionally contains no watcher or local-service logs. After the user starts the watcher or launcher, new local logs are created under `data/` as needed. Log files are ignored by Git and should not be published.

## Permanent Removal Boundary

Permanent removal deletes a selected record from the app's current generated data and accessible logs. It does **not** edit the source TXT file. Remove the same content from its source file, or it can be captured again.

It also cannot erase copies retained by cloud sync history, backups, filesystem snapshots, browser caches, or storage hardware. Do not use a real-data working copy to prepare public demo material; build releases from fictional data only.

## Moving the Project Directory

An older copy of the local service may continue to own port `8765` after the project is moved. The launcher identifies the project instance and will not reuse a service started from another directory. If it reports another instance, close that old service and launch `Open Notes Tool.command` from the new directory. Re-run `python3 install_watcher.py` after a move so the macOS LaunchAgent points to the new path.

## Known Limitations

- The app currently targets macOS; Windows and Linux behavior has not been implemented or tested.
- The watcher polls every 300 seconds by default, so background capture is not instantaneous. Clicking **Organize** performs an immediate scan first.
- The app never edits source TXT files. Changes such as permanent removal must also be made manually in the source file when appropriate.
- Only one full local service can use port `8765` at a time. Multiple copies may be viewed offline, but they cannot all run the default service simultaneously without selecting different ports.
- Model quality and speed depend on the selected model and hardware. Outputs are locally validated, but classification quality can still vary.
- If the first non-empty line is a standalone valid `MMDD` value such as `0903`, event time uses that date in the capture year and timezone. This prevents future plans mentioned in meeting minutes from replacing the meeting date.
- Cancellation is cooperative. An in-flight model or network request is allowed to return before processing stops; it is not forcefully terminated.
- The generated review page embeds its data. Treat `output/review.html` as potentially private after processing personal notes.

## Development

Run the full test suite:

```bash
python3 -m unittest discover -s tests -v
```

Build the offline page:

```bash
python3 build_html.py
```

Rebuild the deterministic fictional demo page:

```bash
python3 build_demo.py
```

Run the complete scan, organize, and build pipeline:

```bash
python3 review.py
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines, [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for community standards, and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Feedback and Customization

Comments are welcome:

- Open a GitHub Issue for reproducible bugs or focused feature requests.
- Comment on an existing Issue or Pull Request when discussing that topic.
- Repository maintainers may enable GitHub Discussions for general questions, ideas, and community examples.

Please use fictional data and redact private notes, local paths, credentials, and provider details before posting. Pull Requests that adapt the tool for broader workflows are welcome when they preserve clear privacy boundaries and include tests.

## License

MIT. See [LICENSE](LICENSE).

---

## 中文简要说明

TXT Notes Organizer 是一个以本地优先为原则的 macOS 纯文本笔记整理工具。你可以继续使用普通 `.txt` 文件写笔记，后台 watcher 会在一条记录以连续三个空白行结束后捕获它，再由本地 Ollama 整理分类、状态、事件时间、排版和表格结构，最后生成一个可离线打开的 `output/review.html`。

本项目采用人类主导、AI 协助的方式完成：产品方向、安全边界和验收由人主导，AI 协助实现、测试和文档。它不是只能照原样使用的固定产品，欢迎根据自己的笔记习惯修改分类、prompt、界面和处理规则。

当前通过 `main` 分支分享源码，尚未发布带标签的 GitHub Release。`v0.1.0` 是未来版本目标，不是下载或安装的前提。只支持并测试了 macOS；Windows 和 Linux 尚未实现或验证。建议始终保留原始 TXT 的备份。

### 快速开始

在[仓库页面](https://github.com/lexizhu/txt-notes-organizer)选择 `main` 分支，点击 **Code → Download ZIP**，或 clone 仓库。运行前请完整解压。下载的是当前分支快照，而不是固定标签版本；不需要等待单独的 GitHub Release。本工具以 Python 源码形式提供，不是独立的 macOS 安装程序。完整应用需要 Python；推荐的本地处理方式还需另外安装 Ollama 和模型。

如果只想预览界面，直接打开 `output/demo-review.html`。这个预生成页面不需要 Python、Ollama 或本地服务。

完整运行步骤：

1. 下载或 clone 整个仓库；如果下载的是压缩包，先完整解压，不要只复制其中某个启动文件。
2. 安装 Python 3.10 或更高版本，在项目目录打开终端并确认：

  ```bash
  python3 --version
  ```

3. 安装 [Ollama for macOS](https://ollama.com/download/mac)，然后打开 Ollama 应用，并确认：

  ```bash
  ollama --version
  ```

4. 检查默认模型是否已经安装：

  ```bash
  ollama list
  ```

  如果列表里没有 `qwen3:4b`，需要下载一次：

  ```bash
  ollama pull qwen3:4b
  ```

  模型由 Ollama 单独管理，不包含在本仓库或发布包中。

5. 在 Finder 中双击 `Open Notes Tool.command` 或 `打开笔记工具.command`。启动器会启动仅限本机访问的服务，并打开 `http://localhost:8765/`。
6. 在浏览器中点击“整理”。
7. 保持 Ollama 运行，等待进度面板完成；成功后页面会自动刷新。

macOS 第一次打开下载的 `.command` 时可能阻止运行。请在 Finder 中按住 Control 点击该文件，选择“打开”，再确认一次。如果终端提示没有执行权限，请运行：

```bash
cd "/你的路径/my-notes-tool-public"
chmod +x "Open Notes Tool.command" "打开笔记工具.command"
./"打开笔记工具.command"
```

首次整理会处理 4 个文件中的 9 条虚构记录。默认 `qwen3:4b` 在不同 Mac 上每条可能需要几十秒到几分钟，全部处理通常需要数分钟。若只想立即查看成品，无需启动模型，直接打开 `output/demo-review.html`。

如果页面能够打开，但整理时无法连接 Ollama，请确认 Ollama 应用仍在运行，并在终端执行：

```bash
curl --silent http://localhost:11434/api/tags
```

如果提示缺少模型，执行 `ollama pull qwen3:4b` 后再次点击“整理”。

仓库内的 4 个 TXT 文件均为虚构 demo，共 9 条记录，覆盖项目符号、编号列表、日期、待办、完成状态和表格。正式使用前可以删除这些 demo 文件，再添加自己的 TXT。

无需运行模型也可以直接打开 `output/demo-review.html` 查看预生成的虚构整理结果。`output/review-mock.html` 是构建模板，有意保留了 6 条虚构 mock 记录用于界面展示。构建时，生成页面中的 mock 记录会被目标数据替换，不会导入用户记录。模板中不得放入真实个人或公司信息。`output/review.html` 是用户运行后生成的页面，发布时内嵌数据为空。

本地 Ollama 是默认且推荐的方式。公开版也支持标准 OpenAI-compatible Chat Completions API；启用云端后，所选笔记正文会发送到你配置的服务商，并可能产生费用。API Key 只应在本机页面输入，不要写入仓库、聊天、截图或日志。

整理或历史重处理运行时可以点击“取消整理”。程序不会强杀当前模型请求，而会在它自然返回后、写入该次结果前停止，并保留之前已经完成的记录。页面会根据当前模型 timeout 显示预计最长等待时间；进入最终页面重建后不再允许取消。

原始 TXT 始终由用户管理，本工具不会自动修改。隐藏不等于删除；“彻底删除”也只清理工具当前可访问的生成数据和日志，不会清理原始 TXT、云同步历史、备份或系统快照。

干净发布目录默认不包含 watcher 或本地服务日志。用户第一次启动 watcher/应用后，程序会按需在 `data/` 下生成新的本机日志；这些日志已被 Git 忽略，不应上传公开仓库。

关闭浏览器窗口不会停止后台 HTTP 服务。需要退出时，可以在终端查找并结束占用 `8765` 的进程；若安装了后台 watcher，还需要运行 `python3 uninstall_watcher.py`。卸载 watcher 不会删除笔记或生成数据。

### 已知限制

- 当前仅支持 macOS。
- watcher 默认每 300 秒轮询一次；点击“整理”会先立即扫描。
- 工具从不修改原始 TXT，彻底删除后仍需按需要手动清理源文件。
- 默认端口 `8765` 同时只能由一份完整服务占用。
- 模型速度和分类质量依赖硬件与模型本身。
- 取消是协作式取消，当前模型或网络请求返回后才停止，不会强制中断。
- `review.html` 会内嵌整理后的数据，处理私人笔记后应将它视为私人文件。

详细架构与特色设计请阅读 [System Design](docs/SYSTEM_DESIGN.md)，其中末尾也附有中文摘要。

欢迎通过 GitHub Issues 提交 bug 或功能建议，也可以在已有 Issue/Pull Request 下评论。一般性交流需要仓库维护者开启 GitHub Discussions。公开内容请只使用虚构数据，并先移除笔记正文、路径、账号和凭据。
