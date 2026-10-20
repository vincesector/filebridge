"""Lane 2: drive a DEDICATED browser profile over CDP so the browser reads local files by path.

Never touches the daily browser: it only ever talks to port 9229, and it refuses to use that
port unless the listener was started with our --user-data-dir and is bound to 127.0.0.1.
"""

import asyncio
import json
import os
import subprocess
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from .cdp import CDP, CDPError, Scoped, ahttp_json
from .common import BROWSER_PROFILE, CDP_PORT, STATE_DIR, FBError, find_tool, local_path, run_capture

BROWSERS = [
    ("brave", "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    ("chrome", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
]

# Open shadow roots and same-origin iframes are walked in JS; closed shadow roots and
# cross-origin frames that expose a contentDocument are covered by the pierced DOM tree.
FB_JS = """
function fbRoots(root, out) { out.push(root);
  for (const el of root.querySelectorAll('*')) {
    if (el.shadowRoot) fbRoots(el.shadowRoot, out);
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') { try { if (el.contentDocument) fbRoots(el.contentDocument, out); } catch (e) {} }
  } return out; }
function fbFind(sel) { for (const r of fbRoots(document, [])) { const m = r.querySelector(sel); if (m) return m; } return null; }
"""


# ---------------------------------------------------------------- launch / lifecycle

async def _version() -> dict | None:
    try:
        v = await ahttp_json("/json/version", timeout=2)
        return v if isinstance(v, dict) else None
    except Exception:
        return None


async def _listener() -> dict | None:
    """Who listens on the CDP port: {'pid', 'bind', 'cmd'} or None."""
    rc, out, _ = await run_capture([find_tool("lsof", ("/usr/sbin/lsof",)), "-nP", f"-iTCP:{CDP_PORT}", "-sTCP:LISTEN", "-Fpn"], timeout=10)
    pid, binds = None, []
    for ln in out.splitlines():
        if ln.startswith("p"):
            pid = int(ln[1:])
        elif ln.startswith("n"):
            binds.append(ln[1:])
    if pid is None:
        return None
    _, cmd, _ = await run_capture(["/bin/ps", "-p", str(pid), "-o", "command="], timeout=5)
    return {"pid": pid, "bind": binds, "cmd": cmd.strip()}


async def _assert_ours() -> dict:
    info = await _listener()
    if not info:
        raise FBError("Nothing is listening on the automation port. Call browser_launch first.")
    if f"--user-data-dir={BROWSER_PROFILE}" not in info["cmd"]:
        raise FBError(f"Port {CDP_PORT} is held by a process that was NOT started with the filebridge profile ({info['cmd'][:120]}). Refusing to attach.")
    if not all(b.startswith("127.0.0.1:") for b in info["bind"]):
        raise FBError(f"Debug port is bound to {info['bind']}, not loopback only. Refusing to use it; close that browser.")
    return info


async def _pid_running(pid: int) -> bool:
    _, out, _ = await run_capture(["/bin/ps", "-p", str(pid), "-o", "stat="], timeout=5)
    return bool(out.strip()) and not out.strip().startswith("Z")  # a zombie has already exited


def needs_restart(running_cmd: str, want_visible: bool) -> bool:
    return ("--headless" not in running_cmd) != want_visible


def _launch_args(exe: str, url: str | None, visible: bool) -> list[str]:
    args = [exe, f"--remote-debugging-port={CDP_PORT}", "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={BROWSER_PROFILE}", "--no-first-run", "--no-default-browser-check"]
    if not visible:
        args += ["--headless=new", "--window-size=1440,900"]
    return args + [url or "about:blank"]


def _spawn(args: list[str]) -> None:
    log = open(STATE_DIR / "browser.log", "ab")
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)


