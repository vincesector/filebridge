"""Lane 1: cloud storage through rclone. Argument lists only, no shell."""

import json
import re
import time
from datetime import datetime
from typing import Any

from . import jobs
from .common import FBError, find_tool, local_path, run_capture, split_location

_BWLIMIT_RE = re.compile(r"^\d+(\.\d+)?[bBkKMGTP]?$")
_BASE = ["--ask-password=false"]


def rclone_bin() -> str:
    return find_tool("rclone")


async def list_remotes() -> dict[str, str]:
    rc, out, err = await run_capture([rclone_bin(), "listremotes", "--long", *_BASE], timeout=30)
    if rc != 0:
        raise FBError(f"rclone listremotes failed: {err.strip()[-300:]}")
    remotes = {}
    for line in out.splitlines():
        name, _, typ = line.partition(":")
        if name.strip():
            remotes[name.strip()] = typ.strip()
    return remotes


async def resolve(p: str, *, source: bool, remotes: dict[str, str] | None = None) -> tuple[str, str | None, str]:
    """Return (argument for rclone, remote_type or None, display form)."""
    name, rest = split_location(p)
    if name is None:
        lp = local_path(p, must_exist=source, source=source)
        return str(lp), None, str(lp)
    remotes = remotes if remotes is not None else await list_remotes()
    if name not in remotes:
        have = ", ".join(sorted(remotes)) or "none configured"
        raise FBError(f"Unknown remote {name!r} (configured: {have}). Call setup_instructions() for the command the user must run to add one.")
    return f"{name}:{rest}", remotes[name], f"{name}:{rest}"


def _run_failure(what: str, rc: int, err: str) -> FBError:
    lines = [ln for ln in err.strip().splitlines() if ln.strip()]
    return FBError(f"{what} failed (rclone exit {rc}): " + " | ".join(lines[-4:])[:700])


async def start_transfer(
    src: str, dest: str, mode: str, dry_run: bool, overwrite: bool, confirm: bool, bwlimit: str | None, _extra: tuple[str, ...] = ()
) -> dict[str, Any]:
    if mode not in ("copy", "move", "sync"):
        raise FBError("mode must be 'copy', 'move' or 'sync'.")
    if bwlimit and not _BWLIMIT_RE.match(bwlimit):
        raise FBError("bwlimit must look like '10M' or '500k' (bytes per second).")
    remotes = await list_remotes()
    s_arg, s_type, s_disp = await resolve(src, source=True, remotes=remotes)
    d_arg, d_type, d_disp = await resolve(dest, source=False, remotes=remotes)
    if s_arg == d_arg:
        raise FBError("Source and destination are the same.")

    if mode == "sync" and not dry_run and not confirm:
        recent = _recent_dry_run(s_disp, d_disp)
        if not recent:
            raise FBError(
                "sync deletes files on the destination that are missing from the source. "
                f"Run transfer(src={src!r}, dest={dest!r}, mode='sync', dry_run=true) first and check the log, "
                "then repeat without dry_run (a successful dry run in the last 24h unlocks it), or pass confirm=true."
            )

    argv = [rclone_bin(), mode, s_arg, d_arg, *_BASE,
            "--transfers", "4", "--retries", "5", "--low-level-retries", "20",
            "--use-json-log", "--stats", "1s", "--stats-log-level", "NOTICE", "--log-level", "NOTICE"]
    if "drive" in (s_type, d_type):
        argv += ["--drive-chunk-size", "64M"]
    if mode in ("copy", "move") and not overwrite:
        argv.append("--ignore-existing")
    if bwlimit:
        argv += ["--bwlimit", bwlimit]
    if dry_run:
        argv.append("--dry-run")
    argv += list(_extra)

    meta = jobs.start_job("transfer", argv, mode=mode, src=s_disp, dest=d_disp, dry_run=dry_run, overwrite=overwrite)
    note = "sync mirrors source onto destination and may delete destination files." if mode == "sync" and not dry_run else None
    return {"job_id": meta["id"], "status": "running", "mode": mode, "src": s_disp, "dest": d_disp,
            "dry_run": dry_run, "log": str(jobs.log_path(meta["id"])), "note": note}


def _recent_dry_run(src: str, dest: str) -> bool:
    cutoff = time.time() - 86400
    for m in jobs.list_jobs(limit=200):
        if (m.get("kind") == "transfer" and m.get("mode") == "sync" and m.get("dry_run") and m.get("status") == "done"
                and m.get("src") == src and m.get("dest") == dest and m.get("finished")):
            if datetime.fromisoformat(m["finished"]).timestamp() >= cutoff:
                return True
    return False


