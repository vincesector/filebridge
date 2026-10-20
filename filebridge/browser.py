"""Lane 2: drive a browser over CDP so the browser reads local files by path.

Two modes:

- own (default): a DEDICATED profile (~/.filebridge/browser-profile) on 127.0.0.1:9229, headless unless visible.
  filebridge refuses that port unless the listener was started with our --user-data-dir and binds loopback only.
- attach: a Chrome/Brave the user started with --remote-debugging-port (or Chrome's chrome://inspect remote-debugging
  toggle). Loopback endpoints only. filebridge never launches or closes a browser in this mode; close only detaches.
"""

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from . import policy
from .cdp import CDP, CDPError, Scoped, ahttp_json
from .common import BROWSER_PROFILE, CDP_PORT, STATE_DIR, FBError, find_tool, local_path, run_capture

OWN_BASE = f"http://127.0.0.1:{CDP_PORT}"
ATTACH_STATE = STATE_DIR / "attach.json"
REFS_DIR = STATE_DIR / "refs"
DEFAULT_ATTACH_URL = "http://127.0.0.1:9222"

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


# ---------------------------------------------------------------- browser discovery (macOS tested; Linux/Windows best effort, untested)

def browsers() -> list[tuple[str, str]]:
    """(name, executable) of installed Brave/Chrome-family browsers, Brave first."""
    found: list[tuple[str, str]] = []
    if sys.platform == "darwin":
        for name, app in (("brave", "Brave Browser.app/Contents/MacOS/Brave Browser"), ("chrome", "Google Chrome.app/Contents/MacOS/Google Chrome")):
            for base in ("/Applications", str(Path.home() / "Applications")):
                found.append((name, f"{base}/{app}"))
    elif sys.platform == "win32":
        bases = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        for name, rel in (("brave", r"BraveSoftware\Brave-Browser\Application\brave.exe"), ("chrome", r"Google\Chrome\Application\chrome.exe")):
            found += [(name, os.path.join(b, rel)) for b in bases if b]
    else:
        for name, cmds in (("brave", ("brave-browser", "brave", "brave-browser-stable")),
                           ("chrome", ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"))):
            found += [(name, w) for w in map(shutil.which, cmds) if w]
        found += [("brave", "/opt/brave.com/brave/brave"), ("chrome", "/opt/google/chrome/chrome"), ("chrome", "/snap/bin/chromium")]
    return [(n, p) for n, p in found if os.path.isfile(p) and os.access(p, os.X_OK)]


# ---------------------------------------------------------------- own-mode lifecycle

async def _version(base: str = OWN_BASE) -> dict | None:
    try:
        v = await ahttp_json(base, "/json/version", timeout=2)
        return v if isinstance(v, dict) else None
    except Exception:
        return None


async def _listener(port: int = CDP_PORT) -> dict | None:
    """Who listens on a TCP port: {'pid', 'bind', 'cmd'} or None."""
    if sys.platform == "win32":
        ps = find_tool("powershell", ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",))
        script = ("$c = @(Get-NetTCPConnection -LocalPort $args[0] -State Listen -ErrorAction SilentlyContinue); if ($c.Count) { $p = $c[0].OwningProcess;"
                  " @{pid=$p; bind=@($c | ForEach-Object { \"$($_.LocalAddress):$($_.LocalPort)\" });"
                  " cmd=(Get-CimInstance Win32_Process -Filter \"ProcessId=$p\").CommandLine} | ConvertTo-Json -Compress }")
        _, out, _ = await run_capture([ps, "-NoProfile", "-NonInteractive", "-Command", script, str(port)], timeout=20)
        if not out.strip():
            return None
        info = json.loads(out)
        return {"pid": int(info["pid"]), "bind": list(info.get("bind") or []), "cmd": info.get("cmd") or ""}
    rc, out, _ = await run_capture([find_tool("lsof", ("/usr/sbin/lsof", "/usr/bin/lsof")), "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpn"], timeout=10)
    pid, binds = None, []
    for ln in out.splitlines():
        if ln.startswith("p"):
            pid = int(ln[1:])
        elif ln.startswith("n"):
            binds.append(ln[1:])
    if pid is None:
        return None
    _, cmd, _ = await run_capture([find_tool("ps", ("/bin/ps",)), "-p", str(pid), "-o", "command="], timeout=5)
    return {"pid": pid, "bind": binds, "cmd": cmd.strip()}


def _loopback_bind(b: str) -> bool:
    return b.startswith(("127.0.0.1:", "[::1]:", "::1:", "localhost:"))


async def _assert_ours() -> dict:
    info = await _listener()
    if not info:
        raise FBError("Nothing is listening on the automation port. Call browser_launch first.", "BrowserNotRunning")
    if f"--user-data-dir={BROWSER_PROFILE}" not in info["cmd"]:
        raise FBError(f"Port {CDP_PORT} is held by a process that was NOT started with the filebridge profile ({info['cmd'][:120]}). Refusing to attach.")
    if not all(_loopback_bind(b) for b in info["bind"]):
        raise FBError(f"Debug port is bound to {info['bind']}, not loopback only. Refusing to use it; close that browser.")
    return info


async def _pid_running(pid: int) -> bool:
    if sys.platform == "win32":
        return False  # untested platform: rely on the DevTools port going away
    _, out, _ = await run_capture([find_tool("ps", ("/bin/ps",)), "-p", str(pid), "-o", "stat="], timeout=5)
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
    subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, start_new_session=os.name == "posix")


def _check_url(url: str | None) -> None:
    if url and urllib.parse.urlparse(url).scheme not in ("http", "https", "file", "about"):
        raise FBError("url must be http(s):// or file://.", "InvalidArgument")


async def launch(url: str | None, browser: str = "auto", mode: str = "headless", cdp_url: str | None = None) -> dict[str, Any]:
    _check_url(url)
    if mode == "attach":
        return await _attach(url, cdp_url)
    if mode not in ("headless", "visible"):
        raise FBError("mode must be 'headless', 'visible' or 'attach'.", "InvalidArgument")
    visible = mode == "visible"
    ATTACH_STATE.unlink(missing_ok=True)
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
            await ahttp_json(OWN_BASE, "/json/new?" + urllib.parse.quote(url, safe=":/?&=#%@+,;~!$'()*"), method="PUT", timeout=10)
    else:
        if await _listener():
            raise FBError(f"Port {CDP_PORT} is already in use by another process. Free it or stop that process first.")
        chosen = [(n, p) for n, p in browsers() if browser in ("auto", n)]
        if not chosen:
            raise FBError(f"No usable browser for browser={browser!r}. Install Brave or Chrome (searched the standard install locations and PATH).", "NotInstalled")
        name, exe = chosen[0]
        BROWSER_PROFILE.mkdir(parents=True, exist_ok=True, mode=0o700)
        _spawn(_launch_args(exe, url, visible))
        for _ in range(100):
            await asyncio.sleep(0.25)
            ver = await _version()
            if ver:
                break
        if not ver:
            raise FBError(f"{name} did not expose DevTools on port {CDP_PORT} within 25s. See {STATE_DIR / 'browser.log'}.", "Timeout")
        await _drop_restored_tabs(url)
    info = await _assert_ours()
    return {"running": True, "mode": mode, "reused_existing": reused, "restarted_for_mode_switch": restarted,
            "app": "brave" if "brave" in info["cmd"].lower() else "chrome", "browser": ver.get("Browser"), "port": CDP_PORT,
            "profile": str(BROWSER_PROFILE), "pid": info["pid"], "tabs": await tabs(),
            "note": ("A browser window is open on screen: sign in to the site(s) now, then call browser_launch() again (headless) so agents can use the saved session."
                     if visible else "Headless: nothing is shown on screen. Sessions saved in the profile are used.")}


async def _drop_restored_tabs(url: str | None) -> None:
    """A fresh start restores last session's tabs, so ids and url matches pile up run after run. Keep one tab."""
    prev, stable, pages = None, 0, []
    for _ in range(16):  # wait until the restored tab set stops changing (max ~6s)
        pages = [t for t in await ahttp_json(OWN_BASE, "/json/list") if t.get("type") == "page" and not t.get("url", "").startswith("devtools://")]
        ids = sorted(t["id"] for t in pages)
        stable = stable + 1 if ids == prev and ids else 0
        if stable >= 2:
            break
        prev = ids
        await asyncio.sleep(0.4)
    keep = next((t for t in pages if url and t.get("url", "").rstrip("/") == url.rstrip("/")), pages[0] if pages else None)
    for t in pages:
        if keep and t["id"] != keep["id"]:
            await ahttp_json(OWN_BASE, f"/json/close/{t['id']}")


async def close() -> dict[str, Any]:
    if _attach_state():
        ATTACH_STATE.unlink(missing_ok=True)
        return {"closed": False, "detached": True, "note": "Attach mode: detached from your browser. filebridge never closes a browser it did not start."}
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


# ---------------------------------------------------------------- attach mode

def _attach_state() -> dict | None:
    try:
        return json.loads(ATTACH_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _is_loopback_url(u: str) -> bool:
    return (urllib.parse.urlparse(u).hostname or "") in ("127.0.0.1", "localhost", "::1")


def _devtools_active_ports() -> list[str]:
    """ws:// endpoints from DevToolsActivePort files of the default Chrome/Brave profiles (chrome://inspect toggle)."""
    home = Path.home()
    if sys.platform == "darwin":
        dirs = [home / "Library/Application Support/Google/Chrome", home / "Library/Application Support/BraveSoftware/Brave-Browser"]
    elif sys.platform == "win32":
        la = Path(os.environ.get("LOCALAPPDATA", home))
        dirs = [la / "Google/Chrome/User Data", la / "BraveSoftware/Brave-Browser/User Data"]
    else:
        dirs = [home / ".config/google-chrome", home / ".config/chromium", home / ".config/BraveSoftware/Brave-Browser"]
    out = []
    for d in dirs:
        try:
            port, path = (d / "DevToolsActivePort").read_text().splitlines()[:2]
            out.append(f"ws://127.0.0.1:{int(port)}{path}")
        except (OSError, ValueError):
            continue
    return out


async def _attach_ws(cdp_url: str) -> str:
    """Browser websocket URL behind a loopback DevTools endpoint (http://host:port or ws://.../devtools/browser/...)."""
    if not _is_loopback_url(cdp_url):
        raise FBError(f"Refusing to attach to {cdp_url}: only loopback endpoints (127.0.0.1, localhost, ::1) are allowed.", "InvalidArgument")
    p = urllib.parse.urlparse(cdp_url)
    if p.scheme in ("ws", "wss"):
        ws = cdp_url
    elif p.scheme in ("http", "https"):
        ver = await _version(f"{p.scheme}://{p.netloc}")
        if not ver or "webSocketDebuggerUrl" not in ver:
            raise FBError(f"No DevTools endpoint answers at {cdp_url}. Start the browser with --remote-debugging-port (see the README, 'Attach to your browser').", "BrowserNotRunning")
        ws = ver["webSocketDebuggerUrl"]
    else:
        raise FBError("cdp_url must be http://127.0.0.1:PORT or ws://127.0.0.1:PORT/devtools/browser/ID.", "InvalidArgument")
    if not _is_loopback_url(ws):
        raise FBError(f"The endpoint advertised a non-loopback websocket ({ws}); refusing.")
    port = urllib.parse.urlparse(ws).port
    if port and sys.platform != "win32":
        try:
            info = await _listener(port)
        except FBError:  # no lsof (some Linux installs): Chrome binds loopback unless told otherwise
            info = None
        if info and not all(_loopback_bind(b) for b in info["bind"]):
            raise FBError(f"DevTools port {port} is bound to {info['bind']}, not loopback only, so anyone on the network could drive that browser. Restart it bound to 127.0.0.1.")
    return ws


async def _attach(url: str | None, cdp_url: str | None) -> dict[str, Any]:
    candidates = [cdp_url] if cdp_url else [DEFAULT_ATTACH_URL, *_devtools_active_ports()]
    errors, ws, used = [], None, None
    for cand in candidates:
        try:
            ws = await _attach_ws(cand)
            c = await CDP.connect(ws)
            try:
                ver = await c.send("Browser.getVersion", timeout=60)  # Chrome's toggle mode asks the user to allow this connection
                if url:
                    await c.send("Target.createTarget", {"url": url})
            finally:
                await c.close()
            used = cand
            break
        except Exception as e:  # refused/closed websocket, denied toggle prompt, nothing listening
            if cdp_url and isinstance(e, FBError):
                raise
            errors.append(f"{cand}: {e}")
            ws = None
    if not ws:
        raise FBError("Could not attach to a running browser. " + " | ".join(errors) +
                      " Start Chrome/Brave with --remote-debugging-port=9222 and a separate --user-data-dir, or enable chrome://inspect/#remote-debugging.",
                      "BrowserNotRunning")
    ATTACH_STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ATTACH_STATE.write_text(json.dumps({"mode": "attach", "cdp_url": used}))
    return {"running": True, "mode": "attach", "cdp_url": used, "browser": ver.get("product"), "tabs": await tabs(),
            "note": "Attached to your running browser. filebridge will not launch or close it; browser_close only detaches. "
                    "Actions bring the target tab to the front."}


# ---------------------------------------------------------------- connections and tabs

async def _browser_ws() -> str:
    st = _attach_state()
    if st:
        return await _attach_ws(st["cdp_url"])
    ver = await _version()
    if not ver:
        raise FBError("The automation browser is not running. Call browser_launch first.", "BrowserNotRunning")
    await _assert_ours()
    return ver["webSocketDebuggerUrl"]


def browser_label() -> str:
    return "your attached browser" if _attach_state() else "the automation browser"


async def _pages(c: CDP) -> list[dict]:
    infos = (await c.send("Target.getTargets"))["targetInfos"]
    return [{"id": t["targetId"], "title": t.get("title", ""), "url": t.get("url", "")} for t in infos
            if t.get("type") == "page" and not t.get("url", "").startswith(("devtools://", "chrome-extension://"))]


async def tabs() -> list[dict[str, str]]:
    c = await CDP.connect(await _browser_ws())
    try:
        return await _pages(c)
    finally:
        await c.close()


def _match_tab(pages: list[dict], tab: str) -> dict:
    exact = [t for t in pages if t["id"] == tab]
    if exact:
        return exact[0]
    fuzzy = [t for t in pages if tab.lower() in t["url"].lower() or tab.lower() in t["title"].lower()]
    if len(fuzzy) == 1:
        return fuzzy[0]
    listing = "; ".join(f"{t['id'][:8]}… {t['title'][:40]} <{t['url'][:60]}>" for t in pages[:30])
    why = "matches several tabs" if fuzzy else "matches no tab"
    raise FBError(f"tab {tab!r} {why}. Pass a full id from browser_tabs() or a unique part of the url/title. Open tabs: {listing}", "TabNotFound")


@asynccontextmanager
async def _session(tab: str):
    c = await CDP.connect(await _browser_ws())
    try:
        t = _match_tab(await _pages(c), tab)
        sid = (await c.send("Target.attachToTarget", {"targetId": t["id"], "flatten": True}))["sessionId"]
        yield Scoped(c, sid), t
    finally:
        try:
            await c.close()
        except Exception:
            pass


# ---------------------------------------------------------------- helpers

async def _check_files(paths: list[str] | None, tab: str) -> list[str]:
    return await policy.check_sources(paths or [], action="upload", destination=f"{browser_label()} (tab {tab!r})", files_only=True)


async def _eval(c, expr: str, *, by_value: bool = True) -> Any:
    r = await c.send("Runtime.evaluate", {"expression": FB_JS + expr, "returnByValue": by_value, "awaitPromise": True})
    if "exceptionDetails" in r:
        d = r["exceptionDetails"]
        raise FBError("Page script error: " + (d.get("exception", {}).get("description") or d.get("text", "?"))[:300])
    return r["result"] if not by_value else r["result"].get("value")


async def _find(c, selector: str) -> str | None:
    """objectId of the first match across the document, open shadow roots and same-origin iframes."""
    res = await _eval(c, f"fbFind({json.dumps(selector)})", by_value=False)
    if res.get("subtype") == "null" or "objectId" not in res:
        return None
    return res["objectId"]


async def _call(c, object_id: str, fn: str, args: list | None = None) -> Any:
    r = await c.send("Runtime.callFunctionOn", {
        "objectId": object_id, "functionDeclaration": fn, "returnByValue": True, "awaitPromise": True,
        "arguments": [a if isinstance(a, dict) else {"value": a} for a in (args or [])]})
    if "exceptionDetails" in r:
        raise FBError("Page script error: " + (r["exceptionDetails"].get("exception", {}).get("description") or "?")[:300])
    return r["result"].get("value")


async def _candidates(c) -> list[dict]:
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


_LOGIN_JS = """(() => { const path = location.pathname + location.search;
  const urlHit = /(^|[\\/_.-])(login|log-in|signin|sign-in|sign_in|signup|auth|sso|oauth2?)([\\/_.?=-]|$)/i.test(path)
    || /^(accounts|login|auth|signin|sso)\\./i.test(location.hostname);
  const pw = fbRoots(document, []).some(r => r.querySelector('input[type=password]'));
  return (urlHit || pw) ? {url: location.href, password_field: pw, login_url: urlHit} : null; })()"""


async def _login_wall(c) -> dict | None:
    try:
        return await _eval(c, _LOGIN_JS)
    except FBError:
        return None


async def _not_found(c, what: str) -> FBError:
    """SelectorNotFound, or NotSignedIn when the page looks like a sign-in wall."""
    wall = await _login_wall(c)
    if wall:
        return FBError(f"{what}, and the page looks like a sign-in page ({wall['url']}). In own mode, sign in once with "
                       "browser_launch(mode='visible'), or use browser_upload_own in your own signed-in browser.", "NotSignedIn", login=wall)
    return FBError(f"{what}. Use browser_snapshot to see what is on the page, or browser_wait if it is still loading.", "SelectorNotFound")


_VERIFY_JS = """(() => {{ const sel = {sel}, txt = {txt}; let okS = true, okT = true;
  if (sel !== null) {{ try {{ okS = !!fbFind(sel); }} catch (e) {{ okS = false; }} }}
  if (txt !== null) okT = fbRoots(document, []).some(r => (((r.body ? r.body.innerText : r.textContent) || '').includes(txt)));
  return okS && okT; }})()"""


async def _poll(c, selector: str | None, text: str | None, timeout: float) -> tuple[bool, float]:
    expr = _VERIFY_JS.format(sel=json.dumps(selector), txt=json.dumps(text))
    t0 = time.time()
    while True:
        if await _eval(c, expr):
            return True, round(time.time() - t0, 1)
        if time.time() - t0 >= timeout:
            return False, round(time.time() - t0, 1)
        await asyncio.sleep(0.25)


async def _verify(c, res: dict, text: str | None, selector: str | None, timeout: float) -> dict:
    if text is None and selector is None:
        return res
    ok, waited = await _poll(c, selector, text, timeout)
    res.update({"verified": ok, "verify_waited_s": waited})
    if not ok:
        res["verify_note"] = f"Within {timeout:.0f}s the page did not show " + " and ".join(
            x for x in (selector is not None and f"selector {selector!r}", text is not None and f"text {text!r}") if x) + ". Check with browser_text or browser_screenshot."
    return res


def _one_target(selector: str | None, ref: str | None, required: bool = True) -> None:
    if selector and ref:
        raise FBError("Pass either selector or ref, not both.", "InvalidArgument")
    if required and not (selector or ref):
        raise FBError("Pass a selector or a ref from browser_snapshot.", "InvalidArgument")


# ---------------------------------------------------------------- element references (browser_snapshot)

_SNAPSHOT_JS = """(() => { const max = %d;
  const interactive = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],'
    + '[role=radio],[role=switch],[role=textbox],[role=combobox],[role=option],[contenteditable=""],[contenteditable=true],[ondrop],[ondragover],[tabindex]:not([tabindex="-1"])';
  const zoneRx = /(drag|drop)\\b.{0,40}\\b(file|files|here|photo|photos|video|videos|image|images|media)|\\bupload|browse (your )?files|choose (a )?files?|select files?|arraste|solte|carregar/i;
  const implicit = el => { const t = el.tagName.toLowerCase();
    if (t === 'a') return 'link'; if (t === 'button' || t === 'summary') return 'button'; if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') { const ty = (el.type || 'text').toLowerCase();
      return ty === 'file' ? 'file input' : ['button', 'submit', 'reset', 'image'].includes(ty) ? 'button' : ty === 'checkbox' ? 'checkbox' : ty === 'radio' ? 'radio' : 'textbox'; }
    if (el.isContentEditable) return 'textbox'; return 'generic'; };
  const nameOf = el => { const a = el.getAttribute('aria-label'); if (a) return a;
    const lb = el.getAttribute('aria-labelledby'); if (lb) { const t = lb.split(/\\s+/).map(id => { const n = el.ownerDocument.getElementById(id); return n ? n.textContent : ''; }).join(' ').trim(); if (t) return t; }
    if (el.labels && el.labels.length) return [...el.labels].map(l => l.textContent).join(' ');
    return el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('alt') || (el.tagName === 'INPUT' ? el.value || '' : ''); };
  const clean = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  const els = [], info = [];
  for (const r of fbRoots(document, [])) {
    for (const el of r.querySelectorAll('*')) {
      if (els.length >= max) break;
      const isFile = el.tagName === 'INPUT' && el.type === 'file';
      const inter = isFile || el.matches(interactive);
      let zone = false;
      if (!inter) { const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim(); zone = !!own && zoneRx.test(own); }
      if (!inter && !zone) continue;
      const rc = el.getBoundingClientRect(), st = el.ownerDocument.defaultView.getComputedStyle(el);
      const visible = rc.width > 0 && rc.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
      if (!visible && !isFile) continue;
      const d = {role: el.getAttribute('role') || implicit(el), name: clean(nameOf(el)), tag: el.tagName.toLowerCase()};
      const text = clean(el.innerText); if (text && text !== d.name) d.text = text;
      if (isFile) { d.file_input = true; if (el.accept) d.accept = el.accept; if (el.multiple) d.multiple = true;
        if (!visible || st.opacity === '0' || rc.right <= 0 || rc.bottom <= 0) d.hidden = true; }
      if (zone || el.hasAttribute('ondrop') || el.hasAttribute('ondragover')) d.drop_zone = el.hasAttribute('ondrop') || el.hasAttribute('ondragover') ? 'attribute' : 'text';
      if (el.disabled) d.disabled = true;
      if (el.getRootNode() !== el.ownerDocument) d.in_shadow_root = true;
      if (el.ownerDocument !== document) d.in_iframe = true;
      els.push(el); info.push(d);
    }
  }
  window.__fbSnapInfo = info; return els; })()"""


def _refs_path(tab_id: str) -> Path:
    return REFS_DIR / f"{''.join(ch for ch in tab_id if ch.isalnum())[:64]}.json"


async def _object_ids(c, array_object_id: str) -> list[str]:
    props = (await c.send("Runtime.getProperties", {"objectId": array_object_id, "ownProperties": True}))["result"]
    idx = sorted((int(p["name"]), p["value"]["objectId"]) for p in props if p["name"].isdigit() and "objectId" in p.get("value", {}))
    return [o for _, o in idx]


async def _listeners(c, object_id: str) -> set[str]:
    try:
        r = await c.send("DOMDebugger.getEventListeners", {"objectId": object_id, "depth": 0})
    except CDPError:
        return set()
    return {lst["type"] for lst in r.get("listeners", [])}


async def snapshot(tab: str, max_items: int = 300) -> dict[str, Any]:
    async with _session(tab) as (c, t):
        arr = await _eval(c, _SNAPSHOT_JS % max(1, min(max_items, 2000)), by_value=False)
        info = await _eval(c, "window.__fbSnapInfo")
        await _eval(c, "delete window.__fbSnapInfo")
        obj_ids = await _object_ids(c, arr["objectId"])
        snap = secrets.token_hex(6)
        refs: dict[str, int] = {}
        items = []
        listener_checks = 0
        for i, (oid, d) in enumerate(zip(obj_ids, info), 1):
            backend = (await c.send("DOM.describeNode", {"objectId": oid}))["node"]["backendNodeId"]
            if d.get("drop_zone") == "text" and listener_checks < 20:
                # Upload text usually sits inside the real drop target: look for drop/dragover listeners up the tree.
                listener_checks += 1
                chain = await c.send("Runtime.callFunctionOn", {"objectId": oid, "functionDeclaration":
                                     "function(){ const out = []; let e = this; for (let i = 0; e && i < 5; i++, e = e.parentElement) out.push(e); return out; }"})
                for j, anc in enumerate(await _object_ids(c, chain["result"]["objectId"])):
                    if {"drop", "dragover"} & await _listeners(c, anc):
                        d["drop_zone"] = "listener" if j == 0 else f"listener on ancestor {j} up"
                        if j:
                            backend = (await c.send("DOM.describeNode", {"objectId": anc}))["node"]["backendNodeId"]
                        break
            ref = f"e{i}"
            refs[ref] = backend
            items.append({"ref": ref, **d})
        win = await _eval(c, "window", by_value=False)
        doc = await _eval(c, "document", by_value=False)
        page_drop = bool({"drop", "dragover"} & (await _listeners(c, win["objectId"]) | await _listeners(c, doc["objectId"])))
        await _eval(c, f"window.__fbSnap = {json.dumps(snap)}")
        page = await _eval(c, "({url: location.href, title: document.title})")
        wall = await _login_wall(c)
    REFS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    _refs_path(t["id"]).write_text(json.dumps({"tab": t["id"], "snap": snap, "url": page["url"], "refs": refs}))
    res = {"ok": True, "tab": t["id"], **page, "count": len(items), "truncated": len(items) >= max_items, "elements": items,
           "page_has_drop_listener": page_drop,
           "note": "Pass ref (e.g. 'e3') to browser_set_files / browser_drop_files / browser_click / browser_type. Refs are valid until the page navigates or reloads. "
                   "Cross-site iframes and closed shadow roots are not listed; browser_set_files without a selector still finds file inputs there."}
    if wall:
        res["warning"] = {"code": "NotSignedIn", "message": "This looks like a sign-in page.", **wall}
    return res


async def _ref_object(c, tab_id: str, ref: str) -> str:
    try:
        data = json.loads(_refs_path(tab_id).read_text())
    except (OSError, json.JSONDecodeError):
        raise FBError(f"No snapshot for this tab. Call browser_snapshot first, then use its refs.", "SelectorNotFound")
    if ref not in data["refs"]:
        raise FBError(f"ref {ref!r} is not in the last snapshot of this tab (it has e1..e{len(data['refs'])}).", "SelectorNotFound")
    if await _eval(c, "window.__fbSnap || null") != data["snap"]:
        raise FBError(f"ref {ref!r} is stale: the page navigated or reloaded since browser_snapshot. Take a new snapshot.", "SelectorNotFound")
    try:
        return (await c.send("DOM.resolveNode", {"backendNodeId": data["refs"][ref]}))["object"]["objectId"]
    except CDPError:
        raise FBError(f"ref {ref!r} no longer exists in the page (removed by the page). Take a new snapshot.", "SelectorNotFound")


async def _target(c, t: dict, selector: str | None, ref: str | None) -> str:
    if ref:
        return await _ref_object(c, t["id"], ref)
    obj = await _find(c, selector)
    if not obj:
        raise await _not_found(c, f"selector {selector!r} matched nothing (searched shadow roots and same-origin iframes)")
    return obj


_READ_FILES = "function(){return {n: this.files.length, files: [...this.files].map(f => ({name: f.name, size: f.size})), multiple: this.multiple, id: this.id, name: this.name}}"
_IS_FILE_INPUT = "function(){ return this.tagName === 'INPUT' && this.type === 'file' ? null : this.tagName.toLowerCase() + (this.id ? '#' + this.id : ''); }"


# ---------------------------------------------------------------- tools

async def _frame_scopes(c: Scoped) -> list[tuple[Any, str]]:
    """The tab plus every out-of-process iframe in it (cross-site frames the tab session cannot see into)."""
    await c.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
    await asyncio.sleep(0.4)
    return [(c, "main")] + [(Scoped(c.cdp, sid), ti.get("url", "")) for sid, ti in c.cdp.attached.items()
                            if ti.get("type") == "iframe" and ti.get("parent") == c.session_id]


async def set_files(tab: str, selector: str | None, paths: list[str], ref: str | None = None, verify_text: str | None = None,
                    verify_selector: str | None = None, verify_timeout: float = 30) -> dict[str, Any]:
    _one_target(selector, ref, required=False)
    files = await _check_files(paths, tab)
    async with _session(tab) as (c, t):
        err = None
        hit = None  # (scope, objectId)
        cands: list[dict] = []
        if ref:
            obj = await _ref_object(c, t["id"], ref)
            other = await _call(c, obj, _IS_FILE_INPUT)
            if other:
                raise FBError(f"ref {ref} is a <{other}>, not a file input. Use browser_click(ref={ref!r}, files=[...]) for an upload button "
                              f"or browser_drop_files(ref={ref!r}) for a drop zone.", "InvalidArgument")
            await c.send("DOM.setFileInputFiles", {"files": files, "objectId": obj})
            hit = (c, obj)
        scopes = [] if hit else await _frame_scopes(c)
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
            code = "SelectorNotFound"
            if err:
                code, reason = "Failed", f"matched an element but setting files failed: {err}"
            elif selector:
                reason = f"selector {selector!r} matched nothing"
            else:
                reason = f"{len(cands)} file inputs on the page; refusing to guess" if cands else "no <input type=file> found in the page, its shadow roots (open or closed) or iframes (incl. cross-site)"
            res = {"ok": False, "code": code, "message": reason, "candidates": cands,
                   "hint": ("Pick one of `candidates` via selector, or some sites only create the input on click: "
                            "use browser_click(tab, selector_of_upload_button, files=[...]) which captures the file chooser. browser_snapshot lists buttons and drop zones.")}
            if not cands and not err:
                wall = await _login_wall(c)
                if wall:
                    res.update({"code": "NotSignedIn", "message": reason + "; the page looks like a sign-in page", "login": wall})
            return res
        sc, obj = hit
        seen = await _call(sc, obj, _READ_FILES)
        want = {os.path.basename(p): os.path.getsize(p) for p in files}
        got = {f["name"]: f["size"] for f in seen["files"]}
        res = {"ok": want == got, "method": "DOM.setFileInputFiles", "tab": t["id"], "input": {"id": seen["id"], "name": seen["name"], "multiple": seen["multiple"]},
               "files_in_page": seen["files"], "sizes_match_disk": want == got,
               "note": "The browser reads the files from disk itself, so there is no size cap. 'change' and 'input' events were fired."}
        if want != got:
            res.update({"code": "Failed", "message": "The input does not hold the expected files (names/sizes differ from disk)."})
        return await _verify(c, res, verify_text, verify_selector, verify_timeout)


async def _centre(c, obj: str) -> dict:
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


async def drop_files(tab: str, paths: list[str], selector: str | None, x: float | None, y: float | None, method: str, ref: str | None = None,
                     verify_text: str | None = None, verify_selector: str | None = None, verify_timeout: float = 30) -> dict[str, Any]:
    _one_target(selector, ref, required=False)
    files = await _check_files(paths, tab)
    async with _session(tab) as (c, t):
        await c.send("Page.bringToFront")
        target = None
        if selector or ref:
            target = await _target(c, t, selector, ref)
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
                res = {"ok": True, "method": "Input.dispatchDragEvent", "trusted_events": all(s["trusted"] for s in seen), "at": {"x": x, "y": y},
                       "page_saw": seen, "note": "Real drag events with disk paths; no size cap."}
                return await _verify(c, res, verify_text, verify_selector, verify_timeout)
            if method == "cdp":
                return {"ok": False, "code": "Failed", "method": "Input.dispatchDragEvent", "page_saw": seen,
                        "message": "The page received a drop with no files (or no drop at all). Retry with method='auto' for the in-page fallback."}

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
        res = {"ok": n == len(files), "method": "synthetic-DataTransfer", "trusted_events": False, "at": {"x": x, "y": y}, "page_saw": seen,
               "note": "Fallback used: File objects are disk-backed (no size cap) but the drag events are synthetic (isTrusted=false); a site that checks isTrusted will ignore them."}
        return await _verify(c, res, verify_text, verify_selector, verify_timeout)


async def click(tab: str, selector: str | None, files: list[str] | None, ref: str | None = None) -> dict[str, Any]:
    _one_target(selector, ref)
    paths = await _check_files(files, tab) if files else None
    async with _session(tab) as (c, t):
        await c.send("Page.enable")
        await c.send("Page.bringToFront")
        obj = await _target(c, t, selector, ref)
        pt = await _centre(c, obj)
        if pt["is_file_input"]:
            raise FBError("That is an <input type=file>: clicking it would open a native picker nobody can drive. Use browser_set_files instead.", "InvalidArgument")
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
                return {"ok": True, "found": True, "matched_as": hit, "waited_s": round(time.time() - t0, 1)}
            if time.time() - t0 >= timeout:
                return {"ok": False, "found": False, "code": "Timeout", "waited_s": round(time.time() - t0, 1),
                        "message": f"Neither a selector nor text matched {selector_or_text!r} within {timeout:.0f}s."}
            await asyncio.sleep(0.25)


# ---------------------------------------------------------------- observability for a headless browser (nothing is on screen)

async def navigate(tab: str, url: str, timeout: float = 30) -> dict[str, Any]:
    _check_url(url)
    async with _session(tab) as (c, t):
        await c.send("Page.enable")
        loaded = c.expect("Page.loadEventFired")
        r = await c.send("Page.navigate", {"url": url})
        if r.get("errorText"):
            raise FBError(f"navigation failed: {r['errorText']}")
        res: dict[str, Any] = {"ok": True, "navigated": True, "loaded": True}
        if not r.get("isDownload"):
            try:
                await asyncio.wait_for(loaded, timeout)
            except TimeoutError:
                return {"ok": False, "code": "Timeout", "navigated": True, "loaded": False,
                        "message": f"load event did not fire within {timeout:.0f}s; use browser_wait for what you need."}
        res.update(await _eval(c, "({url: location.href, title: document.title})"))
        wall = await _login_wall(c)
        if wall:
            res["warning"] = {"code": "NotSignedIn", "message": "This looks like a sign-in page.", **wall}
        return res


async def page_text(tab: str, selector: str | None, max_chars: int) -> dict[str, Any]:
    async with _session(tab) as (c, t):
        if selector:
            obj = await _find(c, selector)
            if not obj:
                raise await _not_found(c, f"selector {selector!r} matched nothing")
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
            raise FBError(f"{out} already exists; pick another path (filebridge never overwrites).", "InvalidArgument")
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


async def type_text(tab: str, selector: str | None, text: str, clear: bool, press_enter: bool, ref: str | None = None) -> dict[str, Any]:
    _one_target(selector, ref)
    async with _session(tab) as (c, t):
        await c.send("Page.bringToFront")
        obj = await _target(c, t, selector, ref)
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
