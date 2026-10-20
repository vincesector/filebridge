"""filebridge MCP server (stdio). Upload any-size files into the browser you're already signed in to, and move files anywhere.

Lane 1: rclone (cloud storage, background jobs)      Lane 2: CDP browser uploads with real local paths
Lane 3: your own signed-in browser via localhost     Lane 4: local helpers (rsync, clipboard, reveal)
"""

import functools
import json
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import Context, MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import core, policy
from .common import FBError

mcp = MCPServer(
    "filebridge",
    instructions=(
        "Upload to a site the user is signed in to in their own browser: browser_upload_own (returns a JS snippet to run in that tab). "
        "Upload in the automation browser (or one you attached to): browser_launch, browser_snapshot, then browser_set_files / browser_drop_files / "
        "browser_click(files=) with a ref or selector. Destination with an API/rclone backend: transfer(). Local-only: local_copy, clipboard_files, reveal. "
        "Files outside the user's allowed folders need their approval; credentials (.ssh, .env, keys...) are always refused (code PathBlocked). "
        "Errors come back as {ok:false, code, message}. Long jobs return a job_id; poll job_status. Nothing here deletes files."
    ),
)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
WRITE_LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

SLIM_TOOLS = {"browser_upload_own", "serve_status", "serve_stop", "browser_set_files", "browser_drop_files", "browser_snapshot",
              "browser_tabs", "browser_navigate", "browser_launch"}
_names: list[str] = []


class Approval(BaseModel):
    allow: bool = Field(description="Allow it for this one action")


def _elicitor(ctx: Context):
    async def ask(question: str) -> bool | None:
        caps = ctx.session.client_capabilities
        if caps is None or caps.elicitation is None:
            return None
        try:
            r = await ctx.elicit(question, Approval)
        except MCPError:  # client declared elicitation but cannot deliver it here
            return None
        except ValueError:  # accepted with malformed content: treat as no
            return False
        return r.action == "accept" and r.data.allow
    return ask


def tool(**kw):
    """mcp.tool plus: FBError -> {ok:false, code, message} (isError), and file-approval prompts routed to this client."""
    def deco(fn):
        @functools.wraps(fn)
        async def run(*a, **k):
            ctx = next((v for v in k.values() if isinstance(v, Context)), None)
            token = policy.approver.set(_elicitor(ctx)) if ctx else policy.approver.set(None)
            try:
                return await fn(*a, **k)
            except FBError as e:
                d = e.to_dict()
                return CallToolResult(content=[TextContent(type="text", text=json.dumps(d))], structured_content=d, is_error=True)
            finally:
                policy.approver.reset(token)
        _names.append(kw.get("name") or fn.__name__)
        return mcp.tool(**kw)(run)
    return deco


Tab = Annotated[str, Field(description="Tab id from browser_tabs(), or a unique part of its url/title (e.g. 'example.com/upload')")]
Paths = Annotated[list[str], Field(description="Absolute or ~ local file paths", min_length=1, max_length=200)]
Ref = Annotated[str | None, Field(description="Element ref from browser_snapshot (e.g. 'e12'), instead of a selector")]
VerifyText = Annotated[str | None, Field(description="After the action, wait until this text shows on the page (e.g. the file name or 'Upload complete'); result gets verified=true/false")]
VerifySelector = Annotated[str | None, Field(description="After the action, wait until this CSS selector matches; result gets verified=true/false")]
VerifyTimeout = Annotated[float, Field(ge=1, le=3600, description="Seconds to wait for verify_text / verify_selector")]


# ------------------------------------------------------------------ Lane 1: rclone

@tool(annotations=LOCAL_RO)
async def remotes() -> dict[str, Any]:
    """List configured rclone remotes and their backend types (never prints tokens).
    If the one you need is missing, call setup_instructions(backend)."""
    return await core.remotes()


@tool(annotations=WRITE)
async def transfer(
    ctx: Context,
    src: Annotated[str, Field(description="Local path (~ or absolute) or 'remote:path'. File or folder.")],
    dest: Annotated[str, Field(description="Local path or 'remote:path'. Always treated as a DIRECTORY the source lands in.")],
    mode: Annotated[Literal["copy", "move", "sync"], Field(description="copy keeps the source. move deletes the source after a verified copy. sync makes dest mirror src and DELETES extra files on dest (needs a prior dry run or confirm=true).")] = "copy",
    dry_run: Annotated[bool, Field(description="Show what would happen without changing anything")] = False,
    overwrite: Annotated[bool, Field(description="Replace files that already exist on the destination. Default false skips them.")] = False,
    confirm: Annotated[bool, Field(description="sync only: skip the dry-run requirement")] = False,
    bwlimit: Annotated[str | None, Field(description="Optional cap in bytes/s, e.g. '10M', so a big upload does not saturate the link")] = None,
) -> dict[str, Any]:
    """Copy/move/sync files or folders between local disk and cloud remotes with rclone, any size, resumable.
    Runs in the BACKGROUND and returns a job_id immediately; poll job_status(job_id).
    Examples: transfer('~/Videos/final.mp4', 'nas:uploads'); transfer('r2:reports', '~/Downloads/reports')."""
    return await core.transfer(src, dest, mode, dry_run, overwrite, confirm, bwlimit)


