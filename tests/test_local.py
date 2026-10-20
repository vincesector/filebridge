"""local_copy (rsync job), reveal validation. reveal itself is not run (it would open Finder on screen)."""
import asyncio, hashlib, os, shutil, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from mcp_client import call, session

W = Path(__file__).parent / "_work" / "test-local"


async def done(s, jid):
    while (st := await call(s, "job_status", job_id=jid))["status"] == "running":
        await asyncio.sleep(0.3)
    return st


async def main():
    shutil.rmtree(W, ignore_errors=True)
    (W / "src" / "sub").mkdir(parents=True)
    for n, mb in (("a.bin", 120), ("sub/b.bin", 60)):
        (W / "src" / n).write_bytes(os.urandom(mb * 1024 * 1024))
    (W / "src" / "keep.txt").write_text("newer content")
    (W / "dest").mkdir()
    (W / "dest" / "keep.txt").write_text("old")
    async with session() as s:
        r = await call(s, "local_copy", src=str(W / "src") + "/", dest=str(W / "dest/mirror") + "/")
        print("start:", {k: r[k] for k in ("job_id", "src", "dest", "source_bytes")})
        st = await done(s, r["job_id"])
        print("done:", {k: st.get(k) for k in ("status", "exit_code", "elapsed_s", "current_file_percent", "speed")})
        same = all(hashlib.sha256((W / "src" / n).read_bytes()).hexdigest() == hashlib.sha256((W / "dest/mirror" / n).read_bytes()).hexdigest() for n in ("a.bin", "sub/b.bin"))
        print("checksums equal:", same)
        r = await call(s, "local_copy", src=str(W / "src/keep.txt"), dest=str(W / "dest/keep.txt"))
        await done(s, r["job_id"])
        print("no-overwrite keeps old:", (W / "dest/keep.txt").read_text() == "old")
        r = await call(s, "local_copy", src=str(W / "src/keep.txt"), dest=str(W / "dest/keep.txt"), overwrite=True)
        await done(s, r["job_id"])
        print("overwrite=true replaces:", (W / "dest/keep.txt").read_text() == "newer content")
        # progress on a bigger tree: sample job_status while rsync runs
        (W / "src2").mkdir()
        (W / "src2" / "big.bin").write_bytes(os.urandom(1024) * (1024 * 1024))  # 1 GiB
        r = await call(s, "local_copy", src=str(W / "src2") + "/", dest=str(W / "dest2") + "/")
        seen = []
        while (st := await call(s, "job_status", job_id=r["job_id"]))["status"] == "running":
            seen.append(st.get("percent")); await asyncio.sleep(0.2)
        print(f"1 GiB local_copy: {len(seen)} running polls, percents seen:", sorted({p for p in seen if p is not None}), "| final:", st["status"], st.get("percent"), st.get("bytes_human"))
        print("   sha256 equal:", hashlib.sha256((W / "src2/big.bin").read_bytes()).hexdigest() == hashlib.sha256((W / "dest2/big.bin").read_bytes()).hexdigest())
        for bad in ({"src": "relative/x", "dest": "/tmp/x"}, {"src": "~/.ssh", "dest": str(W / "x")}):
            try:
                await call(s, "local_copy", **bad); print("NOT BLOCKED", bad)
            except RuntimeError as e:
                print("blocked:", str(e)[-110:])
        try:
            await call(s, "reveal", path=str(W / "nope"))
        except RuntimeError as e:
            print("reveal validation:", str(e)[-70:])
        print("setup_instructions(sftp):", (await call(s, "setup_instructions", backend="sftp"))["command"])
        print("setup backends:", (await call(s, "setup_instructions"))["backends"])
    shutil.rmtree(W)

asyncio.run(main())
