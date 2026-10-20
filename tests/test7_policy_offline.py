"""v0.2 offline checks (no browser, no network): config creation, path policy (allowed / blocked / outside with an
elicitation stub answering yes / no / decline / unsupported, symlink escapes, FILEBRIDGE_UNRESTRICTED), single-use
serve URLs, structured error codes, the --slim tool list and the CLI's non-TTY refusal.

Every server runs with HOME pointed at a temp folder, so the real ~/.filebridge/config.toml is never touched."""
import asyncio, json, os, shutil, subprocess, sys, tempfile, tomllib, urllib.error, urllib.request
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import mcp.types as mt

ROOT = Path(__file__).parents[1]
CLI = str(ROOT / ".venv" / "bin" / "filebridge")
TMP = Path(tempfile.mkdtemp(prefix="fb-policy-")).resolve()
HOME = TMP / "home"
PROJ = HOME / "Projects"
OUT = TMP / "outside"
CWD = TMP / "cwd"
res: dict[str, bool] = {}


def env(**extra):
    e = {k: v for k, v in os.environ.items() if k not in ("FILEBRIDGE_UNRESTRICTED",)}
    return {**e, "HOME": str(HOME), **extra}


def params(*args, **extra):
    return StdioServerParameters(command=CLI, args=["mcp", *args], env=env(**extra), cwd=str(CWD))


def stub(answer):
    """Elicitation callback: True/False = accept with allow, 'decline' / 'cancel' = that action."""
    async def cb(ctx, p):
        stub.asked.append(p.message)
        if answer in ("decline", "cancel"):
            return mt.ElicitResult(action=answer)
        return mt.ElicitResult(action="accept", content={"allow": answer})
    return cb


stub.asked = []


async def call(s, name, **args):
    r = await s.call_tool(name, args)
    sc = r.structured_content
    if sc is None:
        sc = json.loads(r.content[0].text)
    return r.is_error, sc


async def with_server(fn, *, elicit=None, args=(), **extra):
    async with stdio_client(params(*args, **extra)) as (r, w):
        async with ClientSession(r, w, elicitation_callback=elicit) as s:
            await s.initialize()
            return await fn(s)


def check(label, ok, detail=""):
    res[label] = bool(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))


def get(url, method="GET"):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except urllib.error.URLError:
        return -1, b""  # nothing listening any more


def setup():
    for d in (PROJ / "sub", OUT, CWD, HOME / ".ssh"):
        d.mkdir(parents=True, exist_ok=True)
    (PROJ / "ok.txt").write_text("allowed")
    (PROJ / ".env").write_text("SECRET=1")
    (PROJ / "id_rsa_backup").write_text("key")
    (PROJ / "Login Data").write_text("db")
    (PROJ / "sub" / "keep.txt").write_text("x")
    (PROJ / "withsecret").mkdir()
    (PROJ / "withsecret" / "a.txt").write_text("a")
    (PROJ / "withsecret" / ".env.local").write_text("SECRET=2")
    (OUT / "a.txt").write_text("outside")
    (OUT / "b.bin").write_bytes(os.urandom(2048))
    (HOME / ".ssh" / "id_ed25519").write_text("private key")
    (PROJ / "link-out.txt").symlink_to(OUT / "a.txt")
    (PROJ / "innocent.txt").symlink_to(HOME / ".ssh" / "id_ed25519")
    (PROJ / "sshdir").symlink_to(HOME / ".ssh")
    (CWD / "incwd.txt").write_text("cwd file")


