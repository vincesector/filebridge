"""Test 1: 2 GiB random file local -> 'testlocal' alias remote through the real MCP server."""
import asyncio, hashlib, os, shutil, subprocess, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import call, session
from filebridge import rclone

HOME = Path(__file__).parent / "_work"  # inside ~/Projects, an allowed root by default
SRC_DIR = HOME / "test-src"
REMOTE_DIR = HOME / "test-remote"
SIZE = 2 * 1024**3


def make_file(path: Path) -> str:
    h = hashlib.sha256()
    chunk = 64 * 1024 * 1024
    with open(path, "wb") as f:
        for _ in range(SIZE // chunk):
            b = os.urandom(chunk)
            h.update(b)
            f.write(b)
    return h.hexdigest()


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(64 * 1024 * 1024):
            h.update(b)
    return h.hexdigest()


async def wait_job(s, job_id, label, poll=0.25):
    t0 = time.time()
    samples = []
    while True:
        st = await call(s, "job_status", job_id=job_id)
        samples.append((round(time.time() - t0, 2), st["status"], st.get("percent"), st.get("bytes_human"), st.get("speed_human")))
        if st["status"] != "running":
            break
        await asyncio.sleep(poll)
    print(f"[{label}] {len(samples)} polls over {time.time()-t0:.1f}s; distinct percents seen while running:",
          sorted({p for _, stt, p, _, _ in samples if stt == "running" and p is not None}))
    for smp in samples[:: max(1, len(samples) // 6)] + [samples[-1]]:
        print("   ", smp)
    return st, time.time() - t0


async def main():
    SRC_DIR.mkdir(parents=True, exist_ok=True)
    REMOTE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run(["rclone", "config", "create", "testlocal", "alias", f"remote={REMOTE_DIR}"], check=True, capture_output=True)
    t = time.time()
    src = SRC_DIR / "big.bin"
    src_sha = make_file(src)
    print(f"source: {src} {src.stat().st_size} bytes sha256={src_sha} (generated in {time.time()-t:.1f}s)")

    async with session() as s:
        print("remotes():", await call(s, "remotes"))

        # A: the plain tool call
        r = await call(s, "transfer", src=str(src), dest="testlocal:run-a")
        print("A transfer ->", {k: r[k] for k in ("job_id", "status", "mode", "src", "dest")})
        st, secs = await wait_job(s, r["job_id"], "A plain tool call")
        print("   final:", {k: st.get(k) for k in ("status", "exit_code", "bytes", "total_bytes", "percent", "files_done", "elapsed_s")})
        d = REMOTE_DIR / "run-a" / "big.bin"
        print(f"   dest size={d.stat().st_size} match={d.stat().st_size == SIZE}  sha256 match={sha(d) == src_sha}")

        # B: force a real byte copy (APFS clones make A near-instant) and throttle it so progress is observable
        r = await rclone.start_transfer(str(src), "testlocal:run-b", "copy", False, False, False, "300M", ("--disable", "copy"))
        st, secs = await wait_job(s, r["job_id"], "B real byte copy @300M/s")
        print("   final:", {k: st.get(k) for k in ("status", "exit_code", "bytes", "total_bytes", "percent", "files_done", "elapsed_s")})
        d = REMOTE_DIR / "run-b" / "big.bin"
        print(f"   dest size={d.stat().st_size} match={d.stat().st_size == SIZE}  sha256 match={sha(d) == src_sha}")

        # C: re-copy without overwrite is a no-op (--ignore-existing)
        r = await call(s, "transfer", src=str(src), dest="testlocal:run-b")
        st, _ = await wait_job(s, r["job_id"], "C re-copy, no overwrite")
        print("   files_done (expect 0 transferred):", st.get("files_done"), "bytes:", st.get("bytes"))

        # D: ls / stat / mkdir on the remote
        print("ls:", await call(s, "ls", remote_path="testlocal:run-b"))
        print("stat hit:", await call(s, "stat", remote_path="testlocal:run-b/big.bin"))
        print("stat miss:", await call(s, "stat", remote_path="testlocal:run-b/nope.bin"))
        print("mkdir:", await call(s, "mkdir", remote_path="testlocal:made/by/tool"))

        # E: safety gates
        for label, kw in [("sync without dry run", dict(src=str(SRC_DIR), dest="testlocal:run-s", mode="sync")),
                          ("unknown remote", dict(src=str(src), dest="nosuch:x")),
                          ("relative path", dict(src="big.bin", dest="testlocal:x")),
                          ("missing source", dict(src=str(SRC_DIR / "ghost.bin"), dest="testlocal:x"))]:
            try:
                await call(s, "transfer", **kw)
                print(f"E {label}: NOT BLOCKED (bad)")
            except RuntimeError as e:
                print(f"E {label}: blocked ->", str(e)[:170])
        r = await call(s, "transfer", src=str(SRC_DIR), dest="testlocal:run-s", mode="sync", dry_run=True)
        st, _ = await wait_job(s, r["job_id"], "E sync dry run")
        r = await call(s, "transfer", src=str(SRC_DIR), dest="testlocal:run-s", mode="sync")
        st, _ = await wait_job(s, r["job_id"], "E real sync after dry run")
        print("   sync status:", st["status"], "dest has:", sorted(p.name for p in (REMOTE_DIR / "run-s").iterdir()))

        # F: cancel a slow job
        r = await rclone.start_transfer(str(src), "testlocal:run-c", "copy", False, False, False, "50M", ("--disable", "copy"))
        await asyncio.sleep(3)
        mid = await call(s, "job_status", job_id=r["job_id"])
        print("F mid-flight:", {k: mid.get(k) for k in ("status", "percent", "bytes_human", "speed_human")})
        print("F cancel ->", await call(s, "cancel", job_id=r["job_id"]))
        print("F jobs():", [(j["id"][-6:], j["status"]) for j in (await call(s, "jobs", limit=4))["jobs"]])

    # cleanup of test data
    shutil.rmtree(SRC_DIR, ignore_errors=True)
    shutil.rmtree(REMOTE_DIR, ignore_errors=True)
    subprocess.run(["rclone", "config", "delete", "testlocal"], check=True, capture_output=True)
    print("cleanup done; leftover:", [p for p in (SRC_DIR, REMOTE_DIR) if p.exists()])

asyncio.run(main())
