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
2. **Headless CDP browser** (`browser.py`, `cdp.py`). A dedicated automation browser profile
   (`~/.filebridge/browser-profile`, DevTools on `127.0.0.1:9229`). `DOM.setFileInputFiles` makes the browser read the
   file from disk itself, so there is no size cap. Drag-and-drop uses `Input.dispatchDragEvent` with disk paths.
3. **`serve`** (`servefiles.py`). For sites that need a login the automation profile does not have: a token-protected
   HTTP server on `127.0.0.1` hands the files to the user's own signed-in browser through a printed JS snippet.
4. **Local helpers** (`local.py`). `rsync` copy as a background job, macOS pasteboard file references, Finder reveal.

## Decisions

- **Dedicated browser profile, never the daily one.** Chrome 136+ ignores `--remote-debugging-port` on the default
  profile, and attaching an agent to a daily profile is a security risk. The human signs in once with a visible window;
  the session persists and agents reuse it headless.
- **Headless by default.** Agents never open windows on the user's screen. `browser_text` and `browser_screenshot`
  are how the page is observed.
- **No delete tool.** `move` removes the source only after rclone verified the copy. `sync` can delete on the
  destination, so it requires a prior `dry_run` or an explicit `confirm`.
- **Never overwrite by default.** Copies skip files that already exist unless `overwrite=true`.
- **No shell.** Every subprocess is an argument list; no `shell=True`.
- **Secrets stay in rclone's own config.** Remotes are configured by the human (`setup_instructions` prints the exact
  command); the server never configures real accounts or prints tokens.
- **Sensitive local sources are refused** (`~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/rclone`, `~/Library/Keychains`).
- **Drop falls back honestly.** If trusted CDP drag events deliver no files, an in-page `DataTransfer` is used and the
  result reports that the events were synthetic (`isTrusted=false`).

## Tests

The scripts in `tests/` are end-to-end checks, not unit tests. Offline ones: `test_local.py`, `test3_clipboard.py`,
`test4_mcp_listing.py`. Browser ones (`test2_browser.py`, `test5_cli_headless.py`, `test6_browser_extras.py`) need
Brave or Chrome; `test1_transfer.py` needs rclone (it pushes 2 GiB through a local alias remote).