async def launch(url: str | None, browser: str = "auto", visible: bool = False) -> dict[str, Any]:
    if url and urllib.parse.urlparse(url).scheme not in ("http", "https", "file", "about"):
        raise FBError("url must be http(s):// or file://.")
    ver = await _version()
    restarted = False
    if ver:
        info = await _assert_ours()
        if needs_restart(info["cmd"], visible):
            # Mode switch (headless <-> visible) on the same profile: shut the old one down cleanly first so cookies are flushed.
            await close()
            restarted, ver = True, None
    reused = ver is not None
    if reused:
        if url:
            await ahttp_json("/json/new?" + urllib.parse.quote(url, safe=":/?&=#%@+,;~!$'()*"), method="PUT", timeout=10)
    else:
        if await _listener():
            raise FBError(f"Port {CDP_PORT} is already in use by another process. Free it or stop that process first.")
        chosen = [(n, p) for n, p in BROWSERS if os.access(p, os.X_OK) and browser in ("auto", n)]
        if not chosen:
            raise FBError(f"No usable browser for browser={browser!r}. Looked for Brave and Chrome in /Applications.")
        name, exe = chosen[0]
        BROWSER_PROFILE.mkdir(parents=True, exist_ok=True, mode=0o700)
        _spawn(_launch_args(exe, url, visible))
        for _ in range(100):
            await asyncio.sleep(0.25)
            ver = await _version()
            if ver:
                break
        if not ver:
            raise FBError(f"{name} did not expose DevTools on port {CDP_PORT} within 25s. See {STATE_DIR / 'browser.log'}.")
        await _drop_restored_tabs(url)
    info = await _assert_ours()
    return {"running": True, "mode": "visible" if visible else "headless", "reused_existing": reused, "restarted_for_mode_switch": restarted,
            "app": "brave" if "Brave" in info["cmd"] else "chrome", "browser": ver.get("Browser"), "port": CDP_PORT,
            "profile": str(BROWSER_PROFILE), "pid": info["pid"], "tabs": await tabs(),
            "note": ("A browser window is open on screen: sign in to the site(s) now, then call browser_launch() again (headless) so agents can use the saved session."
                     if visible else "Headless: nothing is shown on screen. Sessions saved in the profile are used.")}


async def _drop_restored_tabs(url: str | None) -> None:
    """A fresh start restores last session's tabs, so ids and url matches pile up run after run. Keep one tab."""
    prev, stable = None, 0
    for _ in range(16):  # wait until the restored tab set stops changing (max ~6s)
        pages = [t for t in await _pages_raw()]
        ids = sorted(t["id"] for t in pages)
        stable = stable + 1 if ids == prev and ids else 0
        if stable >= 2:
            break
        prev = ids
        await asyncio.sleep(0.4)
    keep = next((t for t in pages if url and t.get("url", "").rstrip("/") == url.rstrip("/")), pages[0] if pages else None)
    for t in pages:
        if keep and t["id"] != keep["id"]:
            await ahttp_json(f"/json/close/{t['id']}")


async def _pages_raw() -> list[dict]:
    return [t for t in await ahttp_json("/json/list") if t.get("type") == "page" and not t.get("url", "").startswith("devtools://")]


async def close() -> dict[str, Any]:
    ver = await _version()
    if not ver:
        return {"closed": False, "note": "The automation browser is not running."}
    info = await _assert_ours()
    c = await CDP.connect(ver["webSocketDebuggerUrl"])
    try:
        await c.send("Browser.close", timeout=10)
    except CDPError:
        pass
    finally:
        try:
            await c.close()
        except Exception:
            pass
    # Wait for the process itself (not just the port) so the profile lock is released before any relaunch.
    for _ in range(60):
        await asyncio.sleep(0.25)
        if not await _version() and not await _pid_running(info["pid"]):
            return {"closed": True}
    return {"closed": False, "note": "Browser did not exit within 15s."}


# ---------------------------------------------------------------- tabs

async def _pages() -> list[dict]:
    if not await _version():
        raise FBError("The automation browser is not running. Call browser_launch first.")
    await _assert_ours()
    return await _pages_raw()


async def tabs() -> list[dict[str, str]]:
    return [{"id": t["id"], "title": t.get("title", ""), "url": t.get("url", "")} for t in await _pages()]


