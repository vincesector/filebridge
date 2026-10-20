"""Detached job runner: `python -m filebridge.runner <meta.json>`.

Runs meta['argv'] (an argument list, never a shell) with stdout+stderr appended to the
job log, forwards SIGTERM to it, and records the final status in the meta file.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .jobs import append_event, now_iso, write_meta


def main() -> int:
    mp = Path(sys.argv[1])
    meta = json.loads(mp.read_text())
    log = mp.with_suffix(".log")
    cancelled = False
    child: subprocess.Popen | None = None

    def on_term(signum, frame):
        nonlocal cancelled
        cancelled = True
        if child and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    meta["runner_pid"] = os.getpid()
    meta["started"] = now_iso()
    t0 = time.time()
    rc = -1
    err = None
    try:
        with open(log, "ab", buffering=0) as lf:
            child = subprocess.Popen(meta["argv"], stdin=subprocess.DEVNULL, stdout=lf, stderr=lf)
            meta["pid"] = child.pid
            write_meta(mp, meta)
            while True:
                try:
                    rc = child.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if cancelled and child.poll() is None:
                        try:
                            rc = child.wait(timeout=15)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            rc = child.wait()
                        break
    except OSError as e:
        err = f"could not start {meta['argv'][0]}: {e}"

    meta["exit_code"] = rc
    meta["finished"] = now_iso()
    meta["elapsed_s"] = round(time.time() - t0, 1)
    if err:
        meta["status"], meta["error"] = "error", err
    elif cancelled:
        meta["status"] = "cancelled"
    elif rc == 0:
        meta["status"] = "done"
    else:
        meta["status"] = "error"
    write_meta(mp, meta)
    append_event({"event": "finished", "id": meta["id"], "status": meta["status"], "exit_code": rc, "elapsed_s": meta["elapsed_s"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
