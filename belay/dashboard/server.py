"""
The live dashboard server.

It follows the detector's `GET /alerts` stream and `GET /health` server-side
(the detector sends no CORS headers), tails the swarm's event log, watches the
certifier's results directory, and re-serves all of it to the page as one
server-sent event stream at `GET /events`.

Run: uv run belay-dashboard --detector-url http://127.0.0.1:8700 \
        --swarm-log runs/swarm --certifier-dir runs/certifier
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import AsyncIterator, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from belay.contract import key_from_env

from .state import PROFILE, Alarm, DirWatch, Hub, Tail, load_policy

PAGE = Path(__file__).with_name("index.html")


async def _read_alerts(hub: Hub, url: str) -> None:
    timeout = httpx.Timeout(5.0, read=45.0)  # the detector sends a keepalive every 15 s
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("GET", url.rstrip("/") + "/alerts",
                                 headers={"Accept": "text/event-stream"}) as response:
            response.raise_for_status()
            hub.set_detector(stream="live", error=None, since=time.time())
            data_lines: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif line == "" and data_lines:
                    payload, data_lines = "\n".join(data_lines), []
                    try:
                        record = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    # The stream carries alert records and, tagged with kind
                    # "summary", one summary record per request.
                    if record.get("kind") == "summary":
                        hub.ingest_summary(record, "stream", time.time())
                    else:
                        hub.ingest_alert(record, "stream", time.time())


async def follow_alerts(hub: Hub, url: str, resubscribe: asyncio.Event, retry_max: float = 5.0) -> None:
    """Subscribe to the detector's alert stream; reconnect whenever it drops.

    `resubscribe` is set when the detector's health came back after being
    down: a detector that restarted can leave the old process holding the old
    stream open, so the stream alone cannot be trusted to notice.
    """
    backoff = 0.5
    while True:
        resubscribe.clear()
        reader = asyncio.create_task(_read_alerts(hub, url))
        kick = asyncio.create_task(resubscribe.wait())
        try:
            done, _ = await asyncio.wait({reader, kick}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            kick.cancel()
        if reader in done:
            exc = reader.exception()
            error = f"{type(exc).__name__}: {exc}"[:200] if exc else "stream closed by detector"
        else:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            error, backoff = "detector restarted; resubscribing", 0.0
        hub.set_detector(stream="reconnecting", error=error)
        await asyncio.sleep(backoff)
        backoff = min(max(backoff * 2, 0.5), retry_max)


async def poll_health(hub: Hub, url: str, resubscribe: asyncio.Event, every: float = 2.0) -> None:
    was_down = False
    async with httpx.AsyncClient(timeout=3.0) as client:
        while True:
            try:
                r = await client.get(url.rstrip("/") + "/health")
                r.raise_for_status()
                body = r.json()
                hub.set_detector(health="ok" if body.get("ok") else "not ok", identity=body.get("identity"))
                if was_down:
                    resubscribe.set()
                was_down = False
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                hub.set_detector(health="down", error=f"health: {type(e).__name__}"[:200])
                was_down = True
            await asyncio.sleep(every)


async def tail_swarm(hub: Hub, path: str, every: float = 0.15) -> None:
    tail = Tail(path)
    hub.set_swarm(path=path, status="waiting")
    while True:
        try:
            file, events, switched = tail.poll()
            if file is None:
                hub.set_swarm(status="waiting", file=None)
            else:
                if switched:
                    hub.set_swarm(status="following", file=str(file), error=None)
                for event in events:
                    hub.ingest_event(event)
                if events:
                    hub.set_swarm(events=hub.swarm["events"])
        except Exception as e:  # noqa: BLE001
            hub.set_swarm(status="error", error=f"{type(e).__name__}: {e}"[:200])
        await asyncio.sleep(every)


async def watch_certifier(hub: Hub, path: str, every: float = 1.0) -> None:
    watch = DirWatch(path)
    while True:
        try:
            changed, removed = watch.poll()
            for name, data, mtime in changed:
                hub.set_certifier(name, data, mtime)
            for name in removed:
                hub.drop_certifier(name)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(every)


def create_app(detector_url: Optional[str], swarm_log: Optional[str], certifier_dir: Optional[str],
               key: Optional[bytes] = None, hub: Optional[Hub] = None) -> FastAPI:
    hub = hub or Hub(key or key_from_env())
    hub.detector["url"] = detector_url
    hub.swarm["path"] = swarm_log
    queues: set[asyncio.Queue] = set()

    def forward(kind: str, data: dict) -> None:
        message = f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"
        for q in list(queues):
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                pass

    hub.listeners.append(forward)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        tasks = []
        if detector_url:
            resubscribe = app.state.resubscribe = asyncio.Event()
            tasks += [asyncio.create_task(follow_alerts(hub, detector_url, resubscribe)),
                      asyncio.create_task(poll_health(hub, detector_url, resubscribe))]
        else:
            hub.set_detector(stream="off", health="off")
        if swarm_log:
            tasks.append(asyncio.create_task(tail_swarm(hub, swarm_log)))
        else:
            hub.set_swarm(status="off")
        if certifier_dir:
            tasks.append(asyncio.create_task(watch_certifier(hub, certifier_dir)))
        yield
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Belay dashboard", lifespan=lifespan)
    app.state.hub = hub

    @app.get("/", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        return HTMLResponse(PAGE.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})

    @app.get("/state")
    def state() -> JSONResponse:
        return JSONResponse(json.loads(json.dumps(hub.snapshot(), default=str)))

    @app.get("/events")
    async def events(request: Request) -> StreamingResponse:
        q: asyncio.Queue = asyncio.Queue(maxsize=5000)
        queues.add(q)

        async def stream() -> AsyncIterator[str]:
            try:
                yield "retry: 1000\n\n"
                yield f"event: snapshot\ndata: {json.dumps(hub.snapshot(), default=str)}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        yield await asyncio.wait_for(q.get(), timeout=10)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                queues.discard(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app


def main() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="Belay live dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8702)
    p.add_argument("--detector-url", default="http://127.0.0.1:8700",
                   help="detector (or stub) base URL; '' to disable")
    p.add_argument("--swarm-log", default="runs/swarm",
                   help="swarm event log: a .jsonl file, or a directory whose newest .jsonl is followed")
    p.add_argument("--certifier-dir", default="runs/certifier", help="directory of certifier results JSON")
    p.add_argument("--profile", default=str(PROFILE),
                   help="report profile; its .policy.json beside it sets each concept's mode (sustained or spike)")
    p.add_argument("--window", type=int, default=5, help="requests in the rolling window (sustained mode)")
    p.add_argument("--sustained-threshold", type=float, default=0.15,
                   help="alarm when the window's mean request score is at or above this (sustained mode)")
    p.add_argument("--rise", type=float, default=0.02,
                   help="a request is hot when its mean is this far above the swarm's normal (median)")
    p.add_argument("--min-hot", type=int, default=4,
                   help="alarm when this many of the window's requests are hot: a rising trend")
    args = p.parse_args()
    alarm = Alarm(window=args.window, threshold=args.sustained_threshold, rise=args.rise,
                  min_hot=min(args.min_hot, args.window))
    hub = Hub(key_from_env(), policy=load_policy(args.profile), alarm=alarm)
    app = create_app(args.detector_url or None, args.swarm_log or None, args.certifier_dir or None, hub=hub)
    print(f"Belay dashboard on http://{args.host}:{args.port}/")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
