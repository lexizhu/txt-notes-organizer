"""Uninstall the per-user macOS LaunchAgent for the note watcher."""

from __future__ import annotations

import subprocess

from install_watcher import LABEL, PLIST_PATH, launch_domain


def uninstall() -> None:
	domain = launch_domain()
	service = f"{domain}/{LABEL}"
	result = subprocess.run(
		["launchctl", "bootout", service],
		capture_output=True,
		text=True,
	)
	if result.returncode != 0 and "could not find service" not in result.stderr.lower():
		print(f"停止服务时收到提示：{result.stderr.strip()}", flush=True)
	PLIST_PATH.unlink(missing_ok=True)


def main() -> None:
	print(f"准备卸载后台监听服务：{LABEL}", flush=True)
	try:
		uninstall()
	except OSError as error:
		print(f"卸载失败：{error}", flush=True)
		raise SystemExit(1) from error
	print("卸载完成。笔记、JSON 和日志文件均未删除。", flush=True)


if __name__ == "__main__":
	main()