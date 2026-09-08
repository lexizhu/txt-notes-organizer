"""Run the manual organize-and-build workflow in one command."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


PROJECT_DIR = Path(__file__).resolve().parent
REVIEW_HTML_PATH = PROJECT_DIR / "output" / "review.html"


def cancellation_requested() -> bool:
	path = os.environ.get("MY_NOTES_CANCEL_FILE")
	return bool(path) and Path(path).is_file()


def log(message: str, level: str = "INFO") -> None:
	now = datetime.now().astimezone().isoformat(timespec="seconds")
	print(f"[{now}] [{level}] {message}", flush=True)


def run_review_pipeline(
	runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
	python_executable: str = sys.executable,
) -> bool:
	"""Run organize first, and only build HTML when it succeeds."""
	steps = [
		("扫描最新笔记", ["watcher.py", "--once"]),
		("整理笔记", ["organize.py"]),
		("生成 HTML", ["build_html.py"]),
	]
	started_at = time.monotonic()
	for index, (label, script_arguments) in enumerate(steps, start=1):
		if cancellation_requested():
			log("已取消整理，不再开始下一步骤。", "WARNING")
			return False
		log(f"步骤 {index}/{len(steps)}：开始{label}。")
		result = runner(
			[python_executable, str(PROJECT_DIR / script_arguments[0]), *script_arguments[1:]],
			cwd=PROJECT_DIR,
			text=True,
		)
		if result.returncode != 0:
			if result.returncode == 130 or cancellation_requested():
				log("整理已安全取消；已完成结果会保留，未生成新页面。", "WARNING")
				return False
			log(f"{label}失败（退出码 {result.returncode}），后续步骤已停止。", "ERROR")
			return False
		log(f"步骤 {index}/{len(steps)}：{label}完成。")
		if cancellation_requested():
			log("整理已安全取消；不再开始下一步骤。", "WARNING")
			return False

	elapsed = time.monotonic() - started_at
	log(f"日常整理流程完成，总耗时 {elapsed:.1f} 秒。")
	log(f"查看页面：{REVIEW_HTML_PATH}")
	return True


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="整理新笔记并生成离线回顾页面。")
	parser.add_argument(
		"--open",
		action="store_true",
		dest="open_page",
		help="成功后使用 macOS 默认浏览器打开 review.html。",
	)
	return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
	args = parse_args(arguments)
	log("一键整理程序已启动。")
	if not run_review_pipeline():
		raise SystemExit(1)
	if args.open_page:
		log("正在打开回顾页面。")
		result = subprocess.run(["open", str(REVIEW_HTML_PATH)], text=True)
		if result.returncode != 0:
			log("无法自动打开页面，请手动双击 output/review.html。", "WARNING")


if __name__ == "__main__":
	main()