def _human(n: float | None) -> str | None:
    if n is None:
        return None
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return None


def parse_rclone_log(lines: list[str]) -> tuple[dict[str, Any] | None, list[str]]:
    stats, errors = None, []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            errors.append(ln[:300])  # non-JSON output, e.g. a crash trace
            continue
        if "stats" in rec:
            stats = rec["stats"]
        if rec.get("level") in ("error", "critical", "fatal"):
            errors.append(rec.get("msg", "")[:300])
    return stats, errors[-5:]


def rsync_progress(lines: list[str]) -> dict[str, Any] | None:
    # openrsync/rsync --progress line: "  1,234,567  45%   12.3MB/s    0:00:05"
    for ln in reversed(lines):
        m = re.match(r"^\s*([\d,]+)\s+(\d+)%\s+(\S+)\s+(\S+)", ln)
        if m:
            return {"current_file_bytes": int(m.group(1).replace(",", "")), "current_file_percent": int(m.group(2)),
                    "speed": m.group(3), "eta": m.group(4)}
    return None


def status(job_id: str) -> dict[str, Any]:
    meta = jobs.refresh(jobs.read_meta(job_id))
    lines = jobs.tail_lines(jobs.log_path(job_id))
    out: dict[str, Any] = {k: meta.get(k) for k in ("id", "kind", "status", "src", "dest", "mode", "dry_run", "created", "finished", "elapsed_s", "exit_code")}
    out = {k: v for k, v in out.items() if v is not None}
    if meta["kind"] == "transfer":
        stats, errors = parse_rclone_log(lines)
        if stats:
            total = stats.get("totalBytes") or 0
            done = stats.get("bytes") or 0
            out.update({
                "bytes": done, "bytes_human": _human(done), "total_bytes": total, "total_human": _human(total),
                "percent": round(100 * done / total, 1) if total else (100.0 if meta["status"] == "done" else None),
                "speed_bps": stats.get("speed"), "speed_human": (_human(stats["speed"]) + "/s") if stats.get("speed") else None,
                "eta_s": stats.get("eta"),
                "files_done": stats.get("transfers"), "files_total": stats.get("totalTransfers"),
                "errors": stats.get("errors"),
            })
        if errors and (meta["status"] == "error" or (stats or {}).get("errors")):
            out["last_errors"] = errors
    else:
        prog = rsync_progress(lines)
        if prog:
            out.update(prog)
        total = meta.get("source_bytes")
        if total and not meta.get("dry_run"):
            if meta["status"] == "done":
                out.update({"percent": 100.0, "bytes": total, "bytes_human": _human(total)})
            else:
                from .local import tree_size
                now = tree_size(meta["dest_dir"], budget_s=1.0)
                if now is not None:
                    copied = max(0, min(total, now - (meta.get("dest_bytes_before") or 0)))
                    out.update({"percent": round(100 * copied / total, 1), "bytes": copied, "bytes_human": _human(copied), "total_bytes": total, "total_human": _human(total)})
        if meta["status"] == "error":
            out["last_errors"] = lines[-5:]
    if meta.get("error"):
        out["error"] = meta["error"]
    out["log"] = str(jobs.log_path(job_id))
    return out


def summarise_jobs(limit: int) -> list[dict[str, Any]]:
    res = []
    for m in jobs.list_jobs(limit):
        res.append({k: m.get(k) for k in ("id", "kind", "status", "mode", "src", "dest", "dry_run", "created", "finished") if m.get(k) is not None})
    return res


async def ls(remote_path: str, recursive: bool, max_items: int) -> dict[str, Any]:
    arg, _, disp = await resolve(remote_path, source=False)
    argv = [rclone_bin(), "lsjson", arg, *_BASE, "--no-mimetype"]
    if recursive:
        argv.append("-R")
    rc, out, err = await run_capture(argv, timeout=300)
    if rc != 0:
        raise _run_failure(f"ls {disp}", rc, err)
    items = json.loads(out or "[]")
    shown = items[:max_items]
    return {"path": disp, "count": len(items), "truncated": len(items) > len(shown),
            "items": [{"path": i["Path"], "size": i["Size"] if not i["IsDir"] else None, "modified": i.get("ModTime"), "is_dir": i["IsDir"]} for i in shown]}


async def link(remote_path: str) -> dict[str, Any]:
    arg, rtype, disp = await resolve(remote_path, source=False)
    if rtype is None:
        raise FBError("link needs a 'remote:path'; local paths have no shareable link.")
    rc, out, err = await run_capture([rclone_bin(), "link", arg, *_BASE], timeout=120)
    if rc != 0:
        raise _run_failure(f"link {disp} (not every backend supports links)", rc, err)
    return {"path": disp, "url": out.strip()}


