"""Which local files filebridge may read: allowed roots, a hard denylist, and approval for everything else.

Every tool that reads local files (set_files, drop_files, click(files), upload_own/serve, clipboard_files, local_copy,
transfer from a local source) calls check_sources(). It resolves symlinks, then:

- a path (requested or resolved) matching a blocked glob is refused with PathBlocked, always, approval or not;
- a resolved path inside an allowed root proceeds;
- anything else is put to the human through the current approver (MCP elicitation, or a y/N prompt in the CLI).
  No approver, or a "no", is PathNotAllowed.
"""

import contextvars
import os
import re
import time
import tomllib
from collections.abc import Awaitable, Callable
from pathlib import Path

from .common import STATE_DIR, FBError, human_bytes, local_path

CONFIG_PATH = STATE_DIR / "config.toml"

DEFAULT_ROOTS = ["~/Projects", "~/Movies", "~/Downloads", "~/Documents", "<cwd>"]

# Always enforced, even if removed from the config file. The config's blocked_globs add to this list.
BUILTIN_BLOCKED = [
    "**/.ssh/**", "**/.gnupg/**", "**/*.pem", "**/*.key", "**/id_rsa*", "**/.env", "**/.env.*", "**/.aws/**",
    "**/.config/gcloud/**", "**/Library/Keychains/**", "**/.netrc", "**/.npmrc", "**/.pypirc", "**/*secrets*.json",
    "**/Cookies", "**/Login Data", "**/.config/rclone/**",
]

_SCAN_MAX_ENTRIES = 200_000
_SCAN_MAX_SECONDS = 15.0

# Returns True (allow), False (deny) or None (this front end cannot ask a human).
Approver = Callable[[str], Awaitable[bool | None]]
approver: contextvars.ContextVar[Approver | None] = contextvars.ContextVar("filebridge_approver", default=None)

_run_roots: list[str] = []  # CLI --allow-path, for this process only


def add_run_root(path: str) -> None:
    _run_roots.append(str(local_path(path, must_exist=True)))


def _toml_list(items: list[str]) -> str:
    return "[\n" + "".join(f'    "{i}",\n' for i in items) + "]"


def default_config_text() -> str:
    return f"""# filebridge config. Created on first run; edit freely (it is re-read on every call).

# Local files filebridge may read (upload, serve, copy, put on the clipboard) without asking you.
# "<cwd>" is the folder the MCP client (or the CLI) was started in. It is ignored when that folder is / or your
# home folder or above it, so a client started from / does not open up the whole disk.
# Anything outside these folders is put to you first (MCP elicitation, or a y/N prompt in the CLI).
allowed_roots = {_toml_list(DEFAULT_ROOTS)}

# Never readable, not even with your approval. The built-in patterns below always apply, even if you delete them
# here; add your own to block more. "**/" matches any folder depth, "*" anything except "/". Case-insensitive.
blocked_globs = {_toml_list(BUILTIN_BLOCKED)}

# Largest single file filebridge will hand over, in bytes. 0 = no limit.
max_bytes = 0
"""