async def _resolve_tab(tab: str) -> dict:
    pages = await _pages()
    exact = [t for t in pages if t["id"] == tab]
    if exact:
        return exact[0]
    fuzzy = [t for t in pages if tab.lower() in t.get("url", "").lower() or tab.lower() in t.get("title", "").lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    listing = "; ".join(f"{t['id'][:8]}… {t.get('title','')[:40]} <{t.get('url','')[:60]}>" for t in pages)
    why = "matches several tabs" if fuzzy else "matches no tab"
    raise FBError(f"tab {tab!r} {why}. Pass a full id from browser_tabs() or a unique part of the url/title. Open tabs: {listing}")


@asynccontextmanager
async def _session(tab: str):
    t = await _resolve_tab(tab)
    c = await CDP.connect(t["webSocketDebuggerUrl"])
    try:
        yield c, t
    finally:
        try:
            await c.close()
        except Exception:
            pass


# ---------------------------------------------------------------- helpers

def _validate_files(paths: list[str]) -> list[str]:
    if not paths:
        raise FBError("Give at least one file path.")
    out = []
    for p in paths:
        lp = local_path(p, must_exist=True, source=True)
        if not lp.is_file():
            raise FBError(f"{lp} is not a regular file (folders cannot be set on a file input; list the files instead).")
        out.append(str(lp))
    return out


async def _eval(c: CDP, expr: str, *, by_value: bool = True) -> Any:
    r = await c.send("Runtime.evaluate", {"expression": FB_JS + expr, "returnByValue": by_value, "awaitPromise": True})
    if "exceptionDetails" in r:
        d = r["exceptionDetails"]
        raise FBError("Page script error: " + (d.get("exception", {}).get("description") or d.get("text", "?"))[:300])
    return r["result"] if not by_value else r["result"].get("value")


async def _find(c: CDP, selector: str) -> str | None:
    """objectId of the first match across the document, open shadow roots and same-origin iframes."""
    res = await _eval(c, f"fbFind({json.dumps(selector)})", by_value=False)
    if res.get("subtype") == "null" or "objectId" not in res:
        return None
    return res["objectId"]


async def _call(c: CDP, object_id: str, fn: str, args: list | None = None) -> Any:
    r = await c.send("Runtime.callFunctionOn", {
        "objectId": object_id, "functionDeclaration": fn, "returnByValue": True, "awaitPromise": True,
        "arguments": [a if isinstance(a, dict) else {"value": a} for a in (args or [])]})
    if "exceptionDetails" in r:
        raise FBError("Page script error: " + (r["exceptionDetails"].get("exception", {}).get("description") or "?")[:300])
    return r["result"].get("value")


async def _candidates(c: CDP) -> list[dict]:
    """All <input type=file> in the pierced DOM tree (includes closed shadow roots)."""
    doc = (await c.send("DOM.getDocument", {"depth": -1, "pierce": True}))["root"]
    found: list[dict] = []

    def walk(n: dict) -> None:
        if n.get("nodeName") == "INPUT":
            a = dict(zip(n.get("attributes", [])[::2], n.get("attributes", [])[1::2]))
            if a.get("type", "").lower() == "file":
                sel = f"#{a['id']}" if a.get("id") else (f'input[name="{a["name"]}"]' if a.get("name") else None)
                found.append({"backend_node_id": n["backendNodeId"], "selector_hint": sel, "id": a.get("id"), "name": a.get("name"),
                              "accept": a.get("accept"), "multiple": "multiple" in a})
        for key in ("children", "shadowRoots"):
            for ch in n.get(key, []):
                walk(ch)
        if "contentDocument" in n:
            walk(n["contentDocument"])

    walk(doc)
    return found


_READ_FILES = "function(){return {n: this.files.length, files: [...this.files].map(f => ({name: f.name, size: f.size})), multiple: this.multiple, id: this.id, name: this.name}}"


# ---------------------------------------------------------------- tools

async def _frame_scopes(c: CDP) -> list[tuple[Any, str]]:
    """Main frame plus every out-of-process iframe (cross-site frames the main session cannot see into)."""
    await c.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
    await asyncio.sleep(0.4)
    return [(c, "main")] + [(Scoped(c, sid), ti.get("url", "")) for sid, ti in c.attached.items() if ti.get("type") == "iframe"]


async def set_files(tab: str, selector: str | None, paths: list[str]) -> dict[str, Any]:
    files = _validate_files(paths)
    async with _session(tab) as (c, t):
        scopes = await _frame_scopes(c)
        err = None
        hit = None  # (scope, objectId)
        if selector:
            for sc, _frame in scopes:
                obj = await _find(sc, selector)
                if obj:
                    try:
                        await sc.send("DOM.setFileInputFiles", {"files": files, "objectId": obj})
                        hit = (sc, obj)
                        break
                    except CDPError as e:
                        err = str(e)
        cands: list[dict] = []
        if not hit:
            scoped_cands = []
            for sc, frame in scopes:
                for cand in await _candidates(sc):
                    scoped_cands.append((sc, {**cand, "frame": frame}))
            cands = [cd for _, cd in scoped_cands]
            if selector:  # selector failed in JS (closed shadow root): accept an exact match on a candidate's own selector
                exact = [x for x in scoped_cands if x[1]["selector_hint"] == selector]
                if len(exact) == 1:
                    scoped_cands = exact
            # With no selector we only act when there is exactly one file input; otherwise guessing could upload to the wrong widget.
            if (selector is None or len(scoped_cands) == 1 and scoped_cands[0][1]["selector_hint"] == selector) and len(scoped_cands) == 1:
                sc, cd = scoped_cands[0]
                try:
                    await sc.send("DOM.setFileInputFiles", {"files": files, "backendNodeId": cd["backend_node_id"]})
                    hit = (sc, (await sc.send("DOM.resolveNode", {"backendNodeId": cd["backend_node_id"]}))["object"]["objectId"])
                except CDPError as e:
                    err = str(e)
        if not hit:
            if err:
                reason = f"matched an element but setting files failed: {err}"
            elif selector:
                reason = f"selector {selector!r} matched nothing"
            else:
                reason = f"{len(cands)} file inputs on the page; refusing to guess" if cands else "no <input type=file> found in the page, its shadow roots (open or closed) or iframes (incl. cross-site)"
            hint = ("Pick one of `candidates` via selector, or some sites only create the input on click: "
                    "use browser_click(tab, selector_of_upload_button, files=[...]) which captures the file chooser.")
            return {"ok": False, "reason": reason, "candidates": cands, "hint": hint}
        sc, obj = hit
        seen = await _call(sc, obj, _READ_FILES)
        want = {os.path.basename(p): os.path.getsize(p) for p in files}
        got = {f["name"]: f["size"] for f in seen["files"]}
        return {"ok": want == got, "method": "DOM.setFileInputFiles", "tab": t["id"], "input": {"id": seen["id"], "name": seen["name"], "multiple": seen["multiple"]},
                "files_in_page": seen["files"], "sizes_match_disk": want == got,
                "note": "The browser reads the files from disk itself, so there is no size cap. 'change' and 'input' events were fired."}


async def _centre(c: CDP, obj: str) -> dict:
    return await _call(c, obj, """function(){ this.scrollIntoView({block:'center', inline:'center'});
      const r = this.getBoundingClientRect(); let x = r.left + r.width/2, y = r.top + r.height/2;
      let w = this.ownerDocument.defaultView; while (w && w.frameElement) { const f = w.frameElement.getBoundingClientRect(); x += f.left; y += f.top; w = w.parent; }
      const top = this.ownerDocument.defaultView === window.top;
      const hit = top ? document.elementFromPoint(x, y) : this;
      const ok = !hit || this === hit || this.contains(hit) || (hit.shadowRoot && hit.shadowRoot.contains(this));
      return {x, y, w: r.width, h: r.height, covered_by: ok ? null : (hit.tagName.toLowerCase() + (hit.id ? '#' + hit.id : '')),
              tag: this.tagName.toLowerCase() + (this.id ? '#' + this.id : ''), is_file_input: this.tagName === 'INPUT' && this.type === 'file'}; }""")


_PROBE = """function(){ const w = this.ownerDocument ? this.ownerDocument.defaultView : window; if (!w.__fbProbe) { w.__fbProbe = [];
  w.addEventListener('drop', e => w.__fbProbe.push({files: e.dataTransfer ? e.dataTransfer.files.length : 0,
    names: e.dataTransfer ? [...e.dataTransfer.files].map(f => f.name + ':' + f.size) : [], trusted: e.isTrusted}), true); }
  w.__fbProbe.length = 0; return true; }"""
_READ_PROBE = "function(){ const w = this.ownerDocument ? this.ownerDocument.defaultView : window; return w.__fbProbe || []; }"


async def drop_files(tab: str, paths: list[str], selector: str | None, x: float | None, y: float | None, method: str) -> dict[str, Any]:
    files = _validate_files(paths)
    async with _session(tab) as (c, t):
        await c.send("Page.bringToFront")
        target = None
        if selector:
            target = await _find(c, selector)
            if not target:
                raise FBError(f"selector {selector!r} matched nothing (searched shadow roots and same-origin iframes).")
            pt = await _centre(c, target)
            x, y = pt["x"], pt["y"]
        elif x is None or y is None:
            vp = await _eval(c, "({w: innerWidth, h: innerHeight})")
            x, y = vp["w"] / 2, vp["h"] / 2
        if target is None:
            r = await c.send("Runtime.evaluate", {"expression": f"document.elementFromPoint({x},{y}) || document.body"})
            target = r["result"].get("objectId")
        await _call(c, target, _PROBE)
        await c.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})

        if method in ("auto", "cdp"):
            data = {"items": [], "files": files, "dragOperationsMask": 1 | 2 | 16}
            for typ in ("dragEnter", "dragOver", "dragOver", "drop"):
                await c.send("Input.dispatchDragEvent", {"type": typ, "x": x, "y": y, "data": data}, timeout=15)
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.3)
            seen = await _call(c, target, _READ_PROBE)
            if any(s["files"] > 0 for s in seen):
                return {"ok": True, "method": "Input.dispatchDragEvent", "trusted_events": all(s["trusted"] for s in seen), "at": {"x": x, "y": y},
                        "page_saw": seen, "note": "Real drag events with disk paths; no size cap."}
            if method == "cdp":
                return {"ok": False, "method": "Input.dispatchDragEvent", "page_saw": seen,
                        "reason": "The page received a drop with no files (or no drop at all). Retry with method='auto' for the in-page fallback."}

        # Fallback: a hidden <input type=file> gets the disk-backed File objects via DOM.setFileInputFiles,
        # and those same File objects go into an in-page DataTransfer. No bytes are copied into JS,
        # but the events are synthetic (isTrusted=false), which a few sites reject.
        tmp = await c.send("Runtime.callFunctionOn", {"objectId": target, "functionDeclaration":
            "function(){ const i = this.ownerDocument.createElement('input'); i.type = 'file'; i.multiple = true; i.style.display = 'none'; this.ownerDocument.body.appendChild(i); return i; }"})
        tmp_id = tmp["result"]["objectId"]
        await c.send("DOM.setFileInputFiles", {"files": files, "objectId": tmp_id})
        n = await _call(c, target, """function(inp, x, y){ const dt = new DataTransfer(); for (const f of inp.files) dt.items.add(f);
            for (const t of ['dragenter', 'dragover', 'drop']) this.dispatchEvent(new DragEvent(t, {bubbles: true, cancelable: true, composed: true, dataTransfer: dt, clientX: x, clientY: y}));
            inp.remove(); return dt.files.length; }""", [{"objectId": tmp_id}, x, y])
        await asyncio.sleep(0.2)
        seen = await _call(c, target, _READ_PROBE)
        return {"ok": n == len(files), "method": "synthetic-DataTransfer", "trusted_events": False, "at": {"x": x, "y": y}, "page_saw": seen,
                "note": "Fallback used: File objects are disk-backed (no size cap) but the drag events are synthetic (isTrusted=false); a site that checks isTrusted will ignore them."}


async def click(tab: str, selector: str, files: list[str] | None) -> dict[str, Any]:
    paths = _validate_files(files) if files else None
    async with _session(tab) as (c, t):
        await c.send("Page.enable")
        await c.send("Page.bringToFront")
        obj = await _find(c, selector)
        if not obj:
            raise FBError(f"selector {selector!r} matched nothing (searched shadow roots and same-origin iframes). Use browser_wait first if the page is still loading.")
        pt = await _centre(c, obj)
        if pt["is_file_input"]:
            raise FBError("That is an <input type=file>: clicking it would open a native picker nobody can drive. Use browser_set_files instead.")
        # Always intercept: if the click opens a file chooser, capture it instead of showing a native dialog.
        await c.send("Page.setInterceptFileChooserDialog", {"enabled": True})
        chooser = c.expect("Page.fileChooserOpened")
        x, y = pt["x"], pt["y"]
        await c.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        await c.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
        await c.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
        res: dict[str, Any] = {"clicked": True, "element": pt["tag"], "at": {"x": round(x), "y": round(y)}, "file_chooser_opened": False}
        if pt["covered_by"]:
            res["warning"] = f"Another element ({pt['covered_by']}) was on top at that point, so the click may have hit it instead."
        done, _ = await asyncio.wait({chooser}, timeout=0.8)
        if done:
            params = chooser.result()
            res["file_chooser_opened"] = True
            if paths:
                await c.send("DOM.setFileInputFiles", {"files": paths, "backendNodeId": params["backendNodeId"]})
                res["files_set"] = [os.path.basename(p) for p in paths]
            else:
                res["note"] = "The click opened a file chooser (suppressed). Repeat the click with files=[...] to fill it."
        elif paths:
            res["warning"] = "files were given but the click did not open a file chooser; nothing was set. Try browser_set_files."
        return res


