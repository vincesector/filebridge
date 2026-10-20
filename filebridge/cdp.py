"""Minimal Chrome DevTools Protocol client over websockets.

One browser-level connection per tool call; tabs and out-of-process iframes are reached through flattened
sessions (Target.attachToTarget / Target.setAutoAttach with flatten=true). That works the same for the dedicated
browser, a browser started with --remote-debugging-port, and Chrome's chrome://inspect remote-debugging toggle
(which has no HTTP /json endpoints, only the browser websocket).
"""

import asyncio
import json
import urllib.request
from typing import Any, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .common import FBError


class CDPError(FBError):
    pass


def http_json(base: str, path: str, method: str = "GET", timeout: float = 5) -> Any:
    req = urllib.request.Request(base + path, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if body.strip().startswith(("{", "[")) else body


async def ahttp_json(base: str, path: str, method: str = "GET", timeout: float = 5) -> Any:
    return await asyncio.to_thread(http_json, base, path, method, timeout)


class CDP:
    def __init__(self, ws):
        self.ws = ws
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list[tuple[str, str | None, Callable[[dict], bool] | None, asyncio.Future]] = []
        # child sessionId -> targetInfo plus "parent" (the sessionId it was attached under; None = browser level)
        self.attached: dict[str, dict] = {}
        self._reader = asyncio.create_task(self._read())

    @classmethod
    async def connect(cls, ws_url: str) -> "CDP":
        # No Origin header is sent, so no --remote-allow-origins flag is needed.
        try:
            ws = await connect(ws_url, max_size=None, ping_interval=None, open_timeout=10)
        except (OSError, TimeoutError, WebSocketException) as e:
            raise CDPError(f"Could not open DevTools at {ws_url}: {e}. Is the browser still running (and did you allow the connection)?", "BrowserNotRunning")
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
                    sid = msg.get("sessionId")
                    params = msg.get("params", {})
                    if msg.get("method") == "Target.attachedToTarget":
                        self.attached[params["sessionId"]] = {**params["targetInfo"], "parent": sid}
                    for item in list(self._listeners):
                        method, want_sid, pred, fut = item
                        if msg.get("method") == method and want_sid == sid and not fut.done() and (pred is None or pred(params)):
                            fut.set_result(params)
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
            raise CDPError(f"CDP {method} timed out after {timeout:.0f}s", "Timeout")

    def expect(self, method: str, pred: Callable[[dict], bool] | None = None, session_id: str | None = None) -> asyncio.Future:
        """Register for the next matching event BEFORE triggering it."""
        fut = asyncio.get_running_loop().create_future()
        self._listeners.append((method, session_id, pred, fut))
        return fut

    async def close(self) -> None:
        self._reader.cancel()
        await self.ws.close()


class Scoped:
    """A flattened CDP session (a tab, or an out-of-process iframe inside it) with the same send()/expect() as CDP."""

    def __init__(self, cdp: CDP, session_id: str):
        self.cdp, self.session_id = cdp, session_id

    async def send(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        return await self.cdp.send(method, params, timeout, session_id=self.session_id)

    def expect(self, method: str, pred: Callable[[dict], bool] | None = None) -> asyncio.Future:
        return self.cdp.expect(method, pred, self.session_id)
