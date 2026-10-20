"""filebridge MCP server (stdio). Move files of any size anywhere.

Lane 1: rclone (cloud storage, background jobs)      Lane 2: CDP browser uploads with real local paths
Lane 3: local helpers (rsync, pasteboard, reveal)
"""

from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from . import core

mcp = MCPServer(
    "filebridge",
    instructions=(
        "Decision tree: does the destination have an API/rclone backend (S3/R2, SFTP, Dropbox, OneDrive...)? "
        "Use transfer(). No API? Use browser_launch + browser_set_files / browser_drop_files / browser_click(files=). "
        "Local-only moves: local_copy, clipboard_files, reveal. Long jobs return a job_id; poll job_status. Nothing here deletes files."
    ),
)

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)
LOCAL_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
WRITE_LOCAL = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

Tab = Annotated[str, Field(description="Tab id from browser_tabs(), or a unique part of its url/title (e.g. 'example.com/upload')")]
Paths = Annotated[list[str], Field(description="Absolute or ~ local file paths", min_length=1, max_length=200)]


# ------------------------------------------------------------------ Lane 1: rclone

@mcp.tool(annotations=LOCAL_RO)
async def remotes() -> dict[str, Any]:
    """List configured rclone remotes and their backend types (never prints tokens).
    If the one you need is missing, call setup_instructions(backend)."""
    return await core.remotes()


