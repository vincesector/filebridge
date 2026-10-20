"""Lane 3: macOS helpers (rsync copy, pasteboard, Finder reveal)."""

import os
import time
from typing import Any

from . import jobs
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
    trailing = src.strip().endswith("/")
    s = local_path(src, must_exist=True, source=True)
    d = local_path(dest)
    dest_slash = dest.strip().endswith("/")
    if str(s) == str(d):
        raise FBError("Source and destination are the same.")
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
    import json
    rc, out, err = await run_capture([find_tool("osascript", ("/usr/bin/osascript",)), "-l", "JavaScript", "-e", _JXA_READ], timeout=20)
    if rc != 0:
        raise FBError(f"Could not read the pasteboard: {err.strip()[:300]}")
    return json.loads(out)


async def clipboard_files(paths: list[str]) -> dict[str, Any]:
    if not paths:
        raise FBError("Give at least one path.")
    resolved = list(dict.fromkeys(str(local_path(p, must_exist=True, source=True)) for p in paths))
    osa = find_tool("osascript", ("/usr/bin/osascript",))
    rc, out, err = await run_capture([osa, "-l", "JavaScript", "-e", _JXA_WRITE, *resolved], timeout=20)
    if rc != 0 or out.strip() != "ok":
        raise FBError(f"Writing to the pasteboard failed: {(err or out).strip()[:300]}")
    state = await read_pasteboard()
    ok = {os.path.realpath(f) for f in state["files"]} == {os.path.realpath(p) for p in resolved}
    return {"ok": ok, "files": state["files"], "pasteboard_types": state["types"],
            "next": "Cmd+V now pastes these files into Finder, Slack, Telegram Desktop, WhatsApp, Mail, etc."}


async def reveal(path: str) -> dict[str, Any]:
    p = local_path(path, must_exist=True)
    rc, _, err = await run_capture([find_tool("open", ("/usr/bin/open",)), "-R", str(p)], timeout=15)
    if rc != 0:
        raise FBError(f"open -R failed: {err.strip()[:300]}")
    return {"revealed": str(p)}
