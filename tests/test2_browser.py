"""Test 2: browser_set_files / browser_drop_files / browser_click against a local page, with a 300 MB file."""
import asyncio, hashlib, http.server, os, subprocess, sys, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import call, session
from filebridge import browser as fb

HERE = Path(__file__).parent
WORK = Path.home() / ".filebridge" / "test-src"
BIG = WORK / "big300.bin"
SIZE = 300 * 1024 * 1024
PORT = 8765


def serve():
    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            return str(HERE / ("test2_frame2.html" if path.startswith("/frame2.html") else "test2_frame.html" if path.startswith("/frame.html") else "test2_page.html"))
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def daily_brave_pids():
    out = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return sorted(int(l.split(None, 1)[0]) for l in out.splitlines()
                  if "Brave Browser.app/Contents/MacOS/Brave Browser" in l and "filebridge" not in l and "--type=" not in l)


async def page_log(tab):
    async with fb._session(tab) as (c, t):
        return await fb._eval(c, "[...document.querySelectorAll('#log li')].map(li => li.textContent)")


async def reset(s, tab):
    await call(s, "browser_click", tab=tab, selector="#reset")


def check(entries, src, tail_sha):
    hit = [e.split("|") for e in entries if e.startswith(src + "|")]
    ok = bool(hit) and hit[-1][1] == "big300.bin" and int(hit[-1][2]) == SIZE and hit[-1][3] == tail_sha
    print(f"   page reported: {hit[-1] if hit else None}  -> {'MATCH' if ok else 'MISMATCH'}")
    return ok


async def main():
    WORK.mkdir(parents=True, exist_ok=True)
    with open(BIG, "wb") as f:
        for _ in range(SIZE // (50 * 1024 * 1024)):
            f.write(os.urandom(50 * 1024 * 1024))
    with open(BIG, "rb") as f:
        f.seek(SIZE - 1048576)
        tail_sha = hashlib.sha256(f.read()).hexdigest()
    print(f"test file {BIG} = {BIG.stat().st_size} bytes ({BIG.stat().st_size/1048576:.0f} MiB), tail sha256 {tail_sha[:16]}…")
    srv = serve()
    before = daily_brave_pids()
    print("daily Brave main pids before:", before)
    results = {}
    try:
        async with session() as s:
            r = await call(s, "browser_launch", url=f"http://127.0.0.1:{PORT}/")
            print("launch:", {k: r[k] for k in ("running", "reused_existing", "browser", "port", "profile", "pid")}, "tabs:", r["tabs"])
            tab = [t["id"] for t in r["tabs"] if t["url"].startswith(f"http://127.0.0.1:{PORT}")][-1]  # profile restores older tabs too
            await call(s, "browser_wait", tab=tab, selector_or_text="#dropzone", timeout=15)

            print("\n[set_files] selector #fileinput (hidden, off-screen input)")
            t0 = time.time()
            r = await call(s, "browser_set_files", tab=tab, selector="#fileinput", paths=[str(BIG)])
            print("  ", {k: r.get(k) for k in ("ok", "method", "sizes_match_disk")}, f"{time.time()-t0:.2f}s")
            w = await call(s, "browser_wait", tab=tab, selector_or_text="Upload complete", timeout=30)
            print("   wait:", w)
            results["set_files hidden input"] = check(await page_log(tab), "input", tail_sha)

            print("\n[set_files] no selector, several inputs on the page -> candidates, nothing attached")
            await reset(s, tab)
            r = await call(s, "browser_set_files", tab=tab, selector=None, paths=[str(BIG)])
            print("  ", {"ok": r["ok"], "reason": r["reason"], "candidates": [(c["selector_hint"], c["multiple"]) for c in r["candidates"]]})
            results["candidates returned"] = (not r["ok"]) and len(r["candidates"]) >= 5

            print("\n[set_files] selector matches nothing -> candidates")
            r = await call(s, "browser_set_files", tab=tab, selector="#does-not-exist", paths=[str(BIG)])
            print("  ", {"ok": r["ok"], "reason": r["reason"], "n_candidates": len(r["candidates"])})
            results["miss returns candidates"] = (not r["ok"]) and len(r["candidates"]) >= 5

            for label, sel, src in (("open shadow root", "#shadowinput", "shadow"), ("closed shadow root", "#closedinput", "closed"),
                                    ("same-origin iframe", "#frameinput", "iframe"), ("cross-site iframe (out-of-process)", "#xinput", "xframe")):
                print(f"\n[set_files] {label}: {sel}")
                await reset(s, tab)
                r = await call(s, "browser_set_files", tab=tab, selector=sel, paths=[str(BIG)])
                print("  ", {k: r.get(k) for k in ("ok", "method", "sizes_match_disk")})
                await call(s, "browser_wait", tab=tab, selector_or_text="Upload complete", timeout=30)
                results[f"set_files {label}"] = check(await page_log(tab), src, tail_sha)

            for method in ("auto", "synthetic"):
                print(f"\n[drop_files] #dropzone, method={method}")
                await reset(s, tab)
                t0 = time.time()
                r = await call(s, "browser_drop_files", tab=tab, paths=[str(BIG)], selector="#dropzone", method=method)
                print("  ", {k: r.get(k) for k in ("ok", "method", "trusted_events", "at")}, f"{time.time()-t0:.2f}s")
                print("   page_saw:", r.get("page_saw"))
                await call(s, "browser_wait", tab=tab, selector_or_text="Upload complete", timeout=30)
                log = await page_log(tab)
                results[f"drop_files {method} -> {r['method']}"] = check(log, "drop-trusted" if r["trusted_events"] else "drop-synthetic", tail_sha)

            print("\n[drop_files] method=cdp only")
            await reset(s, tab)
            r = await call(s, "browser_drop_files", tab=tab, paths=[str(BIG)], selector="#dropzone", method="cdp")
            print("  ", {k: r.get(k) for k in ("ok", "method", "trusted_events", "reason")}, "page_saw:", r.get("page_saw"))

            print("\n[click] dynamic input via chooser interception, files given")
            await reset(s, tab)
            r = await call(s, "browser_click", tab=tab, selector="#menu-upload", files=[str(BIG)])
            print("  ", r)
            await call(s, "browser_wait", tab=tab, selector_or_text="Upload complete", timeout=30)
            results["click + file chooser"] = check(await page_log(tab), "dynamic", tail_sha)

            print("\n[click] same, no files given -> chooser suppressed, told so")
            await reset(s, tab)
            r = await call(s, "browser_click", tab=tab, selector="#menu-upload")
            print("  ", r)
            results["chooser suppressed w/o files"] = r["file_chooser_opened"] is True

            print("\n[click] hidden file input directly -> refused")
            try:
                await call(s, "browser_click", tab=tab, selector="#shadow-host")
                print("   (host div clicked fine)")
                await call(s, "browser_click", tab=tab, selector="#frameinput")
                print("   NOT REFUSED (bad)")
            except RuntimeError as e:
                print("   refused ->", str(e)[:150])
                results["click on file input refused"] = "input type=file" in str(e)

            print("\n[wait] timeout path")
            print("  ", await call(s, "browser_wait", tab=tab, selector_or_text="text that never appears", timeout=1))
            print("[tabs]", (await call(s, "browser_tabs"))["tabs"])
    finally:
        print("\nclosing dedicated browser ->", await fb.close())
        srv.shutdown()
    after = daily_brave_pids()
    print("daily Brave main pids after:", after, "unchanged:", before == after)
    print("port 9229 still listening?:", await fb._listener())
    print("\nSUMMARY")
    for k, v in results.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    BIG.unlink()

asyncio.run(main())
