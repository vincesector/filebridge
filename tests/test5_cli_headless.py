"""Headless proof, mode switch, and the CLI (same core as MCP). Nothing here shows a window."""
import asyncio, hashlib, http.server, json, os, shutil, subprocess, sys, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from filebridge import browser as fb

HERE = Path(__file__).parent
W = Path.home() / ".filebridge" / "test-cli"
CLI = shutil.which("filebridge") or str(Path(__file__).parents[1] / ".venv" / "bin" / "filebridge")
PORT = 8765


def cli(*args, expect=0):
    r = subprocess.run([CLI, *args], capture_output=True, text=True)
    assert r.returncode == expect, f"{args} exit {r.returncode} (wanted {expect}): {r.stdout[-300:]} {r.stderr[-300:]}"
    return r


def serve():
    class H(http.server.SimpleHTTPRequestHandler):
        def translate_path(self, path):
            return str(HERE / ("test2_frame2.html" if path.startswith("/frame2.html") else "test2_frame.html" if path.startswith("/frame.html") else "test2_page.html"))
        def log_message(self, *a): pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


async def page_log(tab):
    async with fb._session(tab) as (c, t):
        return await fb._eval(c, "[...document.querySelectorAll('#log li')].map(li => li.textContent)")


def main():
    shutil.rmtree(W, ignore_errors=True)
    (W / "remote").mkdir(parents=True)
    f = W / "payload.bin"
    f.write_bytes(os.urandom(64 * 1024 * 1024))
    want = hashlib.sha256(f.read_bytes()).hexdigest()
    srv = serve()
    res = {}
    try:
        print("== CLI, rclone lane")
        cli("--help")
        subprocess.run(["rclone", "config", "create", "testcli", "alias", f"remote={W / 'remote'}"], check=True, capture_output=True)
        r = cli("transfer", str(f), "testcli:in", "--wait", "--json")
        st = json.loads(r.stdout)
        print("transfer --wait --json ->", {k: st.get(k) for k in ("status", "exit_code", "bytes", "total_bytes", "elapsed_s")})
        res["cli transfer + checksum"] = st["status"] == "done" and hashlib.sha256((W / "remote/in/payload.bin").read_bytes()).hexdigest() == want
        print(cli("ls", "testcli:in").stdout.strip())
        print(cli("stat", "testcli:in/payload.bin").stdout.strip().replace("\n", " | "))
        print(cli("stat", "testcli:in/ghost.bin", "--json").stdout.strip().replace("\n", ""))
        print("remotes:", cli("remotes").stdout.strip().replace("\n", " | "))
        e = cli("transfer", "~/nope-does-not-exist", "testcli:x", expect=1)
        print("error path: exit 1, stderr:", e.stderr.strip()[:110])
        e = cli("transfer", "nosuch:a", "testcli:x", "--json", expect=1)
        print("error path json:", e.stderr.strip()[:110])
        res["cli errors exit 1"] = True
        r = cli("copy", str(f), str(W / "local-copy") + "/", "--wait")
        res["cli local copy"] = (W / "local-copy/payload.bin").exists() and "status: done" in r.stdout
        print("copy --wait:", r.stdout.strip().replace("\n", " | ")[:160])

        print("\n== headless browser via CLI")
        r = cli("browser", "launch", "--url", f"http://127.0.0.1:{PORT}/", "--json")
        info = json.loads(r.stdout)
        cmd = subprocess.run(["/bin/ps", "-p", str(info["pid"]), "-o", "command="], capture_output=True, text=True).stdout
        print("mode:", info["mode"], "| app:", info["app"], "| pid:", info["pid"], "| --headless=new in argv:", "--headless=new" in cmd, "| tabs:", len(info["tabs"]))
        res["launch is headless by default"] = info["mode"] == "headless" and "--headless=new" in cmd
        tab = info["tabs"][0]["id"]
        # a headless browser owns no window server connection: count on-screen windows owned by this pid
        wins = subprocess.run(["/usr/bin/python3", "-c", f"""
import Quartz; ws = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
print(sum(1 for w in ws if w.get('kCGWindowOwnerPID') == {info['pid']}))"""], capture_output=True, text=True)
        if wins.returncode == 0:
            print("on-screen windows owned by the automation browser pid:", wins.stdout.strip())
            res["no on-screen window"] = wins.stdout.strip() == "0"
        r = cli("set-files", "--tab", tab, "--selector", "#fileinput", str(f), "--json")
        print("set-files:", {k: json.loads(r.stdout).get(k) for k in ("ok", "method", "sizes_match_disk")})
        cli("browser", "wait", "--tab", tab, "Upload complete", "--timeout", "20")
        log = asyncio.run(page_log(tab))
        res["cli set-files"] = any(l.startswith("input|payload.bin|67108864|") for l in log)
        cli("browser", "click", "--tab", tab, "--selector", "#reset")
        r = cli("drop", "--tab", tab, "--selector", "#dropzone", str(f), "--json")
        print("drop:", {k: json.loads(r.stdout).get(k) for k in ("ok", "method", "trusted_events")})
        cli("browser", "wait", "--tab", tab, "Upload complete", "--timeout", "20")
        res["cli drop"] = any(l.startswith("drop-trusted|payload.bin|67108864|") for l in asyncio.run(page_log(tab)))
        cli("browser", "wait", "--tab", tab, "never appears", "--timeout", "1", expect=1)
        r2 = cli("browser", "launch", "--json")
        res["relaunch reuses headless"] = json.loads(r2.stdout)["reused_existing"] is True and json.loads(r2.stdout)["pid"] == info["pid"]

        print("\n== mode switch headless -> visible (window suppressed by the test seam)")
        print("decision table:", {(c, v): fb.needs_restart(c, v) for c in ("x --headless=new y", "x y") for v in (False, True)})
        res["decision table"] = (not fb.needs_restart("a --headless=new", False)) and fb.needs_restart("a --headless=new", True) \
            and fb.needs_restart("a", False) and not fb.needs_restart("a", True)
        spawned = []
        real_spawn = fb._spawn
        def fake_spawn(args):  # record what would have been a VISIBLE launch, but keep it windowless
            spawned.append(list(args))
            real_spawn(args if any(a.startswith("--headless") for a in args) else args[:-1] + ["--headless=new", args[-1]])
        fb._spawn = fake_spawn
        try:
            r3 = asyncio.run(fb.launch(None, "auto", visible=True))
        finally:
            fb._spawn = real_spawn
        print("visible request ->", {k: r3[k] for k in ("mode", "restarted_for_mode_switch", "pid")}, "(old pid %d)" % info["pid"])
        print("argv that would have opened a window had no --headless:", "--headless=new" not in " ".join(spawned[0]), "| args:", " ".join(spawned[0][1:]) [:150])
        res["mode switch restarts on same profile"] = r3["restarted_for_mode_switch"] and r3["pid"] != info["pid"] and "--headless" not in " ".join(spawned[0]) \
            and f"--user-data-dir={fb.BROWSER_PROFILE}" in spawned[0]
        cli("browser", "close")
    finally:
        subprocess.run([CLI, "browser", "close"], capture_output=True)
        srv.shutdown()
        subprocess.run(["rclone", "config", "delete", "testcli"], capture_output=True)
        shutil.rmtree(W, ignore_errors=True)
    print("\nSUMMARY")
    for k, v in res.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")

main()
