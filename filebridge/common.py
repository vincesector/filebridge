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

# Upload sources that hold credentials. A prompt-injected agent should not be able to
# ship these to a cloud remote. Set FILEBRIDGE_ALLOW_SENSITIVE=1 to override.
_SENSITIVE = [Path.home() / p for p in (".ssh", ".gnupg", ".aws", ".config/rclone", "Library/Keychains")]


class FBError(ToolError):
    """An anticipated failure; the message is shown to the agent verbatim."""


def find_tool(name: str, extra: tuple[str, ...] = ()) -> str:
    """Absolute path of an executable. MCP clients often launch us with a thin PATH."""
    found = shutil.which(name)
    if found:
        return found
    for cand in (*extra, f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"):
        if os.access(cand, os.X_OK):
            return cand
    raise FBError(f"'{name}' not found. Install it (macOS: `brew install {name}`) and retry.")


def ensure_state_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)


def local_path(p: str, *, must_exist: bool = False, source: bool = False) -> Path:
    """Expand ~, require absolute, normalise. No shell is ever involved."""
    if not p or "\x00" in p:
        raise FBError("Empty or invalid path.")
    s = os.path.expanduser(p.strip())
    if not os.path.isabs(s):
        raise FBError(f"Path must be absolute or start with ~ (got {p!r}). Relative paths are ambiguous for a long-lived server.")
    path = Path(os.path.normpath(s))
    if must_exist and not path.exists():
        raise FBError(f"Local path does not exist: {path}")
    if source and os.environ.get("FILEBRIDGE_ALLOW_SENSITIVE") != "1":
        resolved = path.resolve()
        for bad in _SENSITIVE:
            if resolved == bad or bad in resolved.parents:
                raise FBError(f"Refusing to use {path} as a source: it holds credentials. Set FILEBRIDGE_ALLOW_SENSITIVE=1 in the server env to override.")
    return path


_REMOTE_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_. -]*):(.*)$", re.S)


def split_location(p: str) -> tuple[str | None, str]:
    """Return (remote_name, path) for 'remote:path', or (None, p) for a local path.

    Connection strings like ':s3,access_key_id=...:bucket' are rejected on purpose.
    """
    s = p.strip()
    if not s or "\x00" in s:
        raise FBError("Empty or invalid path.")
    if s.startswith(("~", "/")):
        return None, s
    if s.startswith(":"):
        raise FBError("Inline rclone connection strings are not allowed. Configure a named remote (see setup_instructions).")
    m = _REMOTE_RE.match(s)
    if m:
        return m.group(1), m.group(2)
    raise FBError(f"Cannot tell whether {p!r} is local or remote. Use an absolute/~ path or 'remote:path'.")


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
        raise FBError(f"Timed out after {timeout:.0f}s: {Path(argv[0]).name} {argv[1] if len(argv) > 1 else ''}")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
