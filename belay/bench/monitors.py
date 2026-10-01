"""
Background monitors for a benchmark run: peak GPU memory from nvidia-smi, and
the detector's alert stream (GET /alerts), so the time an alert takes to
appear can be measured against the token that raised it.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

import httpx

from belay.contract import AlertRecord, verify


class GpuPoller:
    """
    Polls `nvidia-smi --query-gpu=memory.used` in a background thread and keeps
    the peak since the last `reset()`. Absent (available=False, values None)
    when nvidia-smi is missing or fails.
    """

    def __init__(self, interval_s: float = 0.1, gpu_index: Optional[int] = None,
                 command: Optional[Sequence[str]] = None, enabled: bool = True):
        self.interval_s = interval_s
        if command is None:
            command = ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"]
            if gpu_index is not None:
                command += ["-i", str(gpu_index)]
        self.command = list(command)
        self.available = enabled and shutil.which(self.command[0]) is not None and self.sample() is not None
        self._peak: Optional[float] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def sample(self) -> Optional[float]:
        """Memory used in MiB, summed over the GPUs queried, or None."""
        try:
            out = subprocess.run(self.command, capture_output=True, text=True, timeout=5, check=True).stdout
            values = [float(line.strip()) for line in out.splitlines() if line.strip()]
            return sum(values) if values else None
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def start(self) -> "GpuPoller":
        if self.available and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="gpu-poller", daemon=True)
            self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.is_set():
            value = self.sample()
            if value is not None:
                with self._lock:
                    self._peak = value if self._peak is None else max(self._peak, value)
            self._stop.wait(self.interval_s)

    def reset(self) -> None:
        with self._lock:
            self._peak = None

    def peak(self) -> Optional[float]:
        if not self.available:
            return None
        # Take one more sample so a short cell still has a value.
        value = self.sample()
        with self._lock:
            if value is not None:
                self._peak = value if self._peak is None else max(self._peak, value)
            return self._peak

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


Key = Tuple[str, str, int]  # request_id, concept, token_index


class AlertWatcher:
    """
    Follows GET /alerts and records, for each signed AlertRecord, when it
    arrived (client time.time()) and whether its signature verifies.
    """

    def __init__(self, url: str, key: bytes):
        self.url = url.rstrip("/") + "/alerts"
        self.key = key
        self.connected = threading.Event()
        self.arrivals: Dict[Key, List[Tuple[float, bool]]] = {}
        self.records = 0
        self.bad_signatures = 0
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._response: Optional[httpx.Response] = None
        self._client = httpx.Client(timeout=httpx.Timeout(10.0, read=None))
        self._thread = threading.Thread(target=self._run, name="alert-watcher", daemon=True)

    def start(self, wait_s: float = 10.0) -> "AlertWatcher":
        self._thread.start()
        if not self.connected.wait(wait_s):
            self.error = self.error or "alert stream did not connect"
        return self

    def _run(self) -> None:
        try:
            with self._client.stream("GET", self.url) as response:
                self._response = response
                response.raise_for_status()
                for line in response.iter_lines():
                    now = time.time()
                    if self._stop.is_set():
                        return
                    if line.startswith(":"):
                        self.connected.set()
                        continue
                    if not line.startswith("data:"):
                        continue
                    self.connected.set()
                    self._record(json.loads(line[5:].strip()), now)
        except Exception as e:  # noqa: BLE001 - the stream ends however the server goes away
            if not self._stop.is_set():
                self.error = f"{type(e).__name__}: {e}"
        finally:
            self.connected.set()

    def _record(self, payload: dict, now: float) -> None:
        try:
            record = AlertRecord.model_validate(payload)
            ok = verify(record, self.key)
            key = (record.request_id, record.alert.concept, record.alert.token_index)
        except Exception:  # noqa: BLE001 - a malformed record is a failed signature
            ok, key = False, (str(payload.get("request_id")), "?", -1)
        with self._lock:
            self.records += 1
            self.bad_signatures += 0 if ok else 1
            self.arrivals.setdefault(key, []).append((now, ok))

    def lookup(self, key: Key) -> Optional[Tuple[float, bool]]:
        with self._lock:
            hits = self.arrivals.get(key)
            return hits[0] if hits else None

    def wait_for(self, keys: List[Key], timeout_s: float) -> int:
        """Wait until every key has arrived or the timeout passes; return how many are missing."""
        deadline = time.monotonic() + timeout_s
        while True:
            missing = sum(1 for k in keys if self.lookup(k) is None)
            if missing == 0 or time.monotonic() >= deadline or not self._thread.is_alive():
                return missing
            time.sleep(0.02)

    def stop(self) -> None:
        self._stop.set()
        try:
            # Shut the socket down: that unblocks the reader thread and ends the
            # server's stream too (closing the response from here would not).
            if self._response is not None:
                stream = self._response.extensions.get("network_stream")
                sock = stream.get_extra_info("socket") if stream is not None else None
                if sock is not None:
                    sock.shutdown(socket.SHUT_RDWR)
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
        self._thread.join(timeout=2)
