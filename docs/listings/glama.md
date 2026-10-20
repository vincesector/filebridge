# Glama

Status: not submitted.

## How servers get added

glama.ai/mcp/servers has an "Add Server" link. The page text I could fetch does not describe the form, and Glama's docs I could reach say nothing about a Dockerfile requirement. From what I know of Glama (not re-verified): it indexes public GitHub repos, tracks the awesome-mcp-servers list, and the repo owner claims a listing by adding a `glama.json` listing maintainers. The schema at https://glama.ai/mcp/schemas/server.json requires a `maintainers` array of strings (verified). A Dockerfile is used by Glama to build and inspect the server and can improve the listing's score, but treat it as optional until the Add Server form says otherwise.

Prerequisites: the GitHub repo must be public. The homepage can stay unset until it is live. PyPI is not required.

The `glama.json` goes in the repo root when approved. Content to commit later (not created now):

```json
{
  "$schema": "https://glama.ai/mcp/schemas/server.json",
  "maintainers": ["vincesector"]
}
```

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

## Optional Dockerfile (only if Glama asks for one)

filebridge needs a real browser and a desktop clipboard for several tools, so a container build will mostly prove the server starts and lists its tools. Do not add it to the repo unless asked.

```dockerfile
FROM python:3.12-slim
RUN pip install --no-cache-dir git+https://github.com/vincesector/filebridge
ENTRYPOINT ["filebridge", "mcp"]
```

## Steps (do not run until approved)

1. Make the repo public.
2. Open glama.ai/mcp/servers, click Add Server, paste the repo URL.
3. Add `glama.json` to the repo and claim the listing from the maintainer GitHub account.
