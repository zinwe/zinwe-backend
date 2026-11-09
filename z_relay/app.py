"""
The Zinwe file relay (v1.5 round 2): raw ASGI, one process, memory only. Nothing is ever written to disk and the
database is never touched: Django signs tickets after its own access checks (z_chat.files), and this app checks only
their signature, age and scope (z_relay.tickets).

    Sender (Windows service / owner's browser)        Relay                                    Receiver
      GET  /relay/wait?t=<serve>  (held <= 25 s)  <-  job  <-  GET /relay/file/<id>?t=<get>  (Range ok)
      PUT  /relay/push/<job>?t=<serve>  (bytes from offset)  ->  piped with backpressure  ->  saved to disk

Endpoints (all under /relay/):
- GET  wait?t=<serve ticket>          200 {"job", "file_id", "offset"} or 204 after WAIT_SECONDS with no job.
- GET  file/<file_id>?t=<get ticket>[&inline=1]
                                      200 / 206 (Range: bytes=N- or bytes=N-M) streamed from the sender. When the sender
                                      doesn't start pushing within SENDER_TIMEOUT: 503 (JSON, or a short HTML page for
                                      a browser download). 416 for a range past the end.
- GET  status/<file_id>?t=<get ticket>  {"online": bool}: the holder had a wait open in the last ONLINE_SECONDS.
- PUT  push/<job>?t=<serve ticket>    streams the body to the waiting receiver. 200 {"ok": true, "sent": n} when the
                                      receiver has it all; 410 when the receiver went away (stop sending); 404 when
                                      the job is no longer waiting; 409 when another push already took it.
- GET  health                         {"ok": true}

Memory: each transfer holds at most BUFFER_BYTES (64 MB) in memory, and all transfers together about TOTAL_BYTES
(512 MB; each transfer may always hold one chunk); past that the relay stops reading push bodies, so TCP pushes back
on the senders. Settings (env): ZINWE_RELAY_WAIT_SECONDS (25), ZINWE_RELAY_SENDER_TIMEOUT (20),
ZINWE_RELAY_ONLINE_SECONDS (40), ZINWE_RELAY_BUFFER_MB (64), ZINWE_RELAY_TOTAL_MB (512), ZINWE_RELAY_MAX_JOBS (2000).

Run: `uvicorn z_relay.app:app --workers 1 --host 127.0.0.1 --port <port>` with DJANGO_SETTINGS_MODULE set (for
SECRET_KEY only). One process only: pairing state lives in this process's memory.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from collections import deque
from html import escape
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from . import tickets  # noqa: E402 -- after the settings module is known

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]

BAD_TICKET = "This link has expired. Ask for a new one."
OFFLINE = "{name}'s PC is offline. The download starts when it's back."
NOT_WAITING = "That download isn't waiting any more."
RECEIVER_GONE = "The receiver has gone. Stop sending."
SHORT_BODY = "The file ended before all its bytes were sent."
BUSY = "The relay is busy. Try again in a minute."

_FILE_ID = re.compile(r"^[0-9a-fA-F-]{32,36}$")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")
_RANGE = re.compile(r"^bytes=(\d+)-(\d*)$")

CORS = [
    (b"access-control-allow-origin", b"*"),
    (b"access-control-expose-headers", b"Content-Range, Content-Length, Content-Disposition, Accept-Ranges"),
]


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


class Job:
    """One download: a receiver waiting for (or getting) bytes `offset`..`offset + length - 1` of `file_id`."""

    def __init__(self, holder: str, file_id: str, offset: int, length: int) -> None:
        self.id = uuid.uuid4().hex
        self.holder = holder
        self.file_id = file_id
        self.offset = offset
        self.length = length
        self.started = asyncio.Event()  # a push took it
        self.data_ready = asyncio.Event()  # a chunk arrived, or the push ended / failed
        self.room = asyncio.Event()  # the receiver drained some of the buffer
        self.delivered = asyncio.Event()  # the receiver's response is finished (or it went away)
        self.chunks: deque[bytes] = deque()
        self.buffered = 0
        self.peak = 0  # the most this job ever held in memory (tests check the cap)
        self.max_chunk = 0
        self.taken = False
        self.ended = False  # every byte was pushed
        self.failed = False  # the sender stopped early
        self.receiver_gone = False

    def as_json(self) -> dict[str, Any]:
        return {"job": self.id, "file_id": self.file_id, "offset": self.offset}

    def wake(self) -> None:
        self.data_ready.set()
        self.room.set()


class Relay:
    def __init__(self, *, wait_seconds: float | None = None, sender_timeout: float | None = None,
                 online_seconds: float | None = None, buffer_bytes: int | None = None,
                 total_bytes: int | None = None, max_jobs: int | None = None) -> None:
        self.wait_seconds = wait_seconds if wait_seconds is not None else _env_float("ZINWE_RELAY_WAIT_SECONDS", 25)
        self.sender_timeout = (sender_timeout if sender_timeout is not None
                               else _env_float("ZINWE_RELAY_SENDER_TIMEOUT", 20))
        self.online_seconds = (online_seconds if online_seconds is not None
                               else _env_float("ZINWE_RELAY_ONLINE_SECONDS", 40))
        self.buffer_bytes = (buffer_bytes if buffer_bytes is not None
                             else int(_env_float("ZINWE_RELAY_BUFFER_MB", 64) * 1024 * 1024))
        self.total_bytes = (total_bytes if total_bytes is not None
                            else int(_env_float("ZINWE_RELAY_TOTAL_MB", 512) * 1024 * 1024))
        self.total_buffered = 0  # bytes held by every transfer together
        self.blocked: set[Job] = set()  # pushes waiting for memory
        self.max_jobs = max_jobs if max_jobs is not None else int(_env_float("ZINWE_RELAY_MAX_JOBS", 2000))
        self.waiters: dict[str, deque[asyncio.Future[Job]]] = {}  # holder -> open waits, oldest first
        self.queued: dict[str, deque[Job]] = {}  # holder -> jobs no wait was open for yet
        self.jobs: dict[str, Job] = {}
        self.open_waits: dict[str, int] = {}
        self.seen: dict[str, float] = {}  # holder -> monotonic time of its last wait

    # --- ASGI -----------------------------------------------------------------------------------------------------

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        method = scope.get("method", "GET")
        parts = scope.get("path", "").strip("/").split("/")
        if not parts or parts[0] != "relay":
            await _json(send, 404, {"detail": "Not found."})
            return
        parts = parts[1:]
        if method == "OPTIONS":
            await _preflight(send)
            return
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        ticket = (query.get("t") or [""])[0]
        route = parts[0] if parts else ""
        if route == "health" and len(parts) == 1 and method in ("GET", "HEAD"):
            await _json(send, 200, {"ok": True})
        elif route == "wait" and len(parts) == 1 and method == "GET":
            await self.wait(ticket, receive, send)
        elif route == "file" and len(parts) == 2 and method == "GET":
            await self.file(parts[1], ticket, (query.get("inline") or [""])[0] == "1", _headers(scope), receive, send)
        elif route == "status" and len(parts) == 2 and method == "GET":
            await self.status(parts[1], ticket, send)
        elif route == "push" and len(parts) == 2 and method == "PUT":
            await self.push(parts[1], ticket, receive, send)
        elif route in ("health", "wait", "file", "status", "push"):
            await _json(send, 405, {"detail": "Method not allowed."})
        else:
            await _json(send, 404, {"detail": "Not found."})

    @staticmethod
    async def _lifespan(receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return

    # --- Pairing ----------------------------------------------------------------------------------------------------

    def online(self, holder: str) -> bool:
        if self.open_waits.get(holder, 0) > 0:
            return True
        last = self.seen.get(holder)
        return last is not None and time.monotonic() - last <= self.online_seconds

    def _dispatch(self, job: Job) -> None:
        """Hand the job to the holder's oldest open wait, or queue it for its next one."""
        waits = self.waiters.get(job.holder)
        while waits:
            fut = waits.popleft()
            if not fut.done():
                fut.set_result(job)
                return
        self.queued.setdefault(job.holder, deque()).append(job)

    def _next_queued(self, holder: str) -> Job | None:
        queue = self.queued.get(holder)
        while queue:
            job = queue.popleft()
            if job.id in self.jobs and not job.taken and not job.receiver_gone:
                return job
        self.queued.pop(holder, None)
        return None

    def _drained(self, job: Job) -> None:
        """Memory was freed: this push may read on, and so may any push held back by the total cap."""
        job.room.set()
        for other in list(self.blocked):
            other.room.set()

    def _forget(self, job: Job) -> None:
        self.jobs.pop(job.id, None)
        queue = self.queued.get(job.holder)
        if queue is not None:
            try:
                queue.remove(job)
            except ValueError:
                pass
            if not queue:
                self.queued.pop(job.holder, None)

    # --- Sender: wait -----------------------------------------------------------------------------------------------

    async def wait(self, ticket: str, receive: Receive, send: Send) -> None:
        data = tickets.read(ticket, tickets.SERVE)
        if data is None:
            await _json(send, 403, {"detail": BAD_TICKET})
            return
        holder = data["h"]
        self.seen[holder] = time.monotonic()
        job = self._next_queued(holder)
        if job is not None:
            await _json(send, 200, job.as_json())
            return
        fut: asyncio.Future[Job] = asyncio.get_running_loop().create_future()
        self.waiters.setdefault(holder, deque()).append(fut)
        self.open_waits[holder] = self.open_waits.get(holder, 0) + 1
        gone = asyncio.ensure_future(_disconnected(receive))
        try:
            await asyncio.wait({fut, gone}, timeout=self.wait_seconds, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self.open_waits[holder] -= 1
            if self.open_waits[holder] <= 0:
                self.open_waits.pop(holder, None)
            self.seen[holder] = time.monotonic()
            waits = self.waiters.get(holder)
            if waits is not None:
                try:
                    waits.remove(fut)
                except ValueError:
                    pass
                if not waits:
                    self.waiters.pop(holder, None)
            disconnected = gone.done()
            gone.cancel()
        if fut.done() and not fut.cancelled():
            job = fut.result()
            if disconnected:  # the job reached a wait that had just closed: give it to the next one
                self._dispatch(job)
                return
            await _json(send, 200, job.as_json())
            return
        fut.cancel()
        if not disconnected:
            await _empty(send, 204)

    # --- Receiver: file + status ------------------------------------------------------------------------------------

    async def status(self, file_id: str, ticket: str, send: Send) -> None:
        data = tickets.read(ticket, tickets.GET)
        if data is None or data["f"] != file_id:
            await _json(send, 403, {"detail": BAD_TICKET})
            return
        await _json(send, 200, {"online": self.online(data["h"])})

    async def file(self, file_id: str, ticket: str, inline: bool, headers: dict[str, str], receive: Receive,
                   send: Send) -> None:
        data = tickets.read(ticket, tickets.GET)
        if data is None or data["f"] != file_id or not _FILE_ID.match(file_id):
            await _json(send, 403, {"detail": BAD_TICKET})
            return
        size: int = data["s"]
        start, end, partial = 0, size - 1, False
        wanted = _RANGE.match(headers.get("range", "").replace(" ", ""))
        if wanted:
            start = int(wanted.group(1))
            if wanted.group(2):
                end = min(int(wanted.group(2)), size - 1)
            if start >= size or start > end:
                await _empty(send, 416, [(b"content-range", f"bytes */{size}".encode())])
                return
            partial = True
        length = max(0, end - start + 1)
        inline_type = data.get("i") or ""
        show_inline = inline and bool(inline_type)
        out = [
            (b"content-type", (inline_type if show_inline else "application/octet-stream").encode("latin-1")),
            (b"content-disposition",
             f"{'inline' if show_inline else 'attachment'}; filename*=UTF-8''{quote(data.get('n') or 'file', safe='')}"
             .encode("latin-1")),
            (b"content-length", str(length).encode()),
            (b"accept-ranges", b"bytes"),
            (b"x-content-type-options", b"nosniff"),
            (b"cache-control", b"private, no-store"),
            *CORS,
        ]
        if partial:
            out.append((b"content-range", f"bytes {start}-{end}/{size}".encode()))
        status = 206 if partial else 200
        if length == 0:
            await send({"type": "http.response.start", "status": status, "headers": out})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        if len(self.jobs) >= self.max_jobs:
            await _json(send, 503, {"detail": BUSY})
            return

        job = Job(data["h"], file_id, start, length)
        self.jobs[job.id] = job
        self._dispatch(job)
        gone = asyncio.ensure_future(_disconnected(receive))
        gone.add_done_callback(lambda _t: _receiver_left(job))
        try:
            started = asyncio.ensure_future(job.started.wait())
            await asyncio.wait({started, gone}, timeout=self.sender_timeout, return_when=asyncio.FIRST_COMPLETED)
            started.cancel()
            if not job.started.is_set():
                self._forget(job)
                if not job.receiver_gone:
                    await _offline(send, data.get("fn") or "", headers.get("accept", ""))
                return
            await send({"type": "http.response.start", "status": status, "headers": out})
            while True:
                if job.chunks:
                    chunk = job.chunks.popleft()
                    job.buffered -= len(chunk)
                    self.total_buffered -= len(chunk)
                    self._drained(job)
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                elif job.ended:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
                    return
                elif job.failed or job.receiver_gone:
                    return  # no final message: the server closes the connection, the client sees a cut download
                else:
                    job.data_ready.clear()
                    await job.data_ready.wait()
        finally:
            gone.cancel()
            job.chunks.clear()
            self.total_buffered -= job.buffered
            job.buffered = 0
            job.delivered.set()
            self._drained(job)
            self._forget(job)

    # --- Sender: push -----------------------------------------------------------------------------------------------

    async def push(self, job_id: str, ticket: str, receive: Receive, send: Send) -> None:
        data = tickets.read(ticket, tickets.SERVE)
        if data is None:
            await _json(send, 403, {"detail": BAD_TICKET})
            return
        job = self.jobs.get(job_id) if _JOB_ID.match(job_id) else None
        if job is None or job.holder != data["h"]:
            await _json(send, 404, {"detail": NOT_WAITING})
            return
        if job.receiver_gone:
            await _json(send, 410, {"detail": RECEIVER_GONE})
            return
        if job.taken:
            await _json(send, 409, {"detail": "This download is already being sent."})
            return
        job.taken = True
        job.started.set()
        remaining = job.length
        while remaining > 0 and not job.receiver_gone:
            # Backpressure: stop reading the body while this transfer (or all of them) hold their share of memory.
            while job.buffered > 0 and not job.receiver_gone and (
                    job.buffered + job.max_chunk > self.buffer_bytes
                    or self.total_buffered + job.max_chunk > self.total_bytes):
                job.room.clear()
                self.blocked.add(job)
                try:
                    await job.room.wait()
                finally:
                    self.blocked.discard(job)
            if job.receiver_gone:
                break
            message = await receive()
            if message["type"] == "http.disconnect":
                job.failed = True
                job.wake()
                return
            body = message.get("body", b"")
            if body:
                take = body[:remaining]
                remaining -= len(take)
                job.chunks.append(take)
                job.buffered += len(take)
                self.total_buffered += len(take)
                job.max_chunk = max(job.max_chunk, len(take))
                job.peak = max(job.peak, job.buffered)
                job.data_ready.set()
            if remaining > 0 and not message.get("more_body", False):
                job.failed = True
                job.wake()
                await _json(send, 400, {"detail": SHORT_BODY})
                return
        if not job.receiver_gone:
            job.ended = True
            job.data_ready.set()
            await job.delivered.wait()
        if job.receiver_gone:
            await _json(send, 410, {"detail": RECEIVER_GONE})
            return
        await _json(send, 200, {"ok": True, "sent": job.length})


def _receiver_left(job: Job) -> None:
    """The receiver's connection closed (or its request finished and the watcher was cancelled)."""
    if not job.delivered.is_set():
        job.receiver_gone = True
        job.wake()


async def _disconnected(receive: Receive) -> None:
    """Returns when the client goes away (any request body is ignored)."""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


async def _json(send: Send, status: int, payload: dict[str, Any], extra: list[tuple[bytes, bytes]] | None = None) -> None:
    body = json.dumps(payload).encode()
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"), *CORS, *(extra or []),
    ]})
    await send({"type": "http.response.body", "body": body, "more_body": False})