async def wait(tab: str, selector_or_text: str, timeout: float) -> dict[str, Any]:
    async with _session(tab) as (c, t):
        q = json.dumps(selector_or_text)
        expr = f"""(() => {{ let sel = false; try {{ sel = !!fbFind({q}); }} catch (e) {{}}
          const txt = fbRoots(document, []).some(r => (((r.body ? r.body.innerText : r.textContent) || '').includes({q})));
          return sel ? 'selector' : (txt ? 'text' : null); }})()"""
        t0 = time.time()
        while True:
            hit = await _eval(c, expr)
            if hit:
                return {"found": True, "matched_as": hit, "waited_s": round(time.time() - t0, 1)}
            if time.time() - t0 >= timeout:
                return {"found": False, "waited_s": round(time.time() - t0, 1), "note": f"Neither a selector nor text matched {selector_or_text!r} within {timeout:.0f}s."}
            await asyncio.sleep(0.25)


# ---------------------------------------------------------------- observability for a headless browser (nothing is on screen)

async def navigate(tab: str, url: str, timeout: float = 30) -> dict[str, Any]:
    if urllib.parse.urlparse(url).scheme not in ("http", "https", "file", "about"):
        raise FBError("url must be http(s):// or file://.")
    async with _session(tab) as (c, t):
        await c.send("Page.enable")
        loaded = c.expect("Page.loadEventFired")
        r = await c.send("Page.navigate", {"url": url})
        if r.get("errorText"):
            raise FBError(f"navigation failed: {r['errorText']}")
        if not r.get("isDownload"):
            try:
                await asyncio.wait_for(loaded, timeout)
            except TimeoutError:
                return {"navigated": True, "loaded": False, "note": f"load event did not fire within {timeout:.0f}s; use browser_wait for what you need."}
        info = await _eval(c, "({url: location.href, title: document.title})")
        return {"navigated": True, "loaded": True, **info}


