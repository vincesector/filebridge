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
        "filebridge moves local files of any size where agents usually cannot: website upload forms, drop zones, cloud storage, "
        "drives. Pick by destination. Website where the user is signed in to their own browser (Drive, X, Typefully, a form), or the "
        "browser extension's upload fails / the file is over 10 MB: browser_upload_own, then run its `snippet` in that tab with your "
        "JavaScript tool (top-level await, pass as is; links work once). Website in the automation browser or an attached "
        "Chrome/Brave: browser_launch, browser_snapshot, then browser_set_files (file input) / browser_drop_files (drop zone) / "
        "browser_click(files=) (upload button that opens a file picker), with a ref or selector. Cloud or NAS with an rclone remote: "
        "remotes, then transfer (background job; poll job_status). Drives, NAS mounts, iCloud Drive folder: local_copy. Pasting into a"
        " desktop app: clipboard_files. Files outside the user's allowed folders need their approval; credentials (.ssh, .env, "
        "keys...) are always refused (code PathBlocked). Errors come back as {ok:false, code, message}. Nothing here deletes files."
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
    """Use first when the user wants files on cloud storage or a NAS (S3, R2, Dropbox, OneDrive, SFTP, Drive...) and you need the
    'remote:' name to pass to transfer / ls / link. Lists the rclone remotes already configured, with their backend type
    (tokens are never printed). Returns {remotes: [{name, type, use_as: 'name:path'}], count}. Empty or missing the one you need:
    call setup_instructions(backend) and hand the command to the user; you cannot configure accounts yourself."""
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
    """Use when files must go to (or come from) cloud storage or a NAS that has an rclone remote: S3/R2 buckets, SFTP/NAS,
    Dropbox, OneDrive, Drive... Prefer it over a website upload whenever the destination has a remote, and for multi-GB files.
    Copies, moves or syncs a file or folder, any size, resumable, with rclone. Runs in the BACKGROUND: returns {job_id} at once;
    poll job_status(job_id) until done. `dest` is always a folder the source lands in. Gotchas: existing files are skipped unless
    overwrite=true; mode='move' deletes the source only after a verified copy; mode='sync' DELETES extra files on dest and needs a
    prior dry_run=true (or confirm=true); a local source outside the user's allowed folders triggers an approval prompt, and
    credential files (.ssh, .env, keys) are always refused (PathBlocked). Unknown remote: RemoteNotConfigured.
    Examples: transfer('~/Videos/final.mp4', 'nas:uploads'); transfer('r2:reports', '~/Downloads/reports')."""
    return await core.transfer(src, dest, mode, dry_run, overwrite, confirm, bwlimit)


@tool(annotations=LOCAL_RO)
async def job_status(job_id: Annotated[str, Field(description="Id returned by transfer or local_copy")]) -> dict[str, Any]:
    """Use after transfer or local_copy returned a job_id, to see whether the copy finished. Returns state
    (running / done / error / cancelled), percent, bytes, speed, ETA, the last error lines and the log path. Poll every few seconds
    for big files; do not report the upload as done until state is 'done'."""
    return await core.job_status(job_id)


