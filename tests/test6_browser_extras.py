"""navigate / text / screenshot / type on the headless browser."""
import asyncio, http.server, struct, sys, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import call, session

HERE = Path(__file__).parent
PORT = 8765
OUT = Path.home() / ".filebridge" / "test-shots"


def serve():
    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            return str(HERE / ("test2_frame2.html" if path.startswith("/frame2.html") else "test2_frame.html" if path.startswith("/frame.html") else "test2_page.html"))
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def png_size(p):
    b = Path(p).read_bytes()
    assert b[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", b[16:24])


async def main():
    srv = serve(); res = {}
    try:
        async with session() as s:
            r = await call(s, "browser_launch", url=f"http://127.0.0.1:{PORT}/")
            tab = r["tabs"][0]["id"]
            print("launch mode:", r["mode"])
            n = await call(s, "browser_navigate", tab=tab, url=f"http://127.0.0.1:{PORT}/?second=1")
            print("navigate:", n)
            res["navigate"] = n["loaded"] and n["url"].endswith("?second=1")
            t = await call(s, "browser_text", tab=tab)
            print("text:", {k: t[k] for k in ("title", "chars", "truncated")}, repr(t["text"][:70]))
            res["text whole page"] = "filebridge test page" in t["text"] and "Upload file (dynamic input)" in t["text"]
            t2 = await call(s, "browser_text", tab=tab, selector="#dropzone")
            res["text of element"] = t2["text"].strip() == "drop files here"
            ty = await call(s, "browser_type", tab=tab, selector="#title", text="My new title", clear=True)
            print("type clear:", ty)
            res["type replaces value"] = ty["field_now"] == "My new title"
            ty = await call(s, "browser_type", tab=tab, selector="#title", text=" v2")
            res["type appends"] = ty["field_now"] == "My new title v2"
            OUT.mkdir(exist_ok=True)
            sh = await call(s, "browser_screenshot", tab=tab, path=str(OUT / "viewport.png"))
            sh2 = await call(s, "browser_screenshot", tab=tab, path=str(OUT / "full.png"), full_page=True)
            print("screenshots:", png_size(sh["path"]), sh["bytes"], "bytes;", png_size(sh2["path"]), sh2["bytes"], "bytes")
            res["screenshot png"] = sh["bytes"] > 2000 and png_size(sh["path"])[0] >= 800
            sh3 = await call(s, "browser_screenshot", tab=tab)
            res["screenshot default path"] = Path(sh3["path"]).exists()
            Path(sh3["path"]).unlink()
            for label, kw, name in (("overwrite refused", dict(path=str(OUT / "viewport.png")), "browser_screenshot"),
                                    ("bad scheme", dict(url="javascript:alert(1)"), "browser_navigate")):
                try:
                    await call(s, name, tab=tab, **kw); res[label] = False
                except RuntimeError as e:
                    print(label, "->", str(e)[-80:]); res[label] = True
            await call(s, "browser_close")
    finally:
        srv.shutdown()
        for f in OUT.glob("*.png"): f.unlink()
        OUT.rmdir()
    print("\nSUMMARY")
    for k, v in res.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")

asyncio.run(main())