async def page_text(tab: str, selector: str | None, max_chars: int) -> dict[str, Any]:
    async with _session(tab) as (c, t):
        if selector:
            obj = await _find(c, selector)
            if not obj:
                raise FBError(f"selector {selector!r} matched nothing.")
            txt = await _call(c, obj, "function(){ return this.innerText !== undefined ? this.innerText : this.textContent; }")
        else:
            txt = await _eval(c, "(document.body ? document.body.innerText : '')")
        info = await _eval(c, "({url: location.href, title: document.title})")
        txt = txt or ""
        return {**info, "text": txt[:max_chars], "chars": len(txt), "truncated": len(txt) > max_chars}


async def screenshot(tab: str, path: str | None, full_page: bool) -> dict[str, Any]:
    import base64
    if path:
        out = local_path(path)
        if out.exists():
            raise FBError(f"{out} already exists; pick another path (filebridge never overwrites).")
    else:
        out = STATE_DIR / "screenshots" / f"{time.strftime('%Y%m%d-%H%M%S')}-{tab[:8]}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    async with _session(tab) as (c, t):
        params: dict[str, Any] = {"format": "png"}
        if full_page:
            m = await c.send("Page.getLayoutMetrics")
            size = m.get("cssContentSize") or m["contentSize"]
            params.update({"captureBeyondViewport": True, "clip": {"x": 0, "y": 0, "width": size["width"], "height": size["height"], "scale": 1}})
        shot = await c.send("Page.captureScreenshot", params, timeout=60)
    out.write_bytes(base64.b64decode(shot["data"]))
    return {"path": str(out), "bytes": out.stat().st_size, "note": "View it with an image-capable reader; the browser is headless so this is the only way to see the page."}


async def type_text(tab: str, selector: str, text: str, clear: bool, press_enter: bool) -> dict[str, Any]:
    async with _session(tab) as (c, t):
        await c.send("Page.bringToFront")
        obj = await _find(c, selector)
        if not obj:
            raise FBError(f"selector {selector!r} matched nothing.")
        await _call(c, obj, "function(clear){ this.scrollIntoView({block: 'center'}); this.focus(); if (clear) { if (this.select) this.select(); else document.execCommand('selectAll'); } }", [clear])
        if clear:
            await c.send("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
            await c.send("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})
        if text:
            await c.send("Input.insertText", {"text": text})
        if press_enter:
            for typ in ("keyDown", "keyUp"):
                await c.send("Input.dispatchKeyEvent", {"type": typ, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"})
        value = await _call(c, obj, "function(){ return this.value !== undefined ? this.value : this.textContent; }")
        return {"typed": len(text), "field_now": value if value is None or len(str(value)) <= 200 else str(value)[:200] + "…"}
