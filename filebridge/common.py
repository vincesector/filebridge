"""Shared helpers: state dirs, errors, path validation, subprocess capture."""

import asyncio
import os
import re
import shutil
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

STATE_DIR = Path.home() / ".filebridge"
JOBS_DIR = STATE_DIR / "jobs"
JOBS_LOG = STATE_DIR / "jobs.jsonl"
BROWSER_PROFILE = STATE_DIR / "browser-profile"
CDP_PORT = 9229



class FBError(ToolError):
    """An anticipated failure. Agents get {ok: false, code, message, ...extra}; the CLI prints the message.

    Codes: PathBlocked, PathNotAllowed, FileNotFound, SelectorNotFound, TabNotFound, FileTooLarge, FetchBlocked,
    Timeout, NotSignedIn, RemoteNotConfigured, BrowserNotRunning, NotInstalled, InvalidArgument, Failed.
    """

    def __init__(self, message: str, code: str = "Failed", **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra

    def to_dict(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message, **self.extra}


def human_bytes(n: float | None) -> str | None:
    if n is None:
        return None
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return None


def find_tool(name: str, extra: tuple[str, ...] = ()) -> str:
    """Absolute path of an executable. MCP clients often launch us with a thin PATH."""
    found = shutil.which(name)
    if found:
        return found
    for cand in (*extra, f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"):
        if os.access(cand, os.X_OK):
            return cand
    raise FBError(f"'{name}' not found. Install it (macOS: `brew install {name}`) and retry.", "NotInstalled")


def ensure_state_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


def local_path(p: str, *, must_exist: bool = False) -> Path:
    """Expand ~, require absolute, normalise. No shell is ever involved.

    Anything that READS a local file must go through policy.check_sources() instead, which also resolves symlinks
    and applies the allowlist/denylist."""
    if not p or "\x00" in p:
        raise FBError("Empty or invalid path.", "InvalidArgument")
    s = os.path.expanduser(p.strip())
    if not os.path.isabs(s):
        raise FBError(f"Path must be absolute or start with ~ (got {p!r}). Relative paths are ambiguous for a long-lived server.", "InvalidArgument")
    path = Path(os.path.normpath(s))
    if must_exist and not path.exists():
        raise FBError(f"Local path does not exist: {path}", "FileNotFound")
    return path


_REMOTE_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_. -]*):(.*)$", re.S)


def split_location(p: str) -> tuple[str | None, str]:
    """Return (remote_name, path) for 'remote:path', or (None, p) for a local path.

    Connection strings like ':s3,access_key_id=...:bucket' are rejected on purpose.
    """
    s = p.strip()
    if not s or "\x00" in s:
        raise FBError("Empty or invalid path.", "InvalidArgument")
    if s.startswith(("~", "/")) or (os.name == "nt" and re.match(r"^[A-Za-z]:[\\/]", s)):
        return None, s
    if s.startswith(":"):
        raise FBError("Inline rclone connection strings are not allowed. Configure a named remote (see setup_instructions).", "InvalidArgument")
    m = _REMOTE_RE.match(s)
    if m:
        return m.group(1), m.group(2)
    raise FBError(f"Cannot tell whether {p!r} is local or remote. Use an absolute/~ path or 'remote:path'.", "InvalidArgument")


async def run_capture(argv: list[str], *, timeout: float = 60, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run an argument list (never a shell string) and capture output."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise FBError(f"Timed out after {timeout:.0f}s: {Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''}", "Timeout")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
