"""Minimal Chrome DevTools Protocol client over websockets (one connection per tool call)."""

import asyncio
import json
import urllib.request
from typing import Any, Callable

from websockets.asyncio.client import connect

from .common import CDP_PORT, FBError

HTTP_BASE = f"http://127.0.0.1:{CDP_PORT}"


class CDPError(FBError):
    pass


def http_json(path: str, method: str = "GET", timeout: float = 5) -> Any:
    req = urllib.request.Request(HTTP_BASE + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip().startswith(("{", "[")) else body


async def ahttp_json(path: str, method: str = "GET", timeout: float = 5) -> Any:
    return await asyncio.to_thread(http_json, path, method, timeout)


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list[tuple[str, Callable[[dict], bool] | None, asyncio.Future]] = []
        self.attached: dict[str, dict] = {}  # sessionId -> targetInfo, filled by Target.setAutoAttach (out-of-process iframes)
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, ws_url: str) -> "CDP":
        # No Origin header is sent, so no --remote-allow-origins flag is needed.
        ws = await connect(ws_url, max_size=None, ping_interval=None, open_timeout=10)
        return cls(ws)

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CDPError(f"{msg['error'].get('message')} ({msg['error'].get('code')})"))
                        else:
                            fut.set_result(msg.get("result", {}))
                else:
                    if msg.get("method") == "Target.attachedToTarget":
                        self.attached[msg["params"]["sessionId"]] = msg["params"]["targetInfo"]
                    for item in list(self._listeners):
                        method, pred, fut = item
                        if msg.get("method") == method and not fut.done() and (pred is None or pred(msg.get("params", {}))):
                            fut.set_result(msg.get("params", {}))
                            self._listeners.remove(item)
        except Exception:
            pass
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPError("DevTools connection closed (tab closed or browser exited)."))

    async def send(self, method: str, params: dict | None = None, timeout: float = 60, session_id: str | None = None) -> dict:
        self._id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[self._id] = fut
        msg = {"id": self._id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await self.ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout)
        except TimeoutError:
            raise CDPError(f"CDP {method} timed out after {timeout:.0f}s")

    def expect(self, method: str, pred: Callable[[dict], bool] | None = None) -> asyncio.Future:
        """Register for the next matching event BEFORE triggering it."""
        fut = asyncio.get_running_loop().create_future()
        self._listeners.append((method, pred, fut))
        return fut

    async def close(self) -> None:
        self._reader.cancel()
        await self.ws.close()


class Scoped:
    """A CDP child session (e.g. an out-of-process iframe) exposing the same send() as CDP."""

    def __init__(self, cdp: CDP, session_id: str):
        self._cdp, self._sid = cdp, session_id

    async def send(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        return await self._cdp.send(method, params, timeout, session_id=self._sid)
