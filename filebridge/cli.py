"""`filebridge` command line: the same actions as the MCP tools (both call filebridge.core).

filebridge mcp                      run the MCP server on stdio (what Claude Code / Codex launch)
filebridge <command> [--json] ...   everything else; scripts and launchd jobs can use it without MCP
"""

import argparse
import asyncio
import json
import sys
import time
from typing import Any

from . import core, policy
from .common import FBError


def render(v: Any, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(v, dict):
        lines = []
        for k, x in v.items():
            if x is None:
                continue
            if isinstance(x, (dict, list)) and x:
                lines.append(f"{pad}{k}:")
                lines.append(render(x, indent + 1))
            else:
                lines.append(f"{pad}{k}: {x}")
        return "\n".join(lines)
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                out.append(pad + "- " + "  ".join(f"{k}={val}" for k, val in x.items() if val is not None and not isinstance(val, (dict, list))))
            else:
                out.append(f"{pad}- {x}")
        return "\n".join(out)
    return f"{pad}{v}"


async def wait_for_job(job_id: str, quiet: bool) -> dict[str, Any]:
    while True:
        st = await core.job_status(job_id)
        if st["status"] != "running":
            return st
        if not quiet:
            bits = [st.get("percent") is not None and f"{st['percent']}%", st.get("bytes_human"), st.get("speed_human")]
            print(" ".join(str(b) for b in bits if b) or "running...", file=sys.stderr)
        await asyncio.sleep(1)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable JSON output")
    common.add_argument("--allow-path", action="append", default=[], metavar="DIR",
                        help="also allow reading files under DIR for this run (repeatable)")
    p = argparse.ArgumentParser(prog="filebridge", description="Move files of any size anywhere (rclone, headless browser uploads, local helpers).")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def add(name, help_, **kw):
        sp = sub.add_parser(name, parents=[common], help=help_, description=help_, **kw)
        return sp

    sp = add("mcp", "run the MCP server on stdio")
    sp.add_argument("--slim", action="store_true", help="expose only the upload tools (saves agent context)")
    add("remotes", "list configured rclone remotes")
    sp = add("transfer", "copy/move/sync between local and remotes as a background job")
    sp.add_argument("src"); sp.add_argument("dest")
    sp.add_argument("--mode", choices=["copy", "move", "sync"], default="copy")
    sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--overwrite", action="store_true")
    sp.add_argument("--confirm", action="store_true", help="sync: skip the dry-run requirement")
    sp.add_argument("--bwlimit", help="bytes/s cap, e.g. 10M")
    sp.add_argument("--wait", action="store_true", help="block until the job finishes; exit 1 if it failed")
    sp = add("status", "progress of a background job"); sp.add_argument("job")
    sp = add("jobs", "recent background jobs"); sp.add_argument("--limit", type=int, default=20)
    sp = add("cancel", "stop a running job"); sp.add_argument("job")
    sp = add("ls", "list a remote or local path"); sp.add_argument("path")
    sp.add_argument("-r", "--recursive", action="store_true"); sp.add_argument("--max", type=int, default=200)
    sp = add("link", "shareable link for a remote file/folder"); sp.add_argument("path")
    sp = add("mkdir", "create a remote folder"); sp.add_argument("path")
    sp = add("stat", "does a remote path exist, size, type"); sp.add_argument("path")
    sp = add("setup", "exact commands to add a remote (drive, dropbox, onedrive, icloud, s3, r2, sftp)"); sp.add_argument("backend", nargs="?")
    sp = add("copy", "local rsync copy as a background job")
    sp.add_argument("src"); sp.add_argument("dest")
    sp.add_argument("--overwrite", action="store_true"); sp.add_argument("--dry-run", action="store_true"); sp.add_argument("--wait", action="store_true")
    sp = add("clipboard", "put files on the macOS pasteboard"); sp.add_argument("paths", nargs="+")
    sp = add("reveal", "show a path in Finder (opens a window)"); sp.add_argument("path")

    def verify_args(sp):
        sp.add_argument("--verify-text", help="after the action, wait until this text shows on the page")
        sp.add_argument("--verify-selector", help="after the action, wait until this selector matches")
        sp.add_argument("--verify-timeout", type=float, default=30)

    sp = add("set-files", "attach local files to a file input in the headless browser (no size cap)")
    sp.add_argument("--tab", required=True, help="tab id or part of its url/title")
    sp.add_argument("--selector", help="CSS selector of the <input type=file>")
    sp.add_argument("--ref", help="element ref from `browser snapshot`")
    verify_args(sp)
    sp.add_argument("paths", nargs="+")
    sp = add("serve", "serve files to your own signed-in browser (any size) and print a JS snippet that uploads them")
    sp.add_argument("--selector", help="CSS selector of the <input type=file> to fill; omit to drop on the page")
    sp.add_argument("--port", type=int, default=0)
    sp.add_argument("--ttl", type=float, default=1800, help="seconds before the server stops (it also stops once every file was fetched)")
    sp.add_argument("paths", nargs="+")
    sp = add("drop", "drag-and-drop local files onto a drop zone")
    sp.add_argument("--tab", required=True); sp.add_argument("--selector"); sp.add_argument("--ref")
    sp.add_argument("--x", type=float); sp.add_argument("--y", type=float)
    sp.add_argument("--method", choices=["auto", "cdp", "synthetic"], default="auto")
    verify_args(sp)
    sp.add_argument("paths", nargs="+")

    b = sub.add_parser("browser", help="manage the dedicated automation browser").add_subparsers(dest="bcmd", metavar="action", required=True)
    sp = b.add_parser("launch", parents=[common], help="start/reuse it (headless unless --visible)")
    sp.add_argument("--url"); sp.add_argument("--browser", choices=["auto", "brave", "chrome"], default="auto")
    sp.add_argument("--visible", action="store_true", help="show a window; ONLY for the one-time human sign-in to a site")
    sp = b.add_parser("attach", parents=[common], help="use your already-running Chrome/Brave (started with --remote-debugging-port); never launches or closes it")
    sp.add_argument("--cdp-url", help="loopback DevTools endpoint (default: try http://127.0.0.1:9222, then DevToolsActivePort)")
    sp.add_argument("--url", help="open this page in a new tab")
    b.add_parser("close", parents=[common], help="close it (attach mode: detach only)")
    sp = b.add_parser("snapshot", parents=[common], help="list interactive elements with refs (e12) for --ref")
    sp.add_argument("--tab", required=True); sp.add_argument("--max", type=int, default=300)
    sp = b.add_parser("navigate", parents=[common], help="point an existing tab at a URL")
    sp.add_argument("--tab", required=True); sp.add_argument("url"); sp.add_argument("--timeout", type=float, default=30)
    sp = b.add_parser("text", parents=[common], help="read the visible text of a page or element")
    sp.add_argument("--tab", required=True); sp.add_argument("--selector"); sp.add_argument("--max-chars", type=int, default=8000)
    sp = b.add_parser("screenshot", parents=[common], help="save a PNG of a tab (the browser is headless)")
    sp.add_argument("--tab", required=True); sp.add_argument("--path"); sp.add_argument("--full-page", action="store_true")
    sp = b.add_parser("type", parents=[common], help="type into a field")
    sp.add_argument("--tab", required=True); sp.add_argument("--selector"); sp.add_argument("--ref"); sp.add_argument("text")
    sp.add_argument("--clear", action="store_true"); sp.add_argument("--enter", action="store_true")
    b.add_parser("tabs", parents=[common], help="list tabs")
    sp = b.add_parser("click", parents=[common], help="click an element; --file fills a file chooser it opens")
    sp.add_argument("--tab", required=True); sp.add_argument("--selector"); sp.add_argument("--ref"); sp.add_argument("--file", action="append", dest="files")
    sp = b.add_parser("wait", parents=[common], help="wait for a selector or text")
    sp.add_argument("--tab", required=True); sp.add_argument("what", help="CSS selector or text"); sp.add_argument("--timeout", type=float, default=60)
    return p


async def dispatch(a: argparse.Namespace) -> tuple[dict[str, Any], int]:
    c = a.cmd
    if c == "remotes": return await core.remotes(), 0
    if c in ("transfer", "copy"):
        if c == "transfer":
            r = await core.transfer(a.src, a.dest, a.mode, a.dry_run, a.overwrite, a.confirm, a.bwlimit)
        else:
            r = await core.local_copy(a.src, a.dest, a.overwrite, a.dry_run)
        if a.wait:
            st = await wait_for_job(r["job_id"], a.json)
            return st, 0 if st["status"] == "done" else 1
        return r, 0
    if c == "status":
        st = await core.job_status(a.job)
        return st, 1 if st["status"] in ("error", "cancelled") else 0
    if c == "jobs": return await core.jobs(a.limit), 0
    if c == "cancel": return await core.cancel(a.job), 0
    if c == "ls": return await core.ls(a.path, a.recursive, a.max), 0
    if c == "link": return await core.link(a.path), 0
    if c == "mkdir": return await core.mkdir(a.path), 0
    if c == "stat": return await core.stat(a.path), 0
    if c == "setup": return await core.setup_instructions(a.backend), 0
    if c == "clipboard": return await core.clipboard_files(a.paths), 0
    if c == "reveal": return await core.reveal(a.path), 0
    if c == "set-files":
        r = await core.browser_set_files(a.tab, a.paths, a.selector, a.ref, a.verify_text, a.verify_selector, a.verify_timeout)
        return r, 0 if r.get("ok") and r.get("verified", True) else 1
    if c == "drop":
        r = await core.browser_drop_files(a.tab, a.paths, a.selector, a.x, a.y, a.method, a.ref, a.verify_text, a.verify_selector, a.verify_timeout)
        return r, 0 if r.get("ok") and r.get("verified", True) else 1
    if c == "browser":
        if a.bcmd == "launch": return await core.browser_launch(a.url, a.browser, "visible" if a.visible else "headless"), 0
        if a.bcmd == "attach": return await core.browser_launch(a.url, "auto", "attach", a.cdp_url), 0
        if a.bcmd == "close": return await core.browser_close(), 0
        if a.bcmd == "snapshot": return await core.browser_snapshot(a.tab, a.max), 0
        if a.bcmd == "tabs": return await core.browser_tabs(), 0
        if a.bcmd == "navigate": return await core.browser_navigate(a.tab, a.url, a.timeout), 0
        if a.bcmd == "text": return await core.browser_text(a.tab, a.selector, a.max_chars), 0
        if a.bcmd == "screenshot": return await core.browser_screenshot(a.tab, a.path, a.full_page), 0
        if a.bcmd == "type": return await core.browser_type(a.tab, a.selector, a.text, a.clear, a.enter, a.ref), 0
        if a.bcmd == "click": return await core.browser_click(a.tab, a.selector, a.files, a.ref), 0
        if a.bcmd == "wait":
            r = await core.browser_wait(a.tab, a.what, a.timeout)
            return r, 0 if r.get("found") else 1
    if c == "serve":
        srv = await core.upload_own(a.paths, a.selector, a.ttl, a.port)
        info = srv.info()
        if a.json:
            print(json.dumps(info), flush=True)
        else:
            print("urls (each works once):\n" + "\n".join("  " + u for u in info["urls"]) + "\n" + "".join(f"warning: {w}\n" for w in info.get("warnings", []))
                  + "snippet:\n" + info["snippet"], flush=True)
        st = await asyncio.to_thread(srv.wait)
        return st, 0 if st["served"] == st["total"] else 1
    raise AssertionError(c)


async def tty_approve(question: str) -> bool | None:
    if not sys.stdin.isatty():
        return None
    print(question + " [y/N] ", end="", file=sys.stderr, flush=True)
    return (await asyncio.to_thread(sys.stdin.readline)).strip().lower() in ("y", "yes")


async def run(a: argparse.Namespace) -> tuple[dict[str, Any], int]:
    policy.approver.set(tty_approve)
    return await dispatch(a)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.cmd is None:
        parser.print_help()
        return 2
    try:
        for d in a.allow_path:
            policy.add_run_root(d)
        if a.cmd == "mcp":
            from .server import main as serve
            serve(a.slim)
            return 0
        result, code = asyncio.run(run(a))
    except FBError as e:
        print(json.dumps(e.to_dict()) if a.json else f"error [{e.code}]: {e.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(json.dumps(result, indent=2) if a.json else render(result))
    return code


if __name__ == "__main__":
    sys.exit(main())