def load_config() -> dict:
    """Read ~/.filebridge/config.toml, creating it with the defaults on first run."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        CONFIG_PATH.write_text(default_config_text())
    try:
        cfg = tomllib.loads(CONFIG_PATH.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise FBError(f"{CONFIG_PATH} is not valid TOML: {e}. Fix it or delete it to get the defaults back.", "ConfigError")
    roots, globs, max_bytes = cfg.get("allowed_roots", DEFAULT_ROOTS), cfg.get("blocked_globs", []), cfg.get("max_bytes", 0)
    if not (isinstance(roots, list) and all(isinstance(r, str) for r in roots)
            and isinstance(globs, list) and all(isinstance(g, str) for g in globs)
            and isinstance(max_bytes, int) and max_bytes >= 0):
        raise FBError(f"{CONFIG_PATH}: allowed_roots and blocked_globs must be lists of strings, max_bytes a whole number >= 0.", "ConfigError")
    return {"allowed_roots": roots, "blocked_globs": globs, "max_bytes": max_bytes}


def _glob_re(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE | re.DOTALL)


def blocked_patterns(cfg: dict) -> list[tuple[str, re.Pattern]]:
    return [(g, _glob_re(g)) for g in dict.fromkeys(BUILTIN_BLOCKED + cfg["blocked_globs"])]


def match_blocked(path: str, patterns: list[tuple[str, re.Pattern]]) -> str | None:
    """The first blocked glob matching path (or path as a folder), else None."""
    p = Path(path).as_posix()
    for g, rx in patterns:
        if rx.match(p) or rx.match(p.rstrip("/") + "/"):
            return g
    return None


def _cwd_root() -> str | None:
    cwd = Path(os.path.realpath(os.getcwd()))
    home = Path(os.path.realpath(Path.home()))
    if cwd == Path(cwd.anchor) or cwd == home or cwd in home.parents:
        return None
    return str(cwd)


def allowed_roots(cfg: dict) -> list[Path]:
    roots = []
    for r in cfg["allowed_roots"] + _run_roots:
        r = _cwd_root() if r == "<cwd>" else os.path.expanduser(r)
        if r and os.path.isabs(r):
            roots.append(Path(os.path.realpath(r)))
    return list(dict.fromkeys(roots))


def _inside(p: Path, roots: list[Path]) -> bool:
    return any(p == r or r in p.parents for r in roots)


def _scan_tree(root: Path, patterns, max_bytes: int) -> int:
    """Refuse a folder that contains blocked files or a file over max_bytes. Returns the folder's total bytes.

    Fails closed: a tree too big to scan in the budget is refused rather than shipped unchecked. Symlinks inside
    are not followed (rsync -a copies them as links, rclone skips them), so they cannot leak their targets."""
    total, n, hits = 0, 0, []
    deadline = time.time() + _SCAN_MAX_SECONDS
    for dirpath, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            full = os.path.join(dirpath, name)
            n += 1
            g = match_blocked(full, patterns)
            if g:
                hits.append(f"{full} ({g})")
            elif name in files:
                try:
                    size = os.lstat(full).st_size
                except OSError:
                    continue
                if max_bytes and size > max_bytes:
                    raise FBError(f"{full} is {human_bytes(size)}, over max_bytes ({human_bytes(max_bytes)}) in {CONFIG_PATH}.", "FileTooLarge", path=full)
                total += size
        dirs[:] = [d for d in dirs if not match_blocked(os.path.join(dirpath, d), patterns)]
        if n > _SCAN_MAX_ENTRIES or time.time() > deadline:
            raise FBError(f"{root} is too big to check for blocked files ({n}+ entries). Pass smaller subfolders.", "PathBlocked", path=str(root))
    if hits:
        shown = "; ".join(hits[:10]) + (f"; and {len(hits) - 10} more" if len(hits) > 10 else "")
        raise FBError(f"{root} contains files that can never be shared: {shown}. Move them out or pick the other files/subfolders.",
                      "PathBlocked", path=str(root), blocked=hits[:50])
    return total


async def check_sources(paths: list[str], *, action: str, destination: str, files_only: bool = False) -> list[str]:
    """Validate local sources a tool is about to read. Returns their resolved real paths, in order.

    Callers must hand the RETURNED paths to the reader (browser, rsync, rclone, HTTP server), never the requested
    ones, so a symlink swapped after the check cannot redirect the read."""
    if not paths:
        raise FBError("Give at least one path.", "InvalidArgument")
    cfg = load_config()
    patterns = blocked_patterns(cfg)
    max_bytes = cfg["max_bytes"]
    out: list[tuple[str, int | None]] = []
    for p in paths:
        trailing = p.strip().endswith("/")
        lp = local_path(p, must_exist=True)
        real = Path(os.path.realpath(lp))
        for cand in (str(lp), str(real)):
            g = match_blocked(cand, patterns)
            if g:
                raise FBError(f"{p} can never be shared (matches blocked pattern {g!r}{', via a symlink to ' + str(real) if cand == str(real) and real != lp else ''}).",
                              "PathBlocked", path=str(lp), pattern=g)
        if real.is_file():
            size = real.stat().st_size
            if max_bytes and size > max_bytes:
                raise FBError(f"{lp} is {human_bytes(size)}, over max_bytes ({human_bytes(max_bytes)}) in {CONFIG_PATH}.", "FileTooLarge", path=str(lp))
        elif real.is_dir():
            if files_only:
                raise FBError(f"{lp} is a folder; list the files inside it instead.", "InvalidArgument", path=str(lp))
            size = _scan_tree(real, patterns, max_bytes)
        else:
            raise FBError(f"{lp} is not a regular file or folder.", "InvalidArgument", path=str(lp))
        out.append((str(real) + ("/" if trailing and real.is_dir() else ""), size))

    if os.environ.get("FILEBRIDGE_UNRESTRICTED") != "1":
        roots = allowed_roots(cfg)
        outside = [(r, s) for r, s in out if not _inside(Path(r.rstrip("/") or "/"), roots)]
        if outside:
            await _approve(outside, action, destination, roots)
    return [r for r, _ in out]


async def _approve(outside: list[tuple[str, int | None]], action: str, destination: str, roots: list[Path]) -> None:
    if len(outside) == 1:
        r, s = outside[0]
        question = f"filebridge wants to {action} {r} ({human_bytes(s)}) to {destination}. Allow?"
    else:
        listing = ", ".join(f"{r} ({human_bytes(s)})" for r, s in outside[:20]) + (f" and {len(outside) - 20} more" if len(outside) > 20 else "")
        question = f"filebridge wants to {action} {len(outside)} items outside your allowed folders to {destination}: {listing}. Allow?"
    names = ", ".join(r for r, _ in outside[:5])
    ask = approver.get()
    answer = await ask(question) if ask else None
    if answer is True:
        return
    roots_s = ", ".join(map(str, roots)) or "none"
    if answer is None:
        raise FBError(f"{names} is outside the allowed folders ({roots_s}) and there is no way to ask the user for approval here. "
                      f"Add the folder to allowed_roots in {CONFIG_PATH} (CLI: --allow-path DIR for one run).",
                      "PathNotAllowed", paths=[r for r, _ in outside], config_key="allowed_roots", config_file=str(CONFIG_PATH))
    raise FBError(f"The user declined: {names} (outside the allowed folders {roots_s}). Do not retry the same path; ask the user.",
                  "PathNotAllowed", paths=[r for r, _ in outside])
