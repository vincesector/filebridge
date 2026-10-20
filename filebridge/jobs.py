"""Background jobs. Each job is a detached runner process that survives the MCP server.

State on disk: ~/.filebridge/jobs/<id>.json (meta), <id>.log (tool output),
and an append-only ~/.filebridge/jobs.jsonl event log.
"""

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import JOBS_DIR, JOBS_LOG, FBError, ensure_state_dirs

JOB_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def meta_path(job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id or ""):
        raise FBError(f"Invalid job_id {job_id!r}. Expected something like 20261020-111452-a1b2c3 (see jobs()).", "InvalidArgument")
    return JOBS_DIR / f"{job_id}.json"


def log_path(job_id: str) -> Path:
    return JOBS_DIR / f"{job_id}.log"


def write_meta(path: Path, meta: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, indent=1))
    os.replace(tmp, path)


def append_event(event: dict[str, Any]) -> None:
    ensure_state_dirs()
    with open(JOBS_LOG, "a") as f:
        f.write(json.dumps({"ts": now_iso(), **event}) + "\n")


def read_meta(job_id: str) -> dict[str, Any]:
    p = meta_path(job_id)
    if not p.exists():
        raise FBError(f"No such job {job_id}. Use jobs() to list recent ones.", "InvalidArgument")
    return json.loads(p.read_text())


def start_job(kind: str, argv: list[str], **extra: Any) -> dict[str, Any]:
    """Create the job record and launch the detached runner. Returns the meta."""
    ensure_state_dirs()
    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
    meta = {"id": job_id, "kind": kind, "argv": argv, "created": now_iso(), "status": "running", **extra}
    mp = meta_path(job_id)
    write_meta(mp, meta)
    append_event({"event": "created", "id": job_id, "kind": kind, "argv": argv, **extra})
    runner = subprocess.Popen(
        [sys.executable, "-m", "filebridge.runner", str(mp)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    meta["runner_pid"] = runner.pid  # the runner records the same value itself; this copy is only for the caller
    return meta


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def refresh(meta: dict[str, Any]) -> dict[str, Any]:
    """If the runner died without recording a result, mark the job lost."""
    if meta["status"] == "running" and meta.get("runner_pid") and not pid_alive(meta["runner_pid"]):
        time.sleep(0.2)
        meta = json.loads(meta_path(meta["id"]).read_text())
        if meta["status"] == "running":
            meta["status"] = "error"
            meta["error"] = "runner process disappeared without recording a result"
            write_meta(meta_path(meta["id"]), meta)
    return meta


def tail_lines(path: Path, nbytes: int = 65536) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        data = f.read()
    # rsync progress rewrites lines with \r
    text = data.decode("utf-8", "replace").replace("\r", "\n")
    return [ln for ln in text.split("\n") if ln.strip()]


def list_jobs(limit: int = 20) -> list[dict[str, Any]]:
    ensure_state_dirs()
    files = sorted(JOBS_DIR.glob("*.json"), reverse=True)[:limit]
    out = []
    for f in files:
        try:
            out.append(refresh(json.loads(f.read_text())))
        except (json.JSONDecodeError, OSError):
            continue
    return out


def cancel_job(job_id: str) -> dict[str, Any]:
    meta = refresh(read_meta(job_id))
    if meta["status"] != "running":
        return {"id": job_id, "cancelled": False, "status": meta["status"], "note": "job is not running"}
    pid = meta.get("runner_pid")
    # Guard against pid reuse: only signal a process that really is our runner.
    try:
        cmd = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False).stdout
    except OSError:
        cmd = ""
    if "filebridge.runner" not in cmd:
        raise FBError(f"Runner pid {pid} is not a filebridge runner anymore; refusing to signal it.")
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.1)
        if json.loads(meta_path(job_id).read_text())["status"] != "running":
            break
    final = json.loads(meta_path(job_id).read_text())
    return {"id": job_id, "cancelled": final["status"] == "cancelled", "status": final["status"]}
