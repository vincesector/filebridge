# filebridge design notes

## Problem

Browser automation cannot move real files:

- Browser-extension upload tools cap files at about 10 MB and only accept shared paths.
- Native OS file pickers cannot be driven by an agent.
- Dragging from Finder into a web page is impossible for an agent.
- A headless automation browser has no logged-in sessions.

filebridge is one local MCP server (stdio) plus a CLI that sidesteps the browser wherever an API exists and uses the
Chrome DevTools Protocol (CDP) with real disk paths where one does not. MCP tools and CLI commands call the same
functions in `filebridge/core.py`.

## Four paths

1. **rclone** (`rclone.py`, `jobs.py`). Anything with an rclone backend. Transfers run as background jobs that return a
   `job_id` immediately, so multi-GB copies never block or time out the agent. Logs live in `~/.filebridge/jobs/`.
2. **CDP browser** (`browser.py`, `cdp.py`). A dedicated automation browser profile
   (`~/.filebridge/browser-profile`, DevTools on `127.0.0.1:9229`), or the user's own Chrome/Brave in attach mode.
   `DOM.setFileInputFiles` makes the browser read the file from disk itself, so there is no size cap. Drag-and-drop uses
   `Input.dispatchDragEvent` with disk paths. Every call opens one browser-level websocket and reaches tabs and
   out-of-process iframes through flattened sessions, which is what makes the websocket-only chrome://inspect toggle
   usable too.
3. **`browser_upload_own` / `serve`** (`servefiles.py`). For sites the user is signed in to in their own browser: a
   token-protected HTTP server on `127.0.0.1`, running in background threads of the MCP process, hands each file out
   exactly once to a JS snippet the agent runs in the tab.
4. **Local helpers** (`local.py`). `rsync` copy as a background job, clipboard file references, reveal in the file
   manager.

Local reads in all four go through `policy.py` first.

## Decisions

- **Dedicated browser profile by default.** Chrome 136+ ignores `--remote-debugging-port` on the default profile, and
  attaching an agent to a daily profile is a security risk. The human signs in once with a visible window; the session
  persists and agents reuse it headless. Attach mode is opt-in, loopback-only, and never launches or closes the
  user's browser.
- **Headless by default.** Agents never open windows on the user's screen. `browser_text` and `browser_screenshot`
  are how the page is observed.
- **No delete tool.** `move` removes the source only after rclone verified the copy. `sync` can delete on the
  destination, so it requires a prior `dry_run` or an explicit `confirm`.
- **Never overwrite by default.** Copies skip files that already exist unless `overwrite=true`.
- **No shell.** Every subprocess is an argument list; no `shell=True`.
- **Secrets stay in rclone's own config.** Remotes are configured by the human (`setup_instructions` prints the exact
  command); the server never configures real accounts or prints tokens.
- **Local reads are policed in one place** (`policy.check_sources`). Real paths (symlinks resolved) are checked against
  allowed roots from `~/.filebridge/config.toml`; a built-in denylist of credential files can never be lifted, not by
  approval and not by `FILEBRIDGE_UNRESTRICTED`; folders are scanned for blocked files, failing closed if the scan
  runs out of budget. Readers get the resolved path, so a symlink swapped after the check changes nothing.
- **Approval goes through the front end.** A context variable holds an approver: the MCP server sets one per call that
  uses elicitation (a boolean `allow` form), the CLI sets a TTY `y/N` prompt. No approver means refuse. The plan asked
  for `ctx.elicit`, which is a mid-call server-to-client request; it works on the handshake protocol versions current
  clients speak over stdio (checked with SDK 2.3 at protocol 2025-11-25). Clients on the 2026-07-28 stateless flow
  would need the SDK's `Resolve(...)` multi-round-trip instead; today they get `PathNotAllowed` with the config hint.
- **Errors carry codes.** `FBError(message, code, **extra)`; MCP tools return `{ok: false, code, message, ...}` with
  `isError`, soft failures (no matching input, a wait that timed out) return the same shape without it.
- **One-time links.** Each served URL can be claimed once (`410 Gone` afterwards); the server stops and closes its
  socket once every file was claimed and finished, or at the TTL.
- **Refs, not guesses.** `browser_snapshot` maps refs to backend node ids and marks the page with a random id; a ref
  used after a navigation or reload is reported stale instead of hitting whatever now has that node id.
- **Drop falls back honestly.** If trusted CDP drag events deliver no files, an in-page `DataTransfer` is used and the
  result reports that the events were synthetic (`isTrusted=false`).

## Tests

The scripts in `tests/` are end-to-end checks, not unit tests. Offline ones: `test7_policy_offline.py` (runs servers
with `HOME` in a temp folder and an elicitation stub), `test_local.py`, `test3_clipboard.py`, `test4_mcp_listing.py`.
Browser ones (`test8_browser_v02.py`, `test2_browser.py`, `test5_cli_headless.py`, `test6_browser_extras.py`) need
Brave or Chrome and stay headless; `test1_transfer.py` needs rclone (it pushes 2 GiB through a local alias remote).
Work files live in `tests/_work/`, inside the default allowed root `~/Projects`. Live checks against real sites:
`docs/LIVE_TESTS.md`.
