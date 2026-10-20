"""Lane 3: local helpers (rsync copy, clipboard files, reveal in the file manager).

macOS is the tested platform. Linux (xclip / wl-copy, xdg-open) and Windows (PowerShell Set-Clipboard, explorer)
are best effort and untested."""

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from . import jobs, policy
from .common import FBError, find_tool, local_path, run_capture

# Paths go in as argv, never interpolated into the script, so there is no injection surface.
_JXA_WRITE = """
ObjC.import('AppKit');
function run(argv) {
  const pb = $.NSPasteboard.generalPasteboard;
  pb.clearContents;
  const urls = $.NSMutableArray.alloc.init;
  argv.forEach(p => urls.addObject($.NSURL.fileURLWithPath(p)));
  return pb.writeObjects(urls) ? 'ok' : 'fail';
}
"""

_JXA_READ = """
ObjC.import('AppKit');
function run() {
  const pb = $.NSPasteboard.generalPasteboard;
  const types = ObjC.deepUnwrap(pb.types) || [];
  const opts = $.NSDictionary.dictionaryWithObjectForKey($(true), 'NSPasteboardURLReadingFileURLsOnlyKey');
  const urls = pb.readObjectsForClassesOptions($([$.NSURL]), opts);
  const files = [];
  if (urls) for (let i = 0; i < urls.count; i++) files.push(ObjC.unwrap(urls.objectAtIndex(i).path));
  return JSON.stringify({types: types, files: files});
}
"""


def tree_size(path: str, budget_s: float = 2.0) -> int | None:
    """Sum of file sizes under path; None if it takes too long (huge trees)."""
    if os.path.isfile(path):
        return os.path.getsize(path)
    deadline = time.time() + budget_s
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
        if time.time() > deadline:
            return None
    return total


async def local_copy(src: str, dest: str, overwrite: bool, dry_run: bool) -> dict[str, Any]:
    d = local_path(dest)
    real = (await policy.check_sources([src], action="copy", destination=str(d)))[0]
    trailing = real.endswith("/")
    s = Path(real.rstrip("/") or "/")
    dest_slash = dest.strip().endswith("/")
    if str(s) == str(d):
        raise FBError("Source and destination are the same.", "InvalidArgument")
    if not dry_run:
        # rsync creates only the last path component; make the parents (never deletes anything).
        (d if dest_slash else d.parent).mkdir(parents=True, exist_ok=True)
    # Trailing slash on a source directory means "its contents", as in rsync itself.
    s_arg = str(s) + ("/" if trailing and s.is_dir() else "")
    d_arg = str(d) + ("/" if dest_slash else "")
    argv = [find_tool("rsync", ("/usr/bin/rsync",)), "-a", "--partial", "--progress"]
    if not overwrite:
        argv.append("--ignore-existing")
    if dry_run:
        argv.append("-n")
    argv += [s_arg, d_arg]
    src_bytes = tree_size(str(s))
    # Baseline so job_status can report overall progress as (dest size now - dest size before) / source size.
    base = tree_size(str(d)) if d.exists() else 0
    meta = jobs.start_job("local_copy", argv, src=s_arg, dest=d_arg, dry_run=dry_run, overwrite=overwrite,
                          source_bytes=src_bytes, dest_dir=str(d), dest_bytes_before=base)
    return {"job_id": meta["id"], "status": "running", "src": s_arg, "dest": d_arg, "dry_run": dry_run,
            "source_bytes": src_bytes, "log": str(jobs.log_path(meta["id"]))}


async def read_pasteboard() -> dict[str, Any]:
    rc, out, err = await run_capture([find_tool("osascript", ("/usr/bin/osascript",)), "-l", "JavaScript", "-e", _JXA_READ], timeout=20)
    if rc != 0:
        raise FBError(f"Could not read the pasteboard: {err.strip()[:300]}")
    return json.loads(out)


async def clipboard_files(paths: list[str]) -> dict[str, Any]:
    resolved = list(dict.fromkeys(p.rstrip("/") for p in await policy.check_sources(paths, action="put on the clipboard", destination="the clipboard")))
    if sys.platform.startswith("linux"):
        return await _clipboard_linux(resolved)
    if sys.platform == "win32":
        return await _clipboard_windows(resolved)
    osa = find_tool("osascript", ("/usr/bin/osascript",))
    rc, out, err = await run_capture([osa, "-l", "JavaScript", "-e", _JXA_WRITE, *resolved], timeout=20)
    if rc != 0 or out.strip() != "ok":
        raise FBError(f"Writing to the pasteboard failed: {(err or out).strip()[:300]}")
    state = await read_pasteboard()
    ok = {os.path.realpath(f) for f in state["files"]} == {os.path.realpath(p) for p in resolved}
    return {"ok": ok, "files": state["files"], "pasteboard_types": state["types"],
            "next": "Cmd+V now pastes these files into Finder, Slack, Telegram Desktop, WhatsApp, Mail, etc."}


async def _clipboard_linux(resolved: list[str]) -> dict[str, Any]:
    # File managers (Nautilus, Dolphin) paste text/uri-list. Untested.
    uris = "\n".join(Path(p).as_uri() for p in resolved) + "\n"
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        argv = [find_tool("wl-copy"), "--type", "text/uri-list"]
    elif shutil.which("xclip"):
        argv = [find_tool("xclip"), "-selection", "clipboard", "-t", "text/uri-list", "-i"]
    else:
        raise FBError("Needs wl-copy (Wayland) or xclip (X11) on the PATH.", "NotInstalled")
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    # xclip/wl-copy keep running to serve the selection; only wait for them to take the data.
    proc.stdin.write(uris.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    await asyncio.sleep(0.3)
    if proc.returncode not in (None, 0):
        raise FBError(f"{Path(argv[0]).name} failed: {(await proc.stderr.read()).decode()[:300]}")
    return {"ok": None, "files": resolved, "note": "Linux support is untested: text/uri-list was written; paste in a file manager or app that accepts files."}


async def _clipboard_windows(resolved: list[str]) -> dict[str, Any]:
    # Paths go in as separate argv entries via $args, never into the script text. Untested.
    ps = find_tool("powershell", ("C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",))
    rc, _, err = await run_capture([ps, "-NoProfile", "-NonInteractive", "-Command", "Set-Clipboard -Path $args", *resolved], timeout=30)
    if rc != 0:
        raise FBError(f"Set-Clipboard failed: {err.strip()[:300]}")
    return {"ok": None, "files": resolved, "note": "Windows support is untested: Ctrl+V pastes the files in Explorer and most apps."}


async def reveal(path: str) -> dict[str, Any]:
    p = local_path(path, must_exist=True)
    if sys.platform.startswith("linux"):
        argv = [find_tool("xdg-open"), str(p if p.is_dir() else p.parent)]  # xdg-open cannot select a file; opens its folder. Untested.
    elif sys.platform == "win32":
        argv = ["explorer", f"/select,{p}"]  # explorer exits 1 even on success. Untested.
    else:
        argv = [find_tool("open", ("/usr/bin/open",)), "-R", str(p)]
    rc, _, err = await run_capture(argv, timeout=15)
    if rc != 0 and sys.platform != "win32":
        raise FBError(f"{Path(argv[0]).name} failed: {err.strip()[:300]}")
    return {"revealed": str(p)}
