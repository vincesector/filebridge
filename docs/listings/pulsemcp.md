# PulseMCP

Status: not submitted.

## How servers get added

pulsemcp.com/submit currently says: "We are not accepting new MCP server or client submissions right now" while the directory is restructured. It says that publishing to the **Official MCP Registry** is the best first step, and PulseMCP will pick the entry up automatically. So the real path is the Official MCP Registry, with the name `io.github.vincesector/filebridge`.

Prerequisites, in order:

1. PyPI release of `filebridge-mcp` (the registry verifies PyPI ownership).
2. A line `mcp-name: io.github.vincesector/filebridge` in the package README (done, as an HTML comment at the top), which is the PyPI long description. For PyPI packages the registry checks this line instead of an `mcpName` field.
3. The repo-root `server.json` (done), then `mcp-publisher login github` and `mcp-publisher publish`. Authenticating as the `vincesector` GitHub account is what authorises the `io.github.vincesector/*` namespace.
4. Repo public. Homepage optional but should be live before submission.

`server.json` is ready at the repo root (validated against the 2025-12-11 schema, `registryType: pypi`, `identifier: filebridge-mcp`, `runtimeHint: uvx`, package argument `mcp`). The README already carries the `mcp-name` comment.

If the submit form reopens, it will most likely want the repository URL, name, a short description and a contact email; fill them from the table below.

## Fields

| Field | Value |
|---|---|
| Name | filebridge |
| Short description (<=100 chars) | Upload any-size files to your signed-in browser, rclone clouds and drives from an AI agent. |
| Repository | https://github.com/vincesector/filebridge |
| Homepage | https://filebridge.evinho.xyz (not live yet) |
| License | MIT |
| Language / runtime | Python 3.12+ |
| Version | 0.2.0 |
| Transport | stdio |
| Auth / env vars | None required |
| Optional dependencies | rclone (transfer tools), Brave or Chrome (browser tools) |
| Platform | Tested on macOS only; Linux and Windows best effort and untested |
| Tags | file upload, browser automation, rclone, cloud storage, filesystem, chrome devtools protocol, claude code, codex, macos |
| MCP Registry name | io.github.vincesector/filebridge |

## Long description

filebridge is an MCP server and CLI that lets an AI agent upload files of any size into the browser you are already signed in to, and move files anywhere else.

Agents fail at "upload this video to my Drive" for three boring reasons: native file pickers cannot be driven, browser-extension uploads are capped at about 10 MB, and headless browsers have no logins. filebridge gives the agent four paths around that, and each one hands real disk paths to something that reads the file itself.

1. `browser_upload_own` serves your files on one-time links bound to 127.0.0.1 and returns a JavaScript snippet. The agent runs it in your signed-in tab with any tool that can execute JavaScript there, and the page fetches the files from localhost and attaches them.
2. A CDP browser (a dedicated headless profile, or your own Chrome/Brave attached over DevTools) with `browser_set_files`, `browser_drop_files` and `browser_click(files=...)`.
3. rclone for cloud storage and NAS: S3/R2, SFTP, Drive, Dropbox, OneDrive and others, as resumable background jobs.
4. Local helpers: rsync copy to drives and the iCloud Drive folder, and files on the clipboard for pasting into desktop apps.

Safety: a hard denylist for credentials (keys, .env, cookies, rclone config), a configurable allowlist of folders with an approval prompt for anything outside it, loopback-only servers, single-use links, and no delete tool.

Limits: tested on macOS only; Linux and Windows are best effort and untested. `browser_upload_own` can be blocked by a page's Content Security Policy or the browser's local-network protection. Sites with bot detection may block headless browsers. rclone is only needed for the transfer tools, and Brave or Chrome only for the browser tools. No API keys or environment variables are required.

## Install config

Works today (no PyPI needed):

```bash
claude mcp add filebridge --scope user -- uvx --from git+https://github.com/vincesector/filebridge filebridge mcp
```

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "filebridge": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/vincesector/filebridge", "filebridge", "mcp"]
    }
  }
}
```

Codex (`~/.codex/config.toml`):

```toml
[mcp_servers.filebridge]
command = "uvx"
args = ["--from", "git+https://github.com/vincesector/filebridge", "filebridge", "mcp"]
```

After the PyPI release of `filebridge-mcp` (the package ships a `filebridge-mcp` alias of the `filebridge` CLI):

```bash
claude mcp add filebridge --scope user -- uvx filebridge-mcp mcp
```

```json
{
  "mcpServers": {
    "filebridge": {
      "command": "uvx",
      "args": ["filebridge-mcp", "mcp"]
    }
  }
}
```

## Tools

| Tool | What it does |
|---|---|
| `browser_upload_own` | Serves local files on one-time 127.0.0.1 links and returns a JS snippet that your own signed-in browser tab runs to fetch and attach them (no size cap from the extension) |
| `serve_status` | State of a `browser_upload_own` server and each file (pending / serving / served / failed) |
| `serve_stop` | Stops a `browser_upload_own` server now; its links stop working |
| `browser_launch` | Starts or reuses the automation browser (headless, visible, or attached to your Chrome/Brave over DevTools) |
| `browser_close` | Closes the automation browser (profile kept); in attach mode it only detaches |
| `browser_tabs` | Lists open tabs (id, title, url) |
| `browser_snapshot` | Lists interactive elements, file inputs and drop zones with refs like `e12` |
| `browser_set_files` | Attaches local files of any size to an `<input type=file>` through CDP; the browser reads them from disk |
| `browser_drop_files` | Drops local files onto a drag-and-drop upload area with real CDP drag events |
| `browser_click` | Clicks an element; with `files=[...]` it fills the file chooser the click opens, so no native picker appears |
| `browser_type` | Types into a field picked by ref or selector |
| `browser_wait` | Waits for a CSS selector or visible text, for example "Upload complete" |
| `browser_navigate` | Points an existing tab at a URL and warns with `NotSignedIn` on sign-in pages |
| `browser_text` | Reads the visible text of a page or element |
| `browser_screenshot` | Saves a PNG of a tab and returns its path |
| `remotes` | Lists configured rclone remotes and their backend types (never prints tokens) |
| `transfer` | Copies, moves or syncs a file or folder to or from an rclone remote as a background job |
| `job_status` | Progress of a background job: state, percent, bytes, speed, ETA, errors |
| `jobs` | Lists recent background jobs |
| `cancel` | Stops a running job (partial files are not deleted) |
| `ls` | Lists a remote or local path |
| `link` | Creates or returns a shareable link for a remote file or folder (backend must support it) |
| `mkdir` | Creates a folder on a remote |
| `stat` | Checks whether a remote path exists and returns size, type and modified time |
| `setup_instructions` | Prints the rclone command the user runs to add a remote; filebridge never configures accounts |
| `local_copy` | Copies local files with rsync as a background job (external drive, NAS mount, iCloud Drive folder) |
| `clipboard_files` | Puts files on the clipboard as file references so Cmd+V pastes them into Slack, Mail, Finder |
| `reveal` | Shows a path in Finder or the platform file manager (opens a window) |

28 tools in total. `filebridge mcp --slim` exposes only the upload subset.

## Steps (do not run until approved)

1. Release `filebridge-mcp` to PyPI with the `mcp-name` line in the README.
2. `mcp-publisher login github`, `mcp-publisher publish`.
3. Check pulsemcp.com a few days later. If it is missing and the form is open again, submit the repo URL.