def unit_globs():
    print("\n[glob matching, in process]")
    sys.path.insert(0, str(ROOT))
    from filebridge import policy
    pats = policy.blocked_patterns({"blocked_globs": ["**/private/*.mov"]})
    cases = {
        "/u/x/.ssh/id_rsa": "**/.ssh/**", "/u/x/.ssh": "**/.ssh/**", "/u/x/.SSH/config": "**/.ssh/**",
        "/u/p/.env": "**/.env", "/u/p/.env.production": "**/.env.*", "/u/p/server.pem": "**/*.pem", "/u/p/tls.KEY": "**/*.key",
        "/u/x/Library/Keychains/login.keychain-db": "**/Library/Keychains/**", "/u/x/.config/gcloud/creds.json": "**/.config/gcloud/**",
        "/u/p/app-secrets.json": "**/*secrets*.json", "/u/b/Default/Cookies": "**/Cookies", "/u/b/Default/Login Data": "**/Login Data",
        "/u/p/private/clip.mov": "**/private/*.mov", "/u/x/.config/rclone/rclone.conf": "**/.config/rclone/**",
    }
    for path, want in cases.items():
        got = policy.match_blocked(path, pats)
        check(f"blocked {path}", got == want, f"got {got}")
    for path in ("/u/p/environment.txt", "/u/p/.envrc-notes/readme.md", "/u/p/keynote.mov", "/u/p/secrets.txt", "/u/p/my.keys.txt", "/u/p/private/sub/clip.mov"):
        check(f"not blocked {path}", policy.match_blocked(path, pats) is None, f"got {policy.match_blocked(path, pats)}")
    here = os.getcwd()
    try:
        os.chdir(Path.home())
        check("<cwd> ignored when it is the home folder", policy._cwd_root() is None)
        os.chdir("/")
        check("<cwd> ignored when it is /", policy._cwd_root() is None)
    finally:
        os.chdir(here)


