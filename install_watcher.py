"""Install the note watcher as a per-user macOS LaunchAgent."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


LABEL = "com.local.mynotestool.watcher"
PROJECT_DIR = Path(__file__).resolve().parent
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_launch_agent(
	project_dir: Path = PROJECT_DIR,
	python_executable: str = sys.executable,
) -> dict[str, Any]:
	"""Return a launchd configuration pointing to this portable project."""
	resolved_project = project_dir.resolve()
	return {
		"Label": LABEL,
		"ProgramArguments": [
			str(Path(python_executable).resolve()),
			str(resolved_project / "watcher.py"),
		],
		"WorkingDirectory": str(resolved_project),
		"RunAtLoad": True,
		"KeepAlive": True,
		"ProcessType": "Background",
		# watcher.py owns watcher.log rotation; launchd must not append around it.
		"StandardOutPath": "/dev/null",
		"StandardErrorPath": str(resolved_project / "data" / "watcher-error.log"),
	}


def write_plist(path: Path, configuration: dict[str, Any]) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary_path = path.with_suffix(".tmp")
	with temporary_path.open("wb") as file:
		plistlib.dump(configuration, file, sort_keys=False)
	temporary_path.replace(path)


def launch_domain(user_id: int | None = None) -> str:
	return f"gui/{os.getuid() if user_id is None else user_id}"


def install(
	plist_path: Path = PLIST_PATH,
	runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
	configuration = build_launch_agent()
	(PROJECT_DIR / "data").mkdir(parents=True, exist_ok=True)
	write_plist(plist_path, configuration)
	domain = launch_domain()
	service = f"{domain}/{LABEL}"
	runner(["launchctl", "bootout", service], capture_output=True, text=True)
	result = runner(
		["launchctl", "bootstrap", domain, str(plist_path)],
		capture_output=True,
		text=True,
	)
	if result.returncode != 0:
		raise RuntimeError(result.stderr.strip() or "launchctl bootstrap 失败")
	result = runner(
		["launchctl", "kickstart", "-k", service],
		capture_output=True,
		text=True,
	)
	if result.returncode != 0:
		raise RuntimeError(result.stderr.strip() or "launchctl kickstart 失败")


def main() -> None:
	print("准备安装笔记后台监听服务。", flush=True)
	print(f"LaunchAgent 配置：{PLIST_PATH}", flush=True)
	print(f"项目目录：{PROJECT_DIR}", flush=True)
	try:
		install()
	except (OSError, RuntimeError) as error:
		print(f"安装失败：{error}", flush=True)
		raise SystemExit(1) from error
	print("安装完成：watcher 已注册并启动。", flush=True)
	print(f"轮转日志：{PROJECT_DIR / 'data' / 'watcher.log'}", flush=True)
	print(f"错误日志：{PROJECT_DIR / 'data' / 'watcher-error.log'}", flush=True)


if __name__ == "__main__":
	main()