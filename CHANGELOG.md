# Changelog

## 0.2.0 (2026-10-20)

Easier for the agent, safer by default, works in more places.

### Added

- **`browser_upload_own`** MCP tool: serves files to the user's own signed-in browser in the background and returns
  `{server_id, urls, snippet, expires_in_s}`; `serve_status` and `serve_stop` go with it. The CLI `serve` keeps
  working. The snippet stays in top-level-await form and now returns a JSON string with `{ok, code, message}`
  (`FetchBlocked`, `LinkUsed`, `SelectorNotFound`). Optional `verify_text` / `verify_selector` add a `verify_snippet`.
- **Path policy** with `~/.filebridge/config.toml` (created on first run): `allowed_roots` (default `~/Projects`,
  `~/Movies`, `~/Downloads`, `~/Documents` and the client's working folder), a hard `blocked_globs` denylist for
  credentials, and `max_bytes`. Paths outside the roots are put to the user through MCP elicitation (CLI: `y/N` on a
  TTY). `--allow-path DIR` and `FILEBRIDGE_UNRESTRICTED=1` (allowlist only) for the CLI and server.
- **Attach mode**: `browser_launch(mode="attach", cdp_url=...)` / `filebridge browser attach` drives a Chrome/Brave
  the user started with `--remote-debugging-port`, or via a `ws://` endpoint (chrome://inspect toggle). Loopback only;
  never launches or closes the browser.
- **`browser_snapshot`**: interactive elements, file inputs and drop zones with refs (`e12`) that `browser_set_files`,
  `browser_drop_files`, `browser_click` and `browser_type` accept instead of a selector.
- **Verification**: `verify_text` / `verify_selector` / `verify_timeout` on set_files and drop return `verified`.
- **`filebridge mcp --slim`**: only the upload tools, to save agent context.
- **Structured errors** everywhere: `{ok: false, code, message}` with `PathBlocked`, `PathNotAllowed`,
  `FileNotFound`, `SelectorNotFound`, `TabNotFound`, `FileTooLarge`, `FetchBlocked`, `Timeout`, `NotSignedIn`,
  `RemoteNotConfigured` and a few generic ones. `NotSignedIn` is reported when an upload target is missing on what
  looks like a sign-in page.
- Linux and Windows, best effort and untested: browser discovery on the PATH and in standard install folders,
  `xclip` / `wl-copy` and PowerShell `Set-Clipboard -Path`, `xdg-open` and `explorer /select,`.
- A warning when a file served to the user's browser is over 1 GiB (the tab holds it in memory).
- One-line install with `uvx --from git+https://github.com/vincesector/filebridge filebridge mcp`.

### Changed

- Served URLs work **once** (second request: `410 Gone`); the server closes its socket when it stops, so late
  requests fail fast instead of hanging. URLs now carry a file index, so two files with the same name can be served.
- `browser_launch(visible=...)` became `mode="headless" | "visible" | "attach"` (CLI `--visible` unchanged).
- `browser_click` and `browser_type` take `selector` or `ref`.
- Soft failures use `message` instead of `reason`; `browser_wait` and `browser_navigate` timeouts carry `code: Timeout`.
- Readers (browser, rsync, rclone, the HTTP server) receive the resolved real path, so a symlink is followed once,
  at check time.
- The browser layer uses one browser-level DevTools connection with flattened sessions instead of per-tab sockets.

### Removed

- `FILEBRIDGE_ALLOW_SENSITIVE`. The denylist is a hard floor now; edit `allowed_roots`, not the denylist.
