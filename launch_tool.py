"""Start the local note service when needed, then open it in a browser."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Protocol, Sequence


HOST = "127.0.0.1"
BROWSER_HOST = "localhost"
DEFAULT_PORT = 8765
PROJECT_DIR = Path(__file__).resolve().parent
INSTANCE_ID = hashlib.sha256(str(PROJECT_DIR).encode("utf-8")).hexdigest()
SERVER_SCRIPT_PATH = PROJECT_DIR / "local_server.py"
SERVER_LOG_PATH = PROJECT_DIR / "data" / "local-server.log"


class ServiceProcess(Protocol):
	def poll(self) -> int | None: ...


def service_is_ready(url: str, timeout_seconds: float = 0.5) -> bool:
	"""Return whether the expected local service answers its health endpoint."""
	request = urllib.request.Request(f"{url}api/health", method="GET")
	try:
		with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
			if response.status != 200:
				return False
			payload = json.loads(response.read())
	except (OSError, urllib.error.URLError, json.JSONDecodeError):
		return False
	return payload == {"status": "ok", "instance_id": INSTANCE_ID}


def service_responds(url: str, timeout_seconds: float = 0.5) -> bool:
	"""Return whether any note-tool-like service already owns this address."""
	request = urllib.request.Request(f"{url}api/health", method="GET")
	try:
		with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
			payload = json.loads(response.read())
	except (OSError, urllib.error.URLError, json.JSONDecodeError):
		return False
	return response.status == 200 and payload.get("status") == "ok"


def start_local_service(
	port: int,
	python_executable: str = sys.executable,
	log_path: Path = SERVER_LOG_PATH,
) -> subprocess.Popen[bytes]:
	"""Start the one fixed local service as a detached background process."""
	log_path.parent.mkdir(parents=True, exist_ok=True)
	log_file = log_path.open("ab", buffering=0)
	try:
		return subprocess.Popen(
			[
				python_executable,
				str(SERVER_SCRIPT_PATH),
				"--port",
				str(port),
			],
			cwd=PROJECT_DIR,
			stdin=subprocess.DEVNULL,
			stdout=log_file,
			stderr=subprocess.STDOUT,
			start_new_session=True,
		)
	finally:
		log_file.close()


def launch_tool(
	port: int = DEFAULT_PORT,
	startup_timeout_seconds: float = 10.0,
	ready_checker: Callable[[str], bool] = service_is_ready,
	responds_checker: Callable[[str], bool] | None = None,
	starter: Callable[[int], ServiceProcess] = start_local_service,
	browser_opener: Callable[[str], bool] = webbrowser.open,
	sleep: Callable[[float], None] = time.sleep,
	monotonic: Callable[[], float] = time.monotonic,
) -> bool:
	"""Reuse or start the service, wait until ready, and open its page."""
	service_url = f"http://{HOST}:{port}/"
	browser_url = f"http://{BROWSER_HOST}:{port}/"
	started = False
	process: ServiceProcess | None = None
	if responds_checker is None:
		responds_checker = service_responds if ready_checker is service_is_ready else lambda url: False
	if not ready_checker(service_url):
		if responds_checker(service_url):
			raise RuntimeError(
				"端口上正在运行另一份笔记工具实例。请关闭旧实例后重新打开。"
			)
		process = starter(port)
		started = True
		deadline = monotonic() + startup_timeout_seconds
		while monotonic() < deadline:
			if ready_checker(service_url):
				break
			if process.poll() is not None:
				raise RuntimeError("本地服务启动失败，请查看 data/local-server.log。")
			sleep(0.1)
		else:
			raise RuntimeError("等待本地服务启动超时，请查看 data/local-server.log。")
	if not browser_opener(browser_url):
		raise RuntimeError(f"无法自动打开浏览器，请手动访问 {browser_url}")
	return started


def parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="启动并打开本地笔记工具。")
	parser.add_argument("--port", type=int, default=DEFAULT_PORT)
	return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> None:
	args = parse_args(arguments)
	if not 1 <= args.port <= 65535:
		raise SystemExit("端口必须在 1 到 65535 之间")
	try:
		started = launch_tool(port=args.port)
	except (OSError, RuntimeError) as error:
		print(f"无法打开笔记工具：{error}", file=sys.stderr, flush=True)
		raise SystemExit(1) from error
	message = "本地服务已启动，浏览器已打开。" if started else "本地服务已在运行，浏览器已打开。"
	print(message, flush=True)


if __name__ == "__main__":
	main()