@mcp.tool(annotations=WRITE)
async def transfer(
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


@mcp.tool(annotations=LOCAL_RO)
async def job_status(job_id: Annotated[str, Field(description="Id returned by transfer or local_copy")]) -> dict[str, Any]:
    """Progress of a background job: state (running/done/error/cancelled), percent, bytes, speed, ETA, last error lines, log path."""
    return await core.job_status(job_id)


@mcp.tool(name="jobs", annotations=LOCAL_RO)
async def jobs_list(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
    """List recent background jobs, newest first."""
    return await core.jobs(limit)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def cancel(job_id: str) -> dict[str, Any]:
    """Stop a running job (sends SIGTERM to its runner; partial uploads are not deleted)."""
    return await core.cancel(job_id)


@mcp.tool(annotations=RO)
async def ls(
    remote_path: Annotated[str, Field(description="'remote:path' (a local path also works)")],
    recursive: bool = False,
    max: Annotated[int, Field(ge=1, le=5000, description="Max entries returned")] = 200,
) -> dict[str, Any]:
    """List names, sizes and modification times at a remote (or local) path."""
    return await core.ls(remote_path, recursive, max)


@mcp.tool(annotations=RO)
async def link(remote_path: Annotated[str, Field(description="'remote:path' of a file or folder")]) -> dict[str, Any]:
    """Create/get a shareable link for a remote file or folder (backend must support it: Dropbox, OneDrive, S3-compatible...)."""
    return await core.link(remote_path)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
async def mkdir(remote_path: Annotated[str, Field(description="'remote:path' of the folder to create")]) -> dict[str, Any]:
    """Create a folder (and parents) on a remote. Succeeds quietly if it already exists."""
    return await core.mkdir(remote_path)


@mcp.tool(annotations=RO)
async def stat(remote_path: Annotated[str, Field(description="'remote:path'")]) -> dict[str, Any]:
    """Check whether a remote file or folder exists and get its size/type/modified time."""
    return await core.stat(remote_path)


@mcp.tool(annotations=LOCAL_RO)
async def setup_instructions(
    backend: Annotated[str | None, Field(description="dropbox, onedrive, icloud, s3, r2, sftp Omit to list.")] = None,
) -> dict[str, Any]:
    """Exact commands a human runs to add a remote (OAuth/credentials are the human's job; this server never configures accounts)."""
    return await core.setup_instructions(backend)


# ------------------------------------------------------------------ Lane 2: browser (CDP)

@mcp.tool(annotations=WRITE)
async def browser_launch(
    url: Annotated[str | None, Field(description="Optional page to open (new tab if already running)")] = None,
    browser: Annotated[Literal["auto", "brave", "chrome"], Field(description="auto = Brave, falling back to Chrome")] = "auto",
    visible: Annotated[bool, Field(description="false (default) = HEADLESS, nothing appears on screen. true ONLY for the one-time human sign-in to a site; switching modes restarts the browser on the same profile.")] = False,
) -> dict[str, Any]:
    """Start (or reuse) the DEDICATED automation browser, HEADLESS by default: profile ~/.filebridge/browser-profile, DevTools on 127.0.0.1:9229.
    Never the daily browser profile. Sessions saved there (after a one-time visible=true sign-in by the human) work headless afterwards."""
    return await core.browser_launch(url, browser, visible)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def browser_close() -> dict[str, Any]:
    """Close the dedicated automation browser (only that instance; the profile and logins are kept)."""
    return await core.browser_close()


@mcp.tool(annotations=LOCAL_RO)
async def browser_tabs() -> dict[str, Any]:
    """List open tabs in the automation browser (id, title, url)."""
    return await core.browser_tabs()


@mcp.tool(annotations=WRITE)
async def browser_set_files(
    tab: Tab,
    paths: Paths,
    selector: Annotated[str | None, Field(description="CSS selector of an <input type=file>, hidden ones are fine. Omit to auto-pick when the page has exactly one.")] = None,
) -> dict[str, Any]:
    """Attach local files to a file input via CDP DOM.setFileInputFiles. The browser reads the files from disk: no 10 MB cap, any size.
    Searches shadow roots and iframes. If nothing matches, returns the candidate inputs it found. Verifies names and sizes in the page."""
    return await core.browser_set_files(tab, paths, selector)


@mcp.tool(annotations=WRITE)
async def browser_drop_files(
    tab: Tab,
    paths: Paths,
    selector: Annotated[str | None, Field(description="Drop zone selector; the drop lands at its centre")] = None,
    x: Annotated[float | None, Field(description="Viewport x, used when no selector")] = None,
    y: Annotated[float | None, Field(description="Viewport y, used when no selector")] = None,
    method: Annotated[Literal["auto", "cdp", "synthetic"], Field(description="auto = real CDP drag events, falling back to in-page DataTransfer if the page saw no files")] = "auto",
) -> dict[str, Any]:
    """Drag and drop local files onto a page's drop zone with real CDP drag events (Input.dispatchDragEvent) carrying disk paths.
    Reports which method worked and what the page received."""
    return await core.browser_drop_files(tab, paths, selector, x, y, method)


@mcp.tool(annotations=WRITE)
async def browser_click(
    tab: Tab,
    selector: Annotated[str, Field(description="CSS selector of what to click (searches shadow roots and same-origin iframes)")],
    files: Annotated[list[str] | None, Field(description="If the click opens a native file chooser (e.g. an 'Upload file' menu item), fill it with these paths")] = None,
) -> dict[str, Any]:
    """Real mouse click at the element's centre. File choosers are always intercepted (no native dialog ever appears);
    pass files=[...] to fill them. Refuses to click an <input type=file> directly (use browser_set_files)."""
    return await core.browser_click(tab, selector, files)


@mcp.tool(annotations=LOCAL_RO)
async def browser_wait(
    tab: Tab,
    selector_or_text: Annotated[str, Field(description="CSS selector OR visible text, e.g. 'Upload complete'")],
    timeout: Annotated[float, Field(ge=1, le=3600, description="Seconds to wait")] = 60,
) -> dict[str, Any]:
    """Wait until a CSS selector matches or the text appears anywhere on the page (shadow roots and iframes included)."""
    return await core.browser_wait(tab, selector_or_text, timeout)


@mcp.tool(annotations=WRITE)
async def browser_navigate(tab: Tab, url: Annotated[str, Field(description="http(s):// or file:// URL")], timeout: Annotated[float, Field(ge=1, le=300)] = 30) -> dict[str, Any]:
    """Navigate an existing tab to a URL and wait for it to load (avoids piling up new tabs)."""
    return await core.browser_navigate(tab, url, timeout)


@mcp.tool(annotations=LOCAL_RO)
async def browser_text(
    tab: Tab,
    selector: Annotated[str | None, Field(description="Limit to this element; omit for the whole page")] = None,
    max_chars: Annotated[int, Field(ge=100, le=200000)] = 8000,
) -> dict[str, Any]:
    """Read the visible text of the page (or one element). The browser is headless, so this is how you see status messages, errors and login walls."""
    return await core.browser_text(tab, selector, max_chars)


@mcp.tool(annotations=WRITE_LOCAL)
async def browser_screenshot(
    tab: Tab,
    path: Annotated[str | None, Field(description="Where to save the PNG (absolute or ~; must not exist). Default: ~/.filebridge/screenshots/")] = None,
    full_page: bool = False,
) -> dict[str, Any]:
    """Save a PNG screenshot of the tab and return its path, so you can look at what the headless browser sees."""
    return await core.browser_screenshot(tab, path, full_page)


@mcp.tool(annotations=WRITE)
async def browser_type(
    tab: Tab,
    selector: Annotated[str, Field(description="CSS selector of the field to type into")],
    text: str,
    clear: Annotated[bool, Field(description="Select and delete the existing value first")] = False,
    press_enter: Annotated[bool, Field(description="Press Enter afterwards")] = False,
) -> dict[str, Any]:
    """Type text into a field with real input events (for titles/descriptions that must be filled before an upload)."""
    return await core.browser_type(tab, selector, text, clear, press_enter)


# ------------------------------------------------------------------ Lane 3: local helpers

@mcp.tool(annotations=WRITE_LOCAL)
async def local_copy(
    src: Annotated[str, Field(description="Local file or folder. A trailing '/' on a folder means 'its contents' (rsync semantics)")],
    dest: Annotated[str, Field(description="Local destination: external drive, NAS mount, ~/Library/Mobile Documents/com~apple~CloudDocs/<folder>/ for iCloud Drive")],
    overwrite: Annotated[bool, Field(description="Replace files that exist at the destination (default skips them)")] = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """rsync -a --partial copy between local locations, as a background job (returns job_id; poll job_status). Never deletes."""
    return await core.local_copy(src, dest, overwrite, dry_run)


@mcp.tool(annotations=WRITE_LOCAL)
async def clipboard_files(paths: Paths) -> dict[str, Any]:
    """Put files on the macOS pasteboard as file references (replaces the clipboard). Cmd+V then pastes them into Finder, Slack, Telegram Desktop, WhatsApp, Mail..."""
    return await core.clipboard_files(paths)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
async def reveal(path: Annotated[str, Field(description="Local file or folder to show in Finder")]) -> dict[str, Any]:
    """Reveal a path in Finder (open -R). This opens a Finder window on the user's screen."""
    return await core.reveal(path)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