@tool(name="jobs", annotations=LOCAL_RO)
async def jobs_list(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
    """Use when you lost a job_id or the user asks what is copying right now. Lists recent background jobs from transfer and
    local_copy, newest first, with their state."""
    return await core.jobs(limit)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def cancel(job_id: str) -> dict[str, Any]:
    """Use when the user wants to stop a running transfer or local_copy. Sends SIGTERM to the job's runner and returns its
    final state. Partial files already written on the destination are left in place (nothing is deleted)."""
    return await core.cancel(job_id)


@tool(annotations=RO)
async def ls(
    remote_path: Annotated[str, Field(description="'remote:path' (a local path also works)")],
    recursive: bool = False,
    max: Annotated[int, Field(ge=1, le=5000, description="Max entries returned")] = 200,
) -> dict[str, Any]:
    """Use to check what is in a cloud/NAS folder before or after a transfer (e.g. to confirm the upload landed). Lists names,
    sizes and modification times at 'remote:path' (a local path also works). recursive=true walks subfolders; max caps the entries."""
    return await core.ls(remote_path, recursive, max)


@tool(annotations=RO)
async def link(remote_path: Annotated[str, Field(description="'remote:path' of a file or folder")]) -> dict[str, Any]:
    """Use after a transfer when the user wants a URL to share the uploaded file or folder. Creates or returns a public/shareable
    link for 'remote:path'. Only works on backends that support links (Dropbox, OneDrive, Drive, S3-compatible...); otherwise an error."""
    return await core.link(remote_path)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def mkdir(remote_path: Annotated[str, Field(description="'remote:path' of the folder to create")]) -> dict[str, Any]:
    """Use when a transfer needs a destination folder that should exist first (transfer usually creates it on its own).
    Creates the folder and its parents on a remote; succeeds quietly if it already exists."""
    return await core.mkdir(remote_path)


@tool(annotations=RO)
async def stat(remote_path: Annotated[str, Field(description="'remote:path'")]) -> dict[str, Any]:
    """Use to check whether a specific remote file or folder exists (e.g. before overwriting, or to confirm an upload) and get
    its size, type and modified time."""
    return await core.stat(remote_path)


@tool(annotations=LOCAL_RO)
async def setup_instructions(
    backend: Annotated[str | None, Field(description="dropbox, onedrive, icloud, s3, r2 or sftp. Omit to list them.")] = None,
) -> dict[str, Any]:
    """Use when the destination the user wants (Dropbox, OneDrive, iCloud, S3, R2, SFTP/NAS) is not in remotes().
    Returns the exact rclone command the USER must run to add it, since OAuth and credentials are the human's job; this server never
    configures accounts. Omit backend to list the supported names. Show the command to the user; do not try to run it for them."""
    return await core.setup_instructions(backend)


# ------------------------------------------------------------------ Lane 2: browser (CDP)

@tool(annotations=WRITE)
async def browser_launch(
    url: Annotated[str | None, Field(description="Optional page to open (new tab if already running)")] = None,
    browser: Annotated[Literal["auto", "brave", "chrome"], Field(description="auto = Brave, falling back to Chrome (own modes only)")] = "auto",
    mode: Annotated[Literal["headless", "visible", "attach"], Field(description="headless (default): dedicated profile, nothing on screen. visible: same profile with a window, ONLY for the one-time human sign-in. attach: connect to the user's already-running Chrome/Brave started with --remote-debugging-port (never launched or closed by filebridge).")] = "headless",
    cdp_url: Annotated[str | None, Field(description="attach only: loopback DevTools endpoint, e.g. http://127.0.0.1:9222 or ws://127.0.0.1:PORT/devtools/browser/ID. Omit to try :9222, then Chrome/Brave's DevToolsActivePort file.")] = None,
) -> dict[str, Any]:
    """Use when a website upload should happen in an automation browser rather than the user's everyday one: sites with no
    login, sites the user signed into once in filebridge's dedicated profile, or the user's own Chrome/Brave started with
    --remote-debugging-port (mode='attach'). Starts or reuses the browser and optionally opens `url` in a new tab. Returns the mode,
    browser and tabs. Modes: 'headless' (default, dedicated profile ~/.filebridge/browser-profile, nothing on screen); 'visible' (same
    profile with a window, ONLY for the one-time human sign-in, then launch headless again; logins persist); 'attach' (loopback DevTools
    endpoint only, never launched or closed by filebridge). For a site the user is already signed in to in their normal browser,
    browser_upload_own is usually the better tool. Next: browser_snapshot, then browser_set_files / browser_drop_files / browser_click."""
    return await core.browser_launch(url, browser, mode, cdp_url)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def browser_close() -> dict[str, Any]:
    """Use when the browser work is finished and the automation browser should not keep running. Closes filebridge's
    dedicated browser; its profile and logins are kept for next time. In attach mode it only detaches; the user's browser stays open."""
    return await core.browser_close()


@tool(annotations=LOCAL_RO)
async def browser_tabs() -> dict[str, Any]:
    """Use to find the `tab` value for the other browser_* tools. Lists open tabs (id, title, url) in the automation browser
    or the attached browser. Any tool's `tab` accepts the id or a unique part of the url/title."""
    return await core.browser_tabs()


@tool(annotations=LOCAL_RO)
async def browser_snapshot(
    tab: Tab,
    max_items: Annotated[int, Field(ge=1, le=2000, description="Cap on listed elements")] = 300,
) -> dict[str, Any]:
    """Use before any upload or click in the automation/attached browser, to find the right target without guessing a
    CSS selector. Returns a compact list of interactive elements with refs (e.g. 'e12'): role, name, text for buttons, links, fields,
    file inputs (hidden ones too) and drop zones (elements with drop/dragover listeners or upload-style text). Pass the ref to
    browser_set_files / browser_drop_files / browser_click / browser_type. Refs die when the page navigates or reloads (a stale ref is
    an error, never a wrong element): snapshot again. Cross-site iframes and closed shadow roots are not listed."""
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
    """Use when a page in the automation or attached browser has an <input type=file> (visible or hidden) and you need to
    attach a local file of any size: the fix for 'file upload over 10 MB fails' and 'agents cannot use the native file picker'.
    Sets the files with CDP DOM.setFileInputFiles, so the browser reads them from disk (no size cap). Target with a ref from
    browser_snapshot or a CSS selector; with neither it acts only when the page has exactly one file input, otherwise it returns
    `candidates` and does nothing. Searches shadow roots and iframes (including cross-site). Returns {ok, files_in_page,
    sizes_match_disk, ...}; add verify_text (e.g. the file name or 'Upload complete') to also wait for the site to confirm (verified).
    Errors: SelectorNotFound (with candidates), NotSignedIn (page looks like a login wall), PathNotAllowed (user declined the approval
    prompt for a file outside their allowed folders), PathBlocked (credential files, never allowed). If the input only appears after
    clicking an 'Upload' button, use browser_click(files=...) instead."""
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
    """Use when an upload area in the automation or attached browser accepts drag-and-drop but has no usable file input
    (Drive, X, Typefully style drop zones), i.e. 'drag a local file onto the page'. Drops local files of any size with real CDP drag
    events (Input.dispatchDragEvent) carrying disk paths; if the page sees no files, method='auto' falls back to an in-page DataTransfer
    of disk-backed files (still no size cap, but isTrusted=false). Target: ref from browser_snapshot, a selector (drops at its centre)
    or x/y viewport coordinates. Returns which method worked and what the page received; add verify_text / verify_selector to wait for
    the site's confirmation (verified). Main page and same-origin iframes only. Same path rules as browser_set_files (approval outside
    allowed folders, PathBlocked for credentials)."""
    return await core.browser_drop_files(tab, paths, selector, x, y, method, ref, verify_text, verify_selector, verify_timeout)


@tool(annotations=WRITE)
async def browser_click(
    ctx: Context,
    tab: Tab,
    selector: Annotated[str | None, Field(description="CSS selector of what to click (searches shadow roots and same-origin iframes)")] = None,
    ref: Ref = None,
    files: Annotated[list[str] | None, Field(description="If the click opens a native file chooser (e.g. an 'Upload file' menu item), fill it with these paths")] = None,
) -> dict[str, Any]:
    """Use to press buttons in the automation or attached browser, and especially for 'Upload file' buttons or menu items
    that open a native file picker: pass files=[...] and the file chooser is intercepted and filled, so no OS dialog ever appears.
    Real mouse click at the element's centre; target with a ref from browser_snapshot or a CSS selector (shadow roots and same-origin
    iframes are searched). Refuses to click an <input type=file> directly: use browser_set_files for that. Returns what was clicked
    and, with files, whether the chooser opened and was filled (files_set)."""
    return await core.browser_click(tab, selector, files, ref)


@tool(annotations=LOCAL_RO)
async def browser_wait(
    tab: Tab,
    selector_or_text: Annotated[str, Field(description="CSS selector OR visible text, e.g. 'Upload complete'")],
    timeout: Annotated[float, Field(ge=1, le=3600, description="Seconds to wait")] = 60,
) -> dict[str, Any]:
    """Use after an upload or navigation to wait for the site to finish: until a CSS selector matches or a text (e.g.
    'Upload complete' or the file name) appears anywhere on the page, shadow roots and iframes included. Returns found or a Timeout
    code. Prefer verify_text on set_files/drop when you know the text in advance."""
    return await core.browser_wait(tab, selector_or_text, timeout)


@tool(annotations=WRITE)
async def browser_navigate(tab: Tab, url: Annotated[str, Field(description="http(s):// or file:// URL")], timeout: Annotated[float, Field(ge=1, le=300)] = 30) -> dict[str, Any]:
    """Use to send an existing automation/attached tab to a URL (instead of piling up new tabs) and wait for it to load.
    Warns with NotSignedIn when it lands on a sign-in page: then ask the user to sign in once with browser_launch(mode='visible'), or
    switch to browser_upload_own in their own signed-in browser."""
    return await core.browser_navigate(tab, url, timeout)


@tool(annotations=LOCAL_RO)
async def browser_text(
    tab: Tab,
    selector: Annotated[str | None, Field(description="Limit to this element; omit for the whole page")] = None,
    max_chars: Annotated[int, Field(ge=100, le=200000)] = 8000,
) -> dict[str, Any]:
    """Use to read what the headless browser shows (status messages, upload progress, errors, login walls), since you cannot
    see its window. Returns the visible text of the page or one element (selector), cut at max_chars."""
    return await core.browser_text(tab, selector, max_chars)


@tool(annotations=WRITE_LOCAL)
async def browser_screenshot(
    tab: Tab,
    path: Annotated[str | None, Field(description="Where to save the PNG (absolute or ~; must not exist). Default: ~/.filebridge/screenshots/")] = None,
    full_page: bool = False,
) -> dict[str, Any]:
    """Use when text alone is not enough to understand the headless page (layout, where the upload button is, an error
    dialog). Saves a PNG of the tab (full_page optional) and returns its path; read the image to look at it. path must not exist yet;
    default folder ~/.filebridge/screenshots/."""
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
    """Use to fill text fields in the automation or attached browser, e.g. a title or description an upload form needs.
    Types with real input events into a field picked by ref (from browser_snapshot) or selector; clear=true empties it first,
    press_enter=true submits."""
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
    """Use when a website needs a file uploaded and the user is signed in to that site in their OWN everyday browser
    (Google Drive, X, Typefully, a form...), especially when the browser extension's upload tool fails, is unavailable, or the file is
    over 10 MB. Also the answer to 'attach a local file to an <input type=file> from an agent' when you can run JavaScript in the tab.
    Starts a token-protected server on 127.0.0.1 in the background and returns {server_id, urls, snippet, expires_in_s, files}.
    Then run `snippet` in that tab with a tool that executes JavaScript there (e.g. Claude in Chrome's javascript tool): the page
    fetches each file from localhost and fills the input matching `selector`, or, with no selector, drops the files on the element at
    the page centre (how Drive/X/Typefully listen). Gotchas: the snippet uses top-level await, so pass it as is (do not wrap it in a
    function); its last expression is a JSON string {ok:true, action, files, bytes} or {ok:false, code, message}. Each URL works ONCE
    and the server stops when all files were fetched or after ttl seconds, so a retry needs a new browser_upload_own call (codes
    LinkUsed / FetchBlocked). FetchBlocked can also mean the site's CSP or the browser's local-network permission blocked localhost.
    Pass verify_text (e.g. the file name) or verify_selector to also get `verify_snippet`; run it after the upload snippet to wait for
    the site's confirmation ({verified, waited_s}). Files outside the user's allowed folders trigger an approval prompt (PathNotAllowed
    if declined); credential files are always refused (PathBlocked). Over 1 GiB returns a memory warning."""
    return await core.browser_upload_own(paths, selector, ttl, verify_text, verify_selector, verify_timeout)


@tool(annotations=LOCAL_RO)
async def serve_status(server_id: Annotated[str, Field(description="server_id from browser_upload_own")]) -> dict[str, Any]:
    """Use after running a browser_upload_own snippet to confirm the page actually fetched the files. Returns the server state
    (running / done / expired / stopped), served/total/failed counts and each file's state (pending / serving / served / failed).
    'done' with every file served means the browser has the bytes; the site's own upload may still be in progress (use verify_snippet)."""
    return await core.serve_status(server_id)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def serve_stop(server_id: Annotated[str, Field(description="server_id from browser_upload_own")]) -> dict[str, Any]:
    """Use to cancel a browser_upload_own session you no longer need (wrong file, user changed their mind). Stops its server
    right away; its links stop working. Returns the final status."""
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
    """Use when files must go to another place on this machine: an external drive, a mounted NAS share, or the iCloud Drive
    folder (~/Library/Mobile Documents/com~apple~CloudDocs/<folder>/), which is more reliable than rclone's iCloud backend. Copies with
    rsync -a --partial as a background job: returns {job_id}; poll job_status. A trailing '/' on a source folder copies its contents
    (rsync semantics). Never deletes; existing files are skipped unless overwrite=true. Source outside the allowed folders asks the
    user; credential files are refused (PathBlocked)."""
    return await core.local_copy(src, dest, overwrite, dry_run)


@tool(annotations=WRITE_LOCAL)
async def clipboard_files(ctx: Context, paths: Paths) -> dict[str, Any]:
    """Use when the user wants to send or attach files in a desktop app (Slack, Telegram Desktop, WhatsApp, Mail, Finder)
    where they will paste: puts the files on the clipboard as file references (replacing what was there), then the user (or you, with
    a keyboard tool) presses Cmd+V in that app. Same path rules as uploads. macOS tested; Linux (xclip/wl-copy) and Windows
    (Set-Clipboard) untested."""
    return await core.clipboard_files(paths)


@tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def reveal(path: Annotated[str, Field(description="Local file or folder to show in the file manager")]) -> dict[str, Any]:
    """Use when the user asks to see where a file is, or must drag it somewhere by hand. Shows the path in Finder (open -R) or the
    platform's file manager. This opens a window on the user's screen, so only call it when they asked."""
    return await core.reveal(path)


def main(slim: bool = False) -> None:
    if slim:
        for name in _names:
            if name not in SLIM_TOOLS:
                mcp.remove_tool(name)
    mcp.run("stdio")


if __name__ == "__main__":
    main()