async def _empty(send: Send, status: int, extra: list[tuple[bytes, bytes]] | None = None) -> None:
    await send({"type": "http.response.start", "status": status, "headers": [
        (b"content-length", b"0"), (b"cache-control", b"no-store"), *CORS, *(extra or []),
    ]})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def _preflight(send: Send) -> None:
    await _empty(send, 204, [
        (b"access-control-allow-methods", b"GET, PUT, OPTIONS"),
        (b"access-control-allow-headers", b"Range, Content-Type"),
        (b"access-control-max-age", b"600"),
    ])


async def _offline(send: Send, first_name: str, accept: str) -> None:
    text = OFFLINE.format(name=first_name or "The sender")
    if "text/html" in accept:
        body = (
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width, initial-scale=1\"><title>Zinwe</title></head>"
            "<body style=\"font-family:Geist,system-ui,sans-serif;color:#141210;margin:40px 16px\">"
            f"<p>{escape(text)}</p></body></html>"
        ).encode()
        await send({"type": "http.response.start", "status": 503, "headers": [
            (b"content-type", b"text/html; charset=utf-8"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"), (b"retry-after", b"30"), *CORS,
        ]})
        await send({"type": "http.response.body", "body": body, "more_body": False})
        return
    await _json(send, 503, {"detail": text}, [(b"retry-after", b"30")])


app = Relay()
