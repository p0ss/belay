"""
Bringing a detector up for a setting: launching one from a command template
(the watch setting is a startup flag), or serving an app in-process (the stub,
for dry runs and tests).
"""

from __future__ import annotations

import os
import shlex
import signal
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import httpx

DEFAULT_COMMAND = "uv run belay-detector --host 127.0.0.1 --port {port} --watch {setting}"


def wait_healthy(url: str, timeout_s: float, proc: Optional[subprocess.Popen] = None) -> dict:
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            raise RuntimeError(f"detector exited with code {proc.returncode} before it was healthy")
        try:
            r = httpx.get(url.rstrip("/") + "/health", timeout=5)
            if r.status_code == 200 and r.json().get("ok"):
                return r.json()
            last = f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            last = f"{type(e).__name__}"
        time.sleep(0.5)
    raise TimeoutError(f"detector at {url} not healthy after {timeout_s:.0f}s ({last})")


@contextmanager
def launched(command: str, setting: str, host: str, port: int, log_path: Path,
             startup_timeout_s: float) -> Iterator[str]:
    """Start the detector from `command` (formatted with setting, port, host), yield its URL, stop it."""
    argv = shlex.split(command.format(setting=setting, port=port, host=host))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"http://{host}:{port}"
    with log_path.open("ab") as log:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            wait_healthy(url, startup_timeout_s, proc)
            yield url
        finally:
            _stop(proc)


def _stop(proc: subprocess.Popen, grace_s: float = 30.0) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(grace_s)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


@contextmanager
def serve_in_thread(app, host: str = "127.0.0.1") -> Iterator[str]:
    """Serve an ASGI app on a free port in a background thread; yield its URL."""
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    # "critical": at shutdown uvicorn cancels the open /alerts stream (its generator
    # blocks up to 15 s between keepalives) and would print the cancellation.
    config = uvicorn.Config(app, log_level="critical", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    url = f"http://{host}:{port}"
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("in-process detector did not start")
            time.sleep(0.02)
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