async def main():
    setup()
    unit_globs()

    print("\n[config creation]")
    cfg_path = HOME / ".filebridge" / "config.toml"
    check("no config before first run", not cfg_path.exists())

    async def allowed(s):
        e, r = await call(s, "browser_upload_own", paths=[str(PROJ / "ok.txt")])
        await call(s, "serve_stop", server_id=r["server_id"])
        e2, r2 = await call(s, "browser_upload_own", paths=[str(CWD / "incwd.txt")])
        return e, r, e2, r2
    stub.asked.clear()
    e, r, e2, r2 = await with_server(allowed, elicit=stub(False))
    cfg = tomllib.loads(cfg_path.read_text()) if cfg_path.exists() else {}
    check("config created on first run", cfg_path.exists())
    check("config has default roots incl <cwd>", cfg.get("allowed_roots") == ["~/Projects", "~/Movies", "~/Downloads", "~/Documents", "<cwd>"])
    check("config lists the denylist", "**/.ssh/**" in cfg.get("blocked_globs", []) and "**/Login Data" in cfg["blocked_globs"])
    check("config max_bytes 0", cfg.get("max_bytes") == 0)
    print("\n[path policy]")
    check("inside ~/Projects: allowed, no prompt", not e and r.get("ok") and r.get("server_id") and not stub.asked, r)
    check("inside <cwd> (client working dir): allowed", not e2 and r2.get("ok"), r2)

    async def blocked(s):
        out = {}
        for name in (".env", "id_rsa_backup", "Login Data", "innocent.txt"):
            out[name] = await call(s, "browser_upload_own", paths=[str(PROJ / name)])
        out["sshdir"] = await call(s, "browser_upload_own", paths=[str(PROJ / "sshdir" / "id_ed25519")])
        out["folder"] = await call(s, "local_copy", src=str(PROJ / "withsecret"), dest=str(TMP / "copy-dest"))
        out["ssh-root"] = await call(s, "clipboard_files", paths=[str(HOME / ".ssh")])
        return out
    stub.asked.clear()
    out = await with_server(blocked, elicit=stub(True))
    for name, (e, r) in out.items():
        check(f"PathBlocked even with approval: {name}", e and r.get("code") == "PathBlocked", r)
    check("blocked paths never prompt the user", not stub.asked, stub.asked)
    check("symlink to ~/.ssh is refused with the symlink named", "via a symlink" in out["innocent.txt"][1].get("message", ""), out["innocent.txt"][1])
    check("folder with a .env.local inside is refused and lists it", ".env.local" in out["folder"][1].get("message", ""), out["folder"][1])
    check("nothing was copied", not (TMP / "copy-dest").exists())

    async def outside(s):
        return [await call(s, "browser_upload_own", paths=[str(OUT / "a.txt")]),
                await call(s, "browser_upload_own", paths=[str(PROJ / "link-out.txt")]),
                await call(s, "browser_upload_own", paths=[str(OUT / "a.txt"), str(OUT / "b.bin"), str(PROJ / "ok.txt")])]
    for answer, want_ok in ((True, True), (False, False), ("decline", False), ("cancel", False)):
        stub.asked.clear()
        rs = await with_server(outside, elicit=stub(answer))
        good = all((not e and r.get("ok")) if want_ok else (e and r.get("code") == "PathNotAllowed") for e, r in rs)
        check(f"outside roots, elicitation answers {answer!r} -> {'allowed' if want_ok else 'PathNotAllowed'}", good, rs)
        check(f"  asked once per call ({answer!r})", len(stub.asked) == 3, stub.asked)
    check("question names action, path, size and destination",
          stub.asked and stub.asked[0].startswith("filebridge wants to serve ") and str(OUT / "a.txt") in stub.asked[0] and "(7 B)" in stub.asked[0]
          and "your own browser" in stub.asked[0] and stub.asked[0].endswith("Allow?"), stub.asked[:1])
    check("symlink escape (inside root -> outside target) asks about the real target", str(OUT / "a.txt") in stub.asked[1], stub.asked[1:2])
    check("several outside files -> one question listing them", "2 items" in stub.asked[2] and "b.bin" in stub.asked[2], stub.asked[2:3])

    rs = await with_server(outside, elicit=None)
    check("client without elicitation -> PathNotAllowed naming allowed_roots",
          all(e and r.get("code") == "PathNotAllowed" and r.get("config_key") == "allowed_roots" and "allowed_roots" in r["message"] for e, r in rs), rs[0])

    stub.asked.clear()
    rs = await with_server(outside, elicit=stub(False), FILEBRIDGE_UNRESTRICTED="1")
    out2 = await with_server(blocked, elicit=stub(True), FILEBRIDGE_UNRESTRICTED="1")
    check("FILEBRIDGE_UNRESTRICTED=1 lifts the allowlist without asking", all(not e and r.get("ok") for e, r in rs) and not stub.asked, rs)
    check("FILEBRIDGE_UNRESTRICTED=1 never lifts the denylist", all(e and r.get("code") == "PathBlocked" for e, r in out2.values()))

    print("\n[single-use links]")

    async def single(s):
        e, r = await call(s, "browser_upload_own", paths=[str(OUT / "b.bin"), str(PROJ / "ok.txt")], ttl=60)
        u0, u1 = r["urls"]
        pre = get(u0, "OPTIONS")[0]
        first = get(u0)
        second = get(u0)
        bad_token = get(u0.replace(u0.split("/")[3], "x" * 32))[0]
        mid = (await call(s, "serve_status", server_id=r["server_id"]))[1]
        get(u1)
        await asyncio.sleep(0.6)
        end = (await call(s, "serve_status", server_id=r["server_id"]))[1]
        after = get(u1)[0]
        return r, pre, first, second, bad_token, mid, end, after
    r, pre, first, second, bad_token, mid, end, after = await with_server(single, elicit=stub(True))
    check("result shape {server_id, urls, snippet, expires_in_s}", {"server_id", "urls", "snippet", "expires_in_s"} <= set(r), list(r))
    check("snippet is top-level await, not an IIFE", "await fetch" in r["snippet"] and not r["snippet"].lstrip().startswith(("(async", "(()")))
    check("snippet reports FetchBlocked / LinkUsed codes", "FetchBlocked" in r["snippet"] and "LinkUsed" in r["snippet"])
    check("CORS preflight does not use up the link", pre == 204)
    check("first GET 200 with the exact bytes", first[0] == 200 and first[1] == (OUT / "b.bin").read_bytes())
    check("second GET 410 Gone", second[0] == 410, second[0])
    check("wrong token 404", bad_token == 404, bad_token)
    check("serve_status mid-way: 1 of 2 served, running", mid["state"] == "running" and mid["served"] == 1 and mid["total"] == 2, mid)
    check("serve_status after all fetched: done", end["state"] == "done" and end["served"] == 2, end)
    check("server stops once every file was fetched", after in (-1, 410), after)

    async def stop_and_ttl(s):
        _, r1 = await call(s, "browser_upload_own", paths=[str(PROJ / "ok.txt")])
        st = (await call(s, "serve_stop", server_id=r1["server_id"]))[1]
        _, r2 = await call(s, "browser_upload_own", paths=[str(PROJ / "ok.txt")], ttl=10)
        bad = await call(s, "serve_status", server_id="nope")
        return st, r2, bad
    st, r2, bad = await with_server(stop_and_ttl, elicit=stub(True))
    check("serve_stop -> stopped", st["state"] == "stopped", st)
    check("ttl reported", 8 <= r2["expires_in_s"] <= 10, r2.get("expires_in_s"))
    check("unknown server_id -> InvalidArgument", bad[0] and bad[1].get("code") == "InvalidArgument", bad)

    print("\n[error codes]")

    async def codes(s):
        return {
            "FileNotFound": await call(s, "browser_upload_own", paths=[str(PROJ / "missing.mp4")]),
            "InvalidArgument": await call(s, "browser_upload_own", paths=["relative/file.mp4"]),
            "InvalidArgument folder": await call(s, "browser_upload_own", paths=[str(PROJ / "sub")]),
            "RemoteNotConfigured": await call(s, "transfer", src=str(PROJ / "ok.txt"), dest="nosuchremote-fb:x") if shutil.which("rclone") else (True, {"code": "RemoteNotConfigured"}),
        }
    cs = await with_server(codes, elicit=stub(True))
    for want, (e, r) in cs.items():
        check(f"code {want}", e and r.get("code") == want.split()[0] and r.get("ok") is False and r.get("message"), r)
    cfg_path.write_text(cfg_path.read_text().replace("max_bytes = 0", "max_bytes = 1000"))
    e, r = await with_server(lambda s: call(s, "browser_upload_own", paths=[str(OUT / "b.bin")]), elicit=stub(True))
    check("code FileTooLarge (max_bytes from config)", e and r.get("code") == "FileTooLarge", r)
    cfg_path.write_text(cfg_path.read_text().replace("max_bytes = 1000", "max_bytes = 0"))

    print("\n[--slim]")

    async def names(s):
        return {t.name for t in (await s.list_tools()).tools}
    slim = await with_server(names, args=("--slim",))
    full = await with_server(names)
    want = {"browser_upload_own", "serve_status", "serve_stop", "browser_set_files", "browser_drop_files", "browser_snapshot", "browser_tabs", "browser_navigate", "browser_launch"}
    check("--slim exposes only the upload tools", slim == want, sorted(slim ^ want))
    check("full server keeps every tool", want < full and {"transfer", "local_copy", "clipboard_files"} <= full)

    print("\n[CLI]")
    e = env()
    r = subprocess.run([CLI, "serve", "--json", str(OUT / "a.txt")], capture_output=True, text=True, env=e, cwd=CWD, stdin=subprocess.DEVNULL, timeout=30)
    err = json.loads(r.stderr.strip().splitlines()[-1]) if r.stderr.strip() else {}
    check("CLI, no TTY, outside roots -> exit 1 PathNotAllowed", r.returncode == 1 and err.get("code") == "PathNotAllowed", r.stderr[-200:])
    p = subprocess.Popen([CLI, "serve", "--json", "--ttl", "20", "--allow-path", str(OUT), str(OUT / "a.txt")], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=e, cwd=CWD, stdin=subprocess.DEVNULL)
    info = json.loads(p.stdout.readline())
    a, b = get(info["urls"][0]), get(info["urls"][0])
    rc = p.wait(timeout=20)
    check("CLI --allow-path DIR adds a root for that run; serve stops after the fetch", a == (200, b"outside") and b[0] == 410 and rc == 0, (a, b, rc))
    r = subprocess.run([CLI, "set-files", "--tab", "x", str(PROJ / ".env")], capture_output=True, text=True, env=e, cwd=CWD, stdin=subprocess.DEVNULL, timeout=30)
    check("CLI prints the code", r.returncode == 1 and "[PathBlocked]" in r.stderr, r.stderr[-200:])

    shutil.rmtree(TMP)
    print("\nSUMMARY:", sum(res.values()), "/", len(res), "passed")
    for k, v in res.items():
        if not v:
            print("  FAIL", k)
    sys.exit(0 if all(res.values()) else 1)


asyncio.run(main())
