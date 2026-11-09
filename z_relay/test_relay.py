"""The relay (z_relay.app), in process: pairing, Range / 206, sender offline -> 503, status, receiver going away,
the memory cap (backpressure), ticket scope and age, and that it never touches the database."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

from . import tickets
from .app import Relay

FILE_ID = str(uuid.uuid4())
HOLDER = "d42"
DATA = bytes(range(256)) * 40  # 10 240 bytes


def get_ticket(file_id: str = FILE_ID, size: int = len(DATA), holder: str = HOLDER, inline_type: str = "") -> str:
    return tickets.make_get(file_id, size, "Quote Lumen.pdf", holder, "Priya", inline_type)


def serve_ticket(holder: str = HOLDER) -> str:
    return tickets.make_serve(holder)


class Call:
    """One HTTP request to the ASGI app, driven by the test."""

    def __init__(self, app: Relay, method: str, path: str, ticket: str = "", query: str = "",
                 headers: dict[str, str] | None = None, body: bool = False) -> None:
        qs = f"t={ticket}" if ticket else ""
        if query:
            qs = f"{qs}&{query}" if qs else query
        self.scope = {
            "type": "http", "method": method, "path": path, "query_string": qs.encode(),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        }
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.taken = 0  # messages the app has read
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = bytearray()
        self.finished = False
        self.started = asyncio.Event()
        self.gate: asyncio.Event | None = None  # when set up, body sends block until it is set
        if not body:
            self.inbox.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        self.task = asyncio.ensure_future(app(self.scope, self.receive, self.send))

    async def receive(self) -> dict[str, Any]:
        message = await self.inbox.get()
        self.taken += 1
        return message

    async def send(self, message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = {k.decode(): v.decode() for k, v in message["headers"]}
            self.started.set()
            return
        if self.gate is not None and message.get("body"):
            await self.gate.wait()
        self.body += message.get("body", b"")
        if not message.get("more_body", False):
            self.finished = True

    def feed(self, data: bytes, more: bool = True) -> None:
        self.inbox.put_nowait({"type": "http.request", "body": data, "more_body": more})

    def disconnect(self) -> None:
        self.inbox.put_nowait({"type": "http.disconnect"})

    def json(self) -> Any:
        return json.loads(bytes(self.body))

    async def result(self, timeout: float = 5) -> Call:
        await asyncio.wait_for(asyncio.shield(self.task), timeout)
        return self


async def settle(n: int = 20) -> None:
    for _ in range(n):
        await asyncio.sleep(0)


class RelayTests(unittest.IsolatedAsyncioTestCase):
    def relay(self, **kw: Any) -> Relay:
        kw.setdefault("wait_seconds", 2)
        kw.setdefault("sender_timeout", 2)
        kw.setdefault("online_seconds", 40)
        return Relay(**kw)

    async def push_all(self, relay: Relay, job: dict[str, Any], data: bytes, chunk: int = 1000,
                       ticket: str | None = None) -> Call:
        push = Call(relay, "PUT", f"/relay/push/{job['job']}", ticket or serve_ticket(), body=True)
        for i in range(0, len(data), chunk):
            push.feed(data[i:i + chunk], more=i + chunk < len(data))
        return push

    async def test_health_and_unknown_routes(self) -> None:
        relay = self.relay()
        r = await Call(relay, "GET", "/relay/health").result()
        self.assertEqual((r.status, r.json()), (200, {"ok": True}))
        self.assertEqual((await Call(relay, "GET", "/relay/nope").result()).status, 404)
        self.assertEqual((await Call(relay, "GET", "/other").result()).status, 404)
        self.assertEqual((await Call(relay, "POST", "/relay/wait").result()).status, 405)
        pre = await Call(relay, "OPTIONS", "/relay/push/x").result()
        self.assertEqual((pre.status, pre.headers["access-control-allow-origin"]), (204, "*"))

    async def test_lifespan(self) -> None:
        relay = self.relay()
        sent: list[dict[str, Any]] = []
        inbox = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

        async def receive() -> dict[str, Any]:
            return inbox.pop(0)

        async def send(m: dict[str, Any]) -> None:
            sent.append(m)

        await relay({"type": "lifespan"}, receive, send)
        self.assertEqual([m["type"] for m in sent], ["lifespan.startup.complete", "lifespan.shutdown.complete"])

    async def test_wait_returns_204_with_no_job(self) -> None:
        relay = self.relay(wait_seconds=0.05)
        r = await Call(relay, "GET", "/relay/wait", serve_ticket()).result()
        self.assertEqual((r.status, bytes(r.body)), (204, b""))

    async def test_pairing_whole_file(self) -> None:
        relay = self.relay()
        wait = Call(relay, "GET", "/relay/wait", serve_ticket())
        await settle()
        get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket())
        await wait.result()
        self.assertEqual(wait.status, 200)
        job = wait.json()
        self.assertEqual(set(job), {"job", "file_id", "offset"})
        self.assertEqual((job["file_id"], job["offset"]), (FILE_ID, 0))
        push = await self.push_all(relay, job, DATA)
        await get.result()
        await push.result()
        self.assertEqual((get.status, bytes(get.body), get.finished), (200, DATA, True))
        self.assertEqual(get.headers["content-length"], str(len(DATA)))
        self.assertEqual(get.headers["content-type"], "application/octet-stream")
        self.assertEqual(get.headers["content-disposition"], "attachment; filename*=UTF-8''Quote%20Lumen.pdf")
        self.assertEqual((get.headers["accept-ranges"], get.headers["x-content-type-options"],
                          get.headers["cache-control"]), ("bytes", "nosniff", "private, no-store"))
        self.assertNotIn("content-range", get.headers)
        self.assertEqual((push.status, push.json()), (200, {"ok": True, "sent": len(DATA)}))
        self.assertEqual(relay.jobs, {})  # nothing left in memory

    async def test_a_job_waits_for_the_senders_next_poll(self) -> None:
        relay = self.relay()
        get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket())
        await settle()
        wait = await Call(relay, "GET", "/relay/wait", serve_ticket()).result()  # queued job: returned at once
        self.assertEqual(wait.status, 200)
        push = await self.push_all(relay, wait.json(), DATA, chunk=4096)
        await get.result()
        await push.result()
        self.assertEqual(bytes(get.body), DATA)

    async def test_range_gives_206_from_the_offset(self) -> None:
        relay = self.relay()
        get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket(), headers={"Range": "bytes=1000-"})
        await settle()
        job = (await Call(relay, "GET", "/relay/wait", serve_ticket()).result()).json()
        self.assertEqual(job["offset"], 1000)
        push = await self.push_all(relay, job, DATA[1000:])
        await get.result()
        await push.result()
        self.assertEqual((get.status, bytes(get.body)), (206, DATA[1000:]))
        self.assertEqual(get.headers["content-range"], f"bytes 1000-{len(DATA) - 1}/{len(DATA)}")
        self.assertEqual(get.headers["content-length"], str(len(DATA) - 1000))

    async def test_closed_range_stops_the_push_at_its_end(self) -> None:
        relay = self.relay()
        get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket(), headers={"Range": "bytes=10-19"})
        await settle()
        job = (await Call(relay, "GET", "/relay/wait", serve_ticket()).result()).json()
        push = await self.push_all(relay, job, DATA[10:], chunk=7)  # the sender sends to the end of the file
        await get.result()
        await push.result()
        self.assertEqual((get.status, bytes(get.body)), (206, DATA[10:20]))
        self.assertEqual(get.headers["content-range"], f"bytes 10-19/{len(DATA)}")
        self.assertEqual((push.status, push.json()["sent"]), (200, 10))

    async def test_range_past_the_end_is_416(self) -> None:
        relay = self.relay()
        get = await Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket(),
                         headers={"Range": f"bytes={len(DATA)}-"}).result()
        self.assertEqual((get.status, get.headers["content-range"]), (416, f"bytes */{len(DATA)}"))
        self.assertEqual(relay.jobs, {})

    async def test_sender_offline_is_503_after_the_timeout(self) -> None:
        relay = self.relay(sender_timeout=0.05)
        start = time.monotonic()
        api = await Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket()).result()
        self.assertGreaterEqual(time.monotonic() - start, 0.05)
        self.assertEqual((api.status, api.json()),
                         (503, {"detail": "Priya's PC is offline. The download starts when it's back."}))
        browser = await Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket(),
                             headers={"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}).result()
        self.assertEqual((browser.status, browser.headers["content-type"]), (503, "text/html; charset=utf-8"))
        self.assertIn("Priya&#x27;s PC is offline. The download starts when it&#x27;s back.", bytes(browser.body).decode())
        self.assertEqual((relay.jobs, relay.queued), ({}, {}))
        # A push for the timed-out job is turned away, so the sender stops.
        late = await Call(relay, "PUT", f"/relay/push/{uuid.uuid4().hex}", serve_ticket(), body=True).result()
        self.assertEqual((late.status, late.json()), (404, {"detail": "That download isn't waiting any more."}))

    async def test_status_online_while_waiting_and_shortly_after(self) -> None:
        relay = self.relay(wait_seconds=0.05, online_seconds=0.2)
        status = f"/relay/status/{FILE_ID}"
        self.assertEqual((await Call(relay, "GET", status, get_ticket()).result()).json(), {"online": False})
        wait = Call(relay, "GET", "/relay/wait", serve_ticket())
        await settle()
        self.assertEqual((await Call(relay, "GET", status, get_ticket()).result()).json(), {"online": True})
        await wait.result()
        self.assertEqual((await Call(relay, "GET", status, get_ticket()).result()).json(), {"online": True})
        await asyncio.sleep(0.25)
        self.assertEqual((await Call(relay, "GET", status, get_ticket()).result()).json(), {"online": False})
        # Another holder's file: offline.
        other = get_ticket(holder="b7.browser-abc123")
        self.assertEqual((await Call(relay, "GET", status, other).result()).json(), {"online": False})

    async def test_receiver_going_away_stops_the_push(self) -> None:
        relay = self.relay()
        get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket())
        await settle()
        job = (await Call(relay, "GET", "/relay/wait", serve_ticket()).result()).json()
        push = Call(relay, "PUT", f"/relay/push/{job['job']}", serve_ticket(), body=True)
        push.feed(DATA[:1000])
        await asyncio.wait_for(get.started.wait(), 2)
        await settle()
        self.assertEqual(bytes(get.body), DATA[:1000])
        get.disconnect()
        await get.result()
        push.feed(DATA[1000:2000])
        await push.result()
        self.assertEqual((push.status, push.json()), (410, {"detail": "The receiver has gone. Stop sending."}))
        self.assertFalse(get.finished)
        self.assertEqual(relay.jobs, {})

    async def test_receiver_leaving_before_the_sender_starts(self) -> None:
        relay = self.relay()
        get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket())
        await settle()
        get.disconnect()
        await get.result()
        self.assertIsNone(get.status)
        self.assertEqual(relay.jobs, {})
        wait = await Call(relay, "GET", "/relay/wait", serve_ticket(), ).result(timeout=5)
        self.assertEqual(wait.status, 204)  # the dropped job isn't handed out

    async def test_sender_cut_off_mid_push_cuts_the_download(self) -> None:
        relay = self.relay()
        get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket())
        await settle()
        job = (await Call(relay, "GET", "/relay/wait", serve_ticket()).result()).json()
        push = Call(relay, "PUT", f"/relay/push/{job['job']}", serve_ticket(), body=True)
        push.feed(DATA[:500])
        push.disconnect()
        await push.result()
        await get.result()
        self.assertEqual((get.status, bytes(get.body), get.finished), (200, DATA[:500], False))
        # A body that simply ends early is refused.
        get2 = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket())
        await settle()
        job = (await Call(relay, "GET", "/relay/wait", serve_ticket()).result()).json()
        push = Call(relay, "PUT", f"/relay/push/{job['job']}", serve_ticket(), body=True)
        push.feed(DATA[:500], more=False)
        await push.result()
        await get2.result()
        self.assertEqual(push.status, 400)
        self.assertFalse(get2.finished)

    async def test_memory_cap_backpressure(self) -> None:
        cap, chunk = 256 * 1024, 64 * 1024
        big = os.urandom(2 * 1024 * 1024)
        relay = self.relay(buffer_bytes=cap)
        get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket(size=len(big)))
        get.gate = asyncio.Event()  # the receiver is stuck: nothing drains
        await settle()
        job = (await Call(relay, "GET", "/relay/wait", serve_ticket()).result()).json()
        push = await self.push_all(relay, job, big, chunk=chunk)
        await asyncio.sleep(0.05)
        held = relay.jobs[job["job"]]
        # The receiver holds one chunk in its send; the relay holds at most the cap, and stops reading the push.
        self.assertLessEqual(held.buffered, cap)
        self.assertLessEqual(push.taken, cap // chunk + 2)
        self.assertFalse(push.task.done())
        get.gate.set()
        await get.result()
        await push.result()
        self.assertEqual((bytes(get.body), push.status), (big, 200))
        self.assertLessEqual(held.peak, cap)

    async def test_total_memory_cap_across_transfers(self) -> None:
        chunk, total = 64 * 1024, 192 * 1024
        big = os.urandom(1024 * 1024)
        relay = self.relay(buffer_bytes=1024 * 1024, total_bytes=total)
        gets, pushes = [], []
        for n in range(2):
            fid = str(uuid.uuid4())
            get = Call(relay, "GET", f"/relay/file/{fid}", get_ticket(file_id=fid, size=len(big)))
            get.gate = asyncio.Event()
            await settle()
            job = (await Call(relay, "GET", "/relay/wait", serve_ticket()).result()).json()
            pushes.append(await self.push_all(relay, job, big, chunk=chunk))
            gets.append(get)
            await asyncio.sleep(0.03)
        # Each transfer may always hold one chunk; beyond that the total cap holds both pushes back.
        self.assertLessEqual(relay.total_buffered, total + chunk)
        self.assertEqual(len(relay.blocked), 2)
        for get in gets:
            get.gate.set()
        for get, push in zip(gets, pushes):
            await get.result()
            await push.result()
            self.assertEqual((bytes(get.body), push.status), (big, 200))
        self.assertEqual((relay.total_buffered, relay.blocked, relay.jobs), (0, set(), {}))

    async def test_inline_only_for_types_django_allowed(self) -> None:
        relay = self.relay()
        for inline_type, query, ctype, disposition in (
            ("image/png", "inline=1", "image/png", "inline"),
            ("image/png", "", "application/octet-stream", "attachment"),
            ("", "inline=1", "application/octet-stream", "attachment"),
        ):
            get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket(inline_type=inline_type), query=query)
            await settle()
            job = (await Call(relay, "GET", "/relay/wait", serve_ticket()).result()).json()
            push = await self.push_all(relay, job, DATA, chunk=5000)
            await get.result()
            await push.result()
            self.assertEqual(get.headers["content-type"], ctype)
            self.assertTrue(get.headers["content-disposition"].startswith(disposition + ";"))

    async def test_ticket_scope_signature_and_age(self) -> None:
        relay = self.relay(wait_seconds=0.05, sender_timeout=0.05)
        expired = {"detail": "This link has expired. Ask for a new one."}
        # A get ticket can't wait or push; a serve ticket can't download or ask the status.
        r = await Call(relay, "GET", "/relay/wait", get_ticket()).result()
        self.assertEqual((r.status, r.json()), (403, expired))
        r = await Call(relay, "PUT", f"/relay/push/{uuid.uuid4().hex}", get_ticket(), body=True).result()
        self.assertEqual(r.status, 403)
        self.assertEqual((await Call(relay, "GET", f"/relay/file/{FILE_ID}", serve_ticket()).result()).status, 403)
        self.assertEqual((await Call(relay, "GET", f"/relay/status/{FILE_ID}", serve_ticket()).result()).status, 403)
        # A ticket for another file; a tampered one; none.
        other = get_ticket(file_id=str(uuid.uuid4()))
        self.assertEqual((await Call(relay, "GET", f"/relay/file/{FILE_ID}", other).result()).status, 403)
        tampered = get_ticket()[:-2] + "xx"
        self.assertEqual((await Call(relay, "GET", f"/relay/file/{FILE_ID}", tampered).result()).status, 403)
        self.assertEqual((await Call(relay, "GET", f"/relay/file/{FILE_ID}").result()).status, 403)
        # Too old: a get ticket lives 15 minutes, a serve ticket 24 hours.
        with mock.patch("django.core.signing.time.time", return_value=time.time() - tickets.GET_MAX_AGE - 5):
            old_get = get_ticket()
        with mock.patch("django.core.signing.time.time", return_value=time.time() - tickets.SERVE_MAX_AGE - 5):
            old_serve = serve_ticket()
        self.assertEqual((await Call(relay, "GET", f"/relay/file/{FILE_ID}", old_get).result()).status, 403)
        self.assertEqual((await Call(relay, "GET", "/relay/wait", old_serve).result()).status, 403)
        with mock.patch("django.core.signing.time.time", return_value=time.time() - tickets.GET_MAX_AGE + 30):
            young_get = get_ticket()
        self.assertEqual((await Call(relay, "GET", f"/relay/file/{FILE_ID}", young_get).result()).status, 503)
        # Another holder's serve ticket can't push someone else's job.
        get = Call(relay, "GET", f"/relay/file/{FILE_ID}", get_ticket())
        relay.sender_timeout = 2
        await settle()
        job = (await Call(relay, "GET", "/relay/wait", serve_ticket()).result()).json()
        r = await Call(relay, "PUT", f"/relay/push/{job['job']}", serve_ticket("d43"), body=True).result()
        self.assertEqual(r.status, 404)
        push = await self.push_all(relay, job, DATA)
        await push.result()
        await get.result()
        self.assertEqual(bytes(get.body), DATA)
        # The same job can't be pushed twice.
        self.assertEqual((await Call(relay, "PUT", f"/relay/push/{job['job']}", serve_ticket(), body=True)
                          .result()).status, 404)

    def test_ticket_payloads(self) -> None:
        t = get_ticket(inline_type="application/pdf")
        self.assertEqual(tickets.read(t, tickets.GET), {
            "k": "get", "f": FILE_ID, "s": len(DATA), "n": "Quote Lumen.pdf", "h": HOLDER, "fn": "Priya",
            "i": "application/pdf",
        })
        self.assertIsNone(tickets.read(t, tickets.SERVE))
        self.assertEqual(tickets.read(serve_ticket("b3.AbCdEfGh12"), tickets.SERVE), {"k": "serve", "h": "b3.AbCdEfGh12"})
        self.assertIsNone(tickets.read(serve_ticket("x9"), tickets.SERVE))  # not a holder id
        # Another salt (another use of SECRET_KEY) never passes.
        from django.core import signing

        foreign = signing.TimestampSigner(salt="something.else").sign_object({"k": "serve", "h": HOLDER})
        self.assertIsNone(tickets.read(foreign, tickets.SERVE))


class NoDatabaseTests(unittest.TestCase):
    def test_the_relay_never_loads_the_orm_or_a_database_driver(self) -> None:
        """Run in a fresh interpreter: import the app, pair a whole download, then check what got imported."""
        script = r'''
import asyncio, sys, uuid
from z_relay import tickets
from z_relay.app import Relay
from z_relay.test_relay import Call, settle
async def main():
    relay = Relay(wait_seconds=1, sender_timeout=1)
    fid = str(uuid.uuid4())
    get = Call(relay, "GET", f"/relay/file/{fid}", tickets.make_get(fid, 3, "a.txt", "d1"))
    await settle()
    job = (await Call(relay, "GET", "/relay/wait", tickets.make_serve("d1")).result()).json()
    push = Call(relay, "PUT", f"/relay/push/{job['job']}", tickets.make_serve("d1"), body=True)
    push.feed(b"abc", more=False)
    await push.result(); await get.result()
    assert bytes(get.body) == b"abc", get.body
asyncio.run(main())
from django.apps import apps
loaded = [m for m in sys.modules if m.startswith(("django.db.models", "django.db.backends", "psycopg"))]
print("apps_ready", apps.ready, "loaded", loaded)
'''
        backend = Path(__file__).resolve().parent.parent
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings")}
        out = subprocess.run([sys.executable, "-c", script], cwd=backend, env=env, capture_output=True, text=True,
                             timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("apps_ready False loaded []", out.stdout)