async def mkdir(remote_path: str) -> dict[str, Any]:
    arg, _, disp = await resolve(remote_path, source=False)
    rc, _, err = await run_capture([rclone_bin(), "mkdir", arg, *_BASE], timeout=120)
    if rc != 0:
        raise _run_failure(f"mkdir {disp}", rc, err)
    return {"path": disp, "created": True}


async def stat(remote_path: str) -> dict[str, Any]:
    arg, _, disp = await resolve(remote_path, source=False)
    rc, out, err = await run_capture([rclone_bin(), "lsjson", "--stat", arg, *_BASE, "--no-mimetype"], timeout=120)
    if rc in (3, 4):  # directory / file not found
        return {"path": disp, "exists": False}
    if rc != 0:
        raise _run_failure(f"stat {disp}", rc, err)
    i = json.loads(out)
    return {"path": disp, "exists": True, "is_dir": i["IsDir"], "size": None if i["IsDir"] else i["Size"], "modified": i.get("ModTime")}


_SETUP = {
    "dropbox": {"title": "Dropbox", "command": "rclone config create dropbox dropbox",
                "notes": ["Opens the browser for Dropbox consent."], "verify": "rclone lsd dropbox:"},
    "onedrive": {"title": "OneDrive", "command": "rclone config create onedrive onedrive",
                 "notes": ["Opens the browser for Microsoft consent. If it asks which drive to use, pick the personal/business drive it lists."],
                 "verify": "rclone lsd onedrive:"},
    "icloud": {
        "title": "iCloud Drive",
        "command": "rclone config   # then: n (new remote) > name: icloud > storage: iclouddrive > apple_id > password > 2FA code",
        "notes": ["Interactive: it prompts for the Apple ID password and a 2FA code in the terminal (non-interactive form: rclone config create icloud iclouddrive apple_id=YOU@icloud.com password=...). The iclouddrive backend is fragile (Apple blocks some accounts, Advanced Data Protection must be off).",
                  "On a Mac the better route needs no setup: use local_copy(src, '~/Library/Mobile Documents/com~apple~CloudDocs/<folder>/') and iCloud syncs it."],
        "verify": "rclone lsd icloud:",
    },
    "s3": {
        "title": "Amazon S3 (or S3-compatible)",
        "command": "rclone config create s3 s3 provider=AWS access_key_id=AKIA... secret_access_key=... region=us-east-1",
        "notes": ["Use an IAM key limited to the bucket. Credentials are stored only in rclone's config file.",
                  "Alternative with no stored keys: `rclone config create s3 s3 provider=AWS env_auth=true region=us-east-1` and export AWS_* variables."],
        "verify": "rclone lsd s3:",
    },
    "r2": {
        "title": "Cloudflare R2",
        "command": "rclone config create r2 s3 provider=Cloudflare access_key_id=... secret_access_key=... endpoint=https://<ACCOUNT_ID>.r2.cloudflarestorage.com acl=private",
        "notes": ["Create an R2 API token (Object Read & Write) in the Cloudflare dashboard to get the key pair."],
        "verify": "rclone lsd r2:",
    },
    "sftp": {
        "title": "SFTP / SSH server / NAS",
        "command": "rclone config create nas sftp host=HOSTNAME user=USERNAME key_file=~/.ssh/id_ed25519",
        "notes": ["Add port=2222 if not 22. Use pass=$(rclone obscure 'PASSWORD') instead of key_file for password logins."],
        "verify": "rclone lsd nas:",
    },
}
_ALIASES = {"icloud drive": "icloud", "iclouddrive": "icloud",
            "one drive": "onedrive", "s3-compatible": "s3", "aws": "s3", "cloudflare": "r2", "ssh": "sftp", "nas": "sftp"}


async def setup_instructions(backend: str | None) -> dict[str, Any]:
    key = (backend or "").strip().lower()
    key = _ALIASES.get(key, key)
    try:
        configured = await list_remotes()
    except FBError:
        configured = {}
    if key not in _SETUP:
        return {"backends": sorted(_SETUP), "configured_remotes": configured,
                "hint": "Call again with backend set to one of the listed names."}
    info = dict(_SETUP[key])
    info["backend"] = key
    info["who_runs_it"] = "the user (needs their consent/credentials). The server never configures real accounts itself."
    info["configured_remotes"] = configured
    info["after"] = "Then remotes() should list it, and transfer(src='~/file.zip', dest='<name>:folder') works."
    return info
