"""v0.2 headless-browser checks: browser_snapshot refs (set_files / drop / click / type by ref, stale and unknown refs),
verify_text / verify_selector, NotSignedIn on a sign-in page, the browser_upload_own snippet run inside a real page
(fills the input, LinkUsed, FetchBlocked, verify_snippet), and attach mode against a separate headless Chrome
(http and ws endpoints, loopback-only, close only detaches). Nothing here shows a window."""
import asyncio, hashlib, http.server, json, os, shutil, subprocess, sys, tempfile, threading, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import session
from filebridge import browser as fb

HERE = Path(__file__).parent
W = HERE / "_work" / "test8"
PORT, ATTACH_PORT = 8765, 9333
res: dict[str, bool] = {}


def serve():
    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            name = {"/frame2.html": "test2_frame2.html", "/frame.html": "test2_frame.html", "/login": "test8_login.html"}.get(path.split("?")[0], "test2_page.html")
            return str(HERE / name)
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def check(label, ok, detail=""):
    res[label] = bool(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({str(detail)[:300]})" if detail and not ok else ""))


async def call(s, name, **args):
    r = await s.call_tool(name, args)
    sc = r.structured_content if r.structured_content is not None else json.loads(r.content[0].text)
    return r.is_error, sc


async def page_log(tab):
    async with fb._session(tab) as (c, t):
        return await fb._eval(c, "[...document.querySelectorAll('#log li')].map(li => li.textContent)")


async def run_js(tab, code):
    """Run a snippet the way a browser extension's JavaScript tool does: top-level await, last expression returned."""
    async with fb._session(tab) as (c, t):
        r = await c.send("Runtime.evaluate", {"expression": code, "replMode": True, "awaitPromise": True, "returnByValue": True}, timeout=120)
        return r["result"].get("value")


def logged(entries, src, name, size, tail):
    return any(e == f"{src}|{name}|{size}|{tail}" for e in entries)


async def own_mode(f, size, tail):
    async with session() as s:
        _, r = await call(s, "browser_launch", url=f"http://127.0.0.1:{PORT}/")
        tab = [t["id"] for t in r["tabs"] if t["url"].startswith(f"http://127.0.0.1:{PORT}")][-1]
        await call(s, "browser_wait", tab=tab, selector_or_text="#dropzone", timeout=15)

        print("\n[browser_snapshot]")
        e, snap = await call(s, "browser_snapshot", tab=tab)
        els = snap.get("elements", [])
        print("   ", snap.get("count"), "elements, e.g.", els[:3])
        by = lambda pred: next((x for x in els if pred(x)), None)
        file_el = by(lambda x: x.get("file_input") and x["name"] == "pick (label)")
        zone = by(lambda x: x.get("drop_zone") and "drop files here" in (x.get("text") or x["name"]))
        button = by(lambda x: x["role"] == "button" and "Upload file" in (x.get("text") or x["name"]))
        title = by(lambda x: x["role"] == "textbox" and x["tag"] == "input")
        check("snapshot ok with refs", not e and snap["ok"] and all(x["ref"].startswith("e") for x in els), snap)
        check("hidden file input listed (hidden=true)", file_el and file_el.get("hidden"), file_el)
        check("file inputs in shadow root and same-origin iframe listed",
              by(lambda x: x.get("file_input") and x.get("in_shadow_root")) and by(lambda x: x.get("file_input") and x.get("in_iframe")))
        check("drop zone found through its drop listener", zone and zone["drop_zone"].startswith("listener"), zone)
        check("button and textbox listed", button and title, (button, title))

        print("\n[actions by ref + verification]")
        e, r = await call(s, "browser_set_files", tab=tab, paths=[str(f)], ref=file_el["ref"], verify_text="Upload complete", verify_timeout=20)
        check("set_files by ref, verified", not e and r["ok"] and r.get("verified") is True, r)
        check("  page got the bytes", logged(await page_log(tab), "input", f.name, size, tail))
        await call(s, "browser_click", tab=tab, selector="#reset")
        e, r = await call(s, "browser_drop_files", tab=tab, paths=[str(f)], ref=zone["ref"], verify_selector=".done", verify_timeout=20)
        check("drop by ref, verified by selector", not e and r["ok"] and r.get("verified") is True and r["method"] == "Input.dispatchDragEvent", r)
        check("  page got the bytes", logged(await page_log(tab), "drop-trusted", f.name, size, tail))
        await call(s, "browser_click", tab=tab, selector="#reset")
        e, r = await call(s, "browser_click", tab=tab, ref=button["ref"], files=[str(f)])
        await call(s, "browser_wait", tab=tab, selector_or_text="Upload complete", timeout=20)
        check("click by ref fills the file chooser", not e and r.get("files_set") == [f.name] and logged(await page_log(tab), "dynamic", f.name, size, tail), r)
        e, r = await call(s, "browser_type", tab=tab, ref=title["ref"], text="by ref", clear=True)
        check("type by ref", not e and r["field_now"] == "by ref", r)
        e, r = await call(s, "browser_set_files", tab=tab, paths=[str(f)], ref=button["ref"])
        check("set_files on a non-input ref -> InvalidArgument pointing to click/drop", e and r["code"] == "InvalidArgument" and "browser_click" in r["message"], r)
        e, r = await call(s, "browser_set_files", tab=tab, paths=[str(f)], ref=file_el["ref"], selector="#fileinput")
        check("selector and ref together -> InvalidArgument", e and r["code"] == "InvalidArgument", r)
        e, r = await call(s, "browser_click", tab=tab, ref="e99999")
        check("unknown ref -> SelectorNotFound", e and r["code"] == "SelectorNotFound", r)
        await call(s, "browser_click", tab=tab, selector="#reset")
        e, r = await call(s, "browser_set_files", tab=tab, paths=[str(f)], selector="#fileinput", verify_text="text that never shows", verify_timeout=1)
        check("verification that fails -> ok true, verified false", not e and r["ok"] and r.get("verified") is False and r.get("verify_note"), r)

        await call(s, "browser_navigate", tab=tab, url=f"http://127.0.0.1:{PORT}/?reloaded=1")
        e, r = await call(s, "browser_click", tab=tab, ref=button["ref"])
        check("ref after navigation -> stale, SelectorNotFound", e and r["code"] == "SelectorNotFound" and "stale" in r["message"], r)

        print("\n[NotSignedIn]")
        e, nav = await call(s, "browser_navigate", tab=tab, url=f"http://127.0.0.1:{PORT}/login")
        check("navigate to a sign-in page warns NotSignedIn", not e and (nav.get("warning") or {}).get("code") == "NotSignedIn", nav)
        e, r = await call(s, "browser_set_files", tab=tab, paths=[str(f)])
        check("set_files on a sign-in page -> NotSignedIn", not e and r["code"] == "NotSignedIn" and r["ok"] is False, r)
        e, r = await call(s, "browser_drop_files", tab=tab, paths=[str(f)], selector="#dropzone")
        check("drop with missing selector on a sign-in page -> NotSignedIn", e and r["code"] == "NotSignedIn", r)
        e, r = await call(s, "browser_click", tab=tab, selector="#nothing-here")
        check("click with missing selector on a sign-in page -> NotSignedIn", e and r["code"] == "NotSignedIn", r)
        await call(s, "browser_navigate", tab=tab, url=f"http://127.0.0.1:{PORT}/")
        e, r = await call(s, "browser_click", tab=tab, selector="#nothing-here")
        check("missing selector on a normal page -> SelectorNotFound", e and r["code"] == "SelectorNotFound", r)
        e3, r3 = await call(s, "browser_wait", tab="no-such-tab-anywhere", selector_or_text="x", timeout=1)
        check("unknown tab -> TabNotFound", e3 and r3["code"] == "TabNotFound", r3)

        print("\n[browser_upload_own snippet in a real page]")
        e, up = await call(s, "browser_upload_own", paths=[str(f)], selector="#fileinput", verify_text="Upload complete", verify_timeout=20)
        out = json.loads(await run_js(tab, up["snippet"]))
        check("snippet fills the input with the full file", out.get("ok") and out.get("files") == 1 and out.get("bytes") == size, out)
        ver = json.loads(await run_js(tab, up["verify_snippet"]))
        check("verify_snippet sees the upload", ver.get("verified") is True, ver)
        check("  page got the bytes", logged(await page_log(tab), "input", f.name, size, tail))
        await asyncio.sleep(0.6)
        e, st = await call(s, "serve_status", server_id=up["server_id"])
        check("serve_status done after the fetch", st["state"] == "done" and st["served"] == 1, st)
        again = json.loads(await run_js(tab, up["snippet"]))
        check("running the snippet again after the server stopped -> FetchBlocked (with the stopped-server hint)",
              again.get("ok") is False and again.get("code") == "FetchBlocked" and "already stopped" in again.get("message", ""), again)
        g = W / "second.txt"
        g.write_text("two")
        e, up2 = await call(s, "browser_upload_own", paths=[str(g), str(f)], selector="#fileinput")
        urllib.request.urlopen(up2["urls"][0]).read()
        used = json.loads(await run_js(tab, up2["snippet"]))
        check("a link already used -> LinkUsed", used.get("ok") is False and used.get("code") == "LinkUsed", used)
        await call(s, "serve_stop", server_id=up2["server_id"])
        e, up3 = await call(s, "browser_upload_own", paths=[str(g)], selector="#no-such-input")
        miss = json.loads(await run_js(tab, up3["snippet"]))
        check("snippet with a selector that matches nothing -> SelectorNotFound", miss.get("code") == "SelectorNotFound", miss)
        await call(s, "browser_close")


def attach_mode(f, size, tail):
    chosen = [p for n, p in fb.browsers() if n == "chrome"] or [p for _, p in fb.browsers()]
    prof = Path(tempfile.mkdtemp(prefix="fb-attach-"))
    proc = subprocess.Popen([chosen[0], f"--remote-debugging-port={ATTACH_PORT}", f"--user-data-dir={prof}", "--headless=new", "--no-first-run",
                             "--no-default-browser-check", f"http://127.0.0.1:{PORT}/"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(80):
            try:
                ver = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{ATTACH_PORT}/json/version", timeout=1).read())
                break
            except OSError:
                time.sleep(0.25)
        asyncio.run(_attach_checks(f, size, tail, ver["webSocketDebuggerUrl"], proc))
    finally:
        proc.terminate()
        proc.wait(timeout=20)
        shutil.rmtree(prof, ignore_errors=True)
        fb.ATTACH_STATE.unlink(missing_ok=True)


async def _attach_checks(f, size, tail, ws_url, proc):
    print("\n[attach mode, separate headless Chrome on :%d]" % ATTACH_PORT)
    async with session() as s:
        e, r = await call(s, "browser_launch", mode="attach", cdp_url="http://192.168.1.50:9222")
        check("non-loopback cdp_url refused", e and r["code"] == "InvalidArgument" and "loopback" in r["message"], r)
        e, r = await call(s, "browser_launch", mode="attach", cdp_url=f"http://127.0.0.1:{ATTACH_PORT}")
        check("attach via http endpoint", not e and r["mode"] == "attach" and any(t["url"].startswith(f"http://127.0.0.1:{PORT}") for t in r["tabs"]), r)
        tab = next(t["id"] for t in r["tabs"] if t["url"].startswith(f"http://127.0.0.1:{PORT}"))
        await call(s, "browser_wait", tab=tab, selector_or_text="#fileinput", timeout=15)
        e, r = await call(s, "browser_set_files", tab=tab, paths=[str(f)], selector="#fileinput", verify_text="Upload complete", verify_timeout=20)
        check("set_files in the attached browser", not e and r["ok"] and r.get("verified"), r)
        check("  page got the bytes", logged(await page_log(tab), "input", f.name, size, tail))
        e, r = await call(s, "browser_close")
        check("browser_close in attach mode only detaches", not e and r.get("detached") and not r.get("closed"), r)
        check("  the attached browser is still running", proc.poll() is None)
        e, r = await call(s, "browser_tabs")
        check("after detach, tools no longer reach it", e and r["code"] in ("BrowserNotRunning", "Failed"), r)
        e, r = await call(s, "browser_launch", mode="attach", cdp_url=ws_url, url=f"http://127.0.0.1:{PORT}/?ws=1")
        check("attach via a ws:// browser endpoint (the chrome://inspect toggle path) and open a tab", not e and any("?ws=1" in t["url"] for t in r["tabs"]), r)
        await call(s, "browser_close")
    check("  still running after the second detach", proc.poll() is None)


def main():
    shutil.rmtree(W, ignore_errors=True)
    W.mkdir(parents=True)
    f = W / "payload64.bin"
    f.write_bytes(os.urandom(64 * 1024 * 1024))
    size = f.stat().st_size
    with open(f, "rb") as fh:
        fh.seek(size - 1048576)
        tail = hashlib.sha256(fh.read()).hexdigest()
    srv = serve()
    try:
        asyncio.run(own_mode(f, size, tail))
        attach_mode(f, size, tail)
    finally:
        srv.shutdown()
        subprocess.run([str(HERE.parent / ".venv" / "bin" / "filebridge"), "browser", "close"], capture_output=True)
        shutil.rmtree(W, ignore_errors=True)
    print("\nSUMMARY:", sum(res.values()), "/", len(res), "passed")
    for k, v in res.items():
        if not v:
            print("  FAIL", k)
    sys.exit(0 if all(res.values()) else 1)


main()