@tool(annotations=LOCAL_RO)
async def job_status(job_id: Annotated[str, Field(description="Id returned by transfer or local_copy")]) -> dict[str, Any]:
    """Progress of a background job: state (running/done/error/cancelled), percent, bytes, speed, ETA, last error lines, log path."""
    return await core.job_status(job_id)


@tool(name="jobs", annotations=LOCAL_RO)
async def jobs_list(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
    """List recent background jobs, newest first."""
    return await core.jobs(limit)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def cancel(job_id: str) -> dict[str, Any]:
    """Stop a running job (sends SIGTERM to its runner; partial uploads are not deleted)."""
    return await core.cancel(job_id)


@tool(annotations=RO)
async def ls(
    remote_path: Annotated[str, Field(description="'remote:path' (a local path also works)")],
    recursive: bool = False,
    max: Annotated[int, Field(ge=1, le=5000, description="Max entries returned")] = 200,
) -> dict[str, Any]:
    """List names, sizes and modification times at a remote (or local) path."""
    return await core.ls(remote_path, recursive, max)


@tool(annotations=RO)
async def link(remote_path: Annotated[str, Field(description="'remote:path' of a file or folder")]) -> dict[str, Any]:
    """Create/get a shareable link for a remote file or folder (backend must support it: Dropbox, OneDrive, S3-compatible...)."""
    return await core.link(remote_path)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def mkdir(remote_path: Annotated[str, Field(description="'remote:path' of the folder to create")]) -> dict[str, Any]:
    """Create a folder (and parents) on a remote. Succeeds quietly if it already exists."""
    return await core.mkdir(remote_path)


@tool(annotations=RO)
async def stat(remote_path: Annotated[str, Field(description="'remote:path'")]) -> dict[str, Any]:
    """Check whether a remote file or folder exists and get its size/type/modified time."""
    return await core.stat(remote_path)


@tool(annotations=LOCAL_RO)
async def setup_instructions(
    backend: Annotated[str | None, Field(description="dropbox, onedrive, icloud, s3, r2, sftp Omit to list.")] = None,
) -> dict[str, Any]:
    """Exact commands a human runs to add a remote (OAuth/credentials are the human's job; this server never configures accounts)."""
    return await core.setup_instructions(backend)


# ------------------------------------------------------------------ Lane 2: browser (CDP)

@tool(annotations=WRITE)
async def browser_launch(
    url: Annotated[str | None, Field(description="Optional page to open (new tab if already running)")] = None,
    browser: Annotated[Literal["auto", "brave", "chrome"], Field(description="auto = Brave, falling back to Chrome (own modes only)")] = "auto",
    mode: Annotated[Literal["headless", "visible", "attach"], Field(description="headless (default): dedicated profile, nothing on screen. visible: same profile with a window, ONLY for the one-time human sign-in. attach: connect to the user's already-running Chrome/Brave started with --remote-debugging-port (never launched or closed by filebridge).")] = "headless",
    cdp_url: Annotated[str | None, Field(description="attach only: loopback DevTools endpoint, e.g. http://127.0.0.1:9222 or ws://127.0.0.1:PORT/devtools/browser/ID. Omit to try :9222, then Chrome/Brave's DevToolsActivePort file.")] = None,
) -> dict[str, Any]:
    """Start (or reuse) the DEDICATED automation browser, HEADLESS by default (profile ~/.filebridge/browser-profile, DevTools on 127.0.0.1:9229),
    or attach to the user's running browser with mode='attach'. Sessions saved in the dedicated profile (after a one-time mode='visible' sign-in) work headless afterwards."""
    return await core.browser_launch(url, browser, mode, cdp_url)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def browser_close() -> dict[str, Any]:
    """Close the dedicated automation browser (profile and logins are kept). In attach mode this only detaches; the user's browser stays open."""
    return await core.browser_close()


@tool(annotations=LOCAL_RO)
async def browser_tabs() -> dict[str, Any]:
    """List open tabs (id, title, url) in the automation browser or the attached browser."""
    return await core.browser_tabs()


@tool(annotations=LOCAL_RO)
async def browser_snapshot(
    tab: Tab,
    max_items: Annotated[int, Field(ge=1, le=2000, description="Cap on listed elements")] = 300,
) -> dict[str, Any]:
    """Compact list of the page's interactive elements (role, name, text, ref like 'e12'): buttons, links, fields, file inputs (even hidden)
    and drop zones (elements with drop/dragover listeners or upload text). Pass a ref to browser_set_files / browser_drop_files / browser_click / browser_type."""
    return await core.browser_snapshot(tab, max_items)


@tool(annotations=WRITE)
async def browser_set_files(
    ctx: Context,
    tab: Tab,
    paths: Paths,
    selector: Annotated[str | None, Field(description="CSS selector of an <input type=file>, hidden ones are fine. Omit (and omit ref) to auto-pick when the page has exactly one.")] = None,
    ref: Ref = None,
    verify_text: VerifyText = None,
    verify_selector: VerifySelector = None,
    verify_timeout: VerifyTimeout = 30,
) -> dict[str, Any]:
    """Attach local files to a file input via CDP DOM.setFileInputFiles. The browser reads the files from disk: no 10 MB cap, any size.
    Searches shadow roots and iframes. If nothing matches, returns the candidate inputs it found. Verifies names and sizes in the page."""
    return await core.browser_set_files(tab, paths, selector, ref, verify_text, verify_selector, verify_timeout)


@tool(annotations=WRITE)
async def browser_drop_files(
    ctx: Context,
    tab: Tab,
    paths: Paths,
    selector: Annotated[str | None, Field(description="Drop zone selector; the drop lands at its centre")] = None,
    ref: Ref = None,
    x: Annotated[float | None, Field(description="Viewport x, used when no selector/ref")] = None,
    y: Annotated[float | None, Field(description="Viewport y, used when no selector/ref")] = None,
    method: Annotated[Literal["auto", "cdp", "synthetic"], Field(description="auto = real CDP drag events, falling back to in-page DataTransfer if the page saw no files")] = "auto",
    verify_text: VerifyText = None,
    verify_selector: VerifySelector = None,
    verify_timeout: VerifyTimeout = 30,
) -> dict[str, Any]:
    """Drag and drop local files onto a page's drop zone with real CDP drag events (Input.dispatchDragEvent) carrying disk paths.
    Reports which method worked and what the page received."""
    return await core.browser_drop_files(tab, paths, selector, x, y, method, ref, verify_text, verify_selector, verify_timeout)


@tool(annotations=WRITE)
async def browser_click(
    ctx: Context,
    tab: Tab,
    selector: Annotated[str | None, Field(description="CSS selector of what to click (searches shadow roots and same-origin iframes)")] = None,
    ref: Ref = None,
    files: Annotated[list[str] | None, Field(description="If the click opens a native file chooser (e.g. an 'Upload file' menu item), fill it with these paths")] = None,
) -> dict[str, Any]:
    """Real mouse click at the element's centre (give selector or ref). File choosers are always intercepted (no native dialog ever appears);
    pass files=[...] to fill them. Refuses to click an <input type=file> directly (use browser_set_files)."""
    return await core.browser_click(tab, selector, files, ref)


@tool(annotations=LOCAL_RO)
async def browser_wait(
    tab: Tab,
    selector_or_text: Annotated[str, Field(description="CSS selector OR visible text, e.g. 'Upload complete'")],
    timeout: Annotated[float, Field(ge=1, le=3600, description="Seconds to wait")] = 60,
) -> dict[str, Any]:
    """Wait until a CSS selector matches or the text appears anywhere on the page (shadow roots and iframes included)."""
    return await core.browser_wait(tab, selector_or_text, timeout)


@tool(annotations=WRITE)
async def browser_navigate(tab: Tab, url: Annotated[str, Field(description="http(s):// or file:// URL")], timeout: Annotated[float, Field(ge=1, le=300)] = 30) -> dict[str, Any]:
    """Navigate an existing tab to a URL and wait for it to load (avoids piling up new tabs). Warns (NotSignedIn) when it lands on a sign-in page."""
    return await core.browser_navigate(tab, url, timeout)


@tool(annotations=LOCAL_RO)
async def browser_text(
    tab: Tab,
    selector: Annotated[str | None, Field(description="Limit to this element; omit for the whole page")] = None,
    max_chars: Annotated[int, Field(ge=100, le=200000)] = 8000,
) -> dict[str, Any]:
    """Read the visible text of the page (or one element). The browser is headless, so this is how you see status messages, errors and login walls."""
    return await core.browser_text(tab, selector, max_chars)


@tool(annotations=WRITE_LOCAL)
async def browser_screenshot(
    tab: Tab,
    path: Annotated[str | None, Field(description="Where to save the PNG (absolute or ~; must not exist). Default: ~/.filebridge/screenshots/")] = None,
    full_page: bool = False,
) -> dict[str, Any]:
    """Save a PNG screenshot of the tab and return its path, so you can look at what the headless browser sees."""
    return await core.browser_screenshot(tab, path, full_page)


@tool(annotations=WRITE)
async def browser_type(
    tab: Tab,
    text: str,
    selector: Annotated[str | None, Field(description="CSS selector of the field to type into")] = None,
    ref: Ref = None,
    clear: Annotated[bool, Field(description="Select and delete the existing value first")] = False,
    press_enter: Annotated[bool, Field(description="Press Enter afterwards")] = False,
) -> dict[str, Any]:
    """Type text into a field (selector or ref) with real input events (for titles/descriptions that must be filled before an upload)."""
    return await core.browser_type(tab, selector, text, clear, press_enter, ref)


# ------------------------------------------------------------------ Lane 3: the user's own signed-in browser

@tool(annotations=WRITE_LOCAL)
async def browser_upload_own(
    ctx: Context,
    paths: Paths,
    selector: Annotated[str | None, Field(description="CSS selector of the <input type=file> to fill; omit to drop the files on the element at the page centre")] = None,
    ttl: Annotated[float, Field(ge=10, le=86400, description="Seconds before the links expire (the server also stops once every file was fetched)")] = 1800,
    verify_text: Annotated[str | None, Field(description="Also return verify_snippet, which waits until this text shows on the page")] = None,
    verify_selector: Annotated[str | None, Field(description="Also return verify_snippet, which waits until this selector matches")] = None,
    verify_timeout: Annotated[float, Field(ge=1, le=3600, description="Seconds verify_snippet waits")] = 60,
) -> dict[str, Any]:
    """Upload any-size local files into a tab of the user's OWN signed-in browser (Drive, X, Typefully...). Starts a token-protected server on 127.0.0.1
    in the background and returns {server_id, urls, snippet, expires_in_s}. Run `snippet` in that tab with a tool that executes JavaScript there
    (e.g. a browser extension); it fetches the files and fills the input or drops them on the page. Each URL works once. Poll serve_status(server_id)."""
    return await core.browser_upload_own(paths, selector, ttl, verify_text, verify_selector, verify_timeout)


@tool(annotations=LOCAL_RO)
async def serve_status(server_id: Annotated[str, Field(description="server_id from browser_upload_own")]) -> dict[str, Any]:
    """State of a browser_upload_own server: running / done / expired / stopped, and per file pending / serving / served / failed."""
    return await core.serve_status(server_id)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def serve_stop(server_id: Annotated[str, Field(description="server_id from browser_upload_own")]) -> dict[str, Any]:
    """Stop a browser_upload_own server now; its links stop working."""
    return await core.serve_stop(server_id)


# ------------------------------------------------------------------ Lane 4: local helpers

@tool(annotations=WRITE_LOCAL)
async def local_copy(
    ctx: Context,
    src: Annotated[str, Field(description="Local file or folder. A trailing '/' on a folder means 'its contents' (rsync semantics)")],
    dest: Annotated[str, Field(description="Local destination: external drive, NAS mount, ~/Library/Mobile Documents/com~apple~CloudDocs/<folder>/ for iCloud Drive")],
    overwrite: Annotated[bool, Field(description="Replace files that exist at the destination (default skips them)")] = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """rsync -a --partial copy between local locations, as a background job (returns job_id; poll job_status). Never deletes."""
    return await core.local_copy(src, dest, overwrite, dry_run)


@tool(annotations=WRITE_LOCAL)
async def clipboard_files(ctx: Context, paths: Paths) -> dict[str, Any]:
    """Put files on the clipboard as file references (replaces the clipboard). Cmd+V then pastes them into Finder, Slack, Telegram Desktop, WhatsApp, Mail...
    macOS tested; Linux (xclip/wl-copy) and Windows (Set-Clipboard) untested."""
    return await core.clipboard_files(paths)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def reveal(path: Annotated[str, Field(description="Local file or folder to show in the file manager")]) -> dict[str, Any]:
    """Reveal a path in Finder (open -R) or the platform's file manager. This opens a window on the user's screen."""
    return await core.reveal(path)


def main(slim: bool = False) -> None:
    if slim:
        for name in _names:
            if name not in SLIM_TOOLS:
                mcp.remove_tool(name)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
