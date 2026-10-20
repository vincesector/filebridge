# punkpeye/awesome-mcp-servers

Status: not submitted.

## Rules read from the repo (CONTRIBUTING.md and README, 2026-10-20)

- One server per line, alphabetical within its category, follow the existing format.
- Entry: `[owner/repo](url)`, then emoji legend items, then ` - description`.
- Legend: language 🐍 Python; scope 🏠 Local Service (use local when the server controls locally installed software, "e.g. taking control over Chrome browser"); OS 🍎 macOS (also 🪟 Windows, 🐧 Linux).
- Only add OS icons that are tested. filebridge is tested on macOS only, so 🍎 only, not 🪟 🐧.
- A bot can add the Glama score badge after the Glama listing exists; do not add it by hand.
- Do not add the `🤖🤖🤖` PR title suffix: that is an opt-in for automated agents, and this PR is opened by the owner.

## Target section

**File Systems** (`### 📂 File Systems`). Why: the product is moving files from disk to a destination (browser forms, cloud via rclone, drives, clipboard). Browser Automation is the runner-up, but there the tools are mostly page-driving servers (click, navigate, scrape), and filebridge's browser tools exist for one job, attaching local files. File Systems also describes the rclone, rsync and clipboard tools that Browser Automation would not. Alphabetical spot: by `owner/repo`, so after the `v...` entries and before `w...`/`z...`, per whatever ordering that section currently uses; check neighbours when opening the PR.

## The line

```
- [vincesector/filebridge](https://github.com/vincesector/filebridge) 🐍 🏠 🍎 - Upload files of any size from an AI agent into your signed-in browser (one-time localhost links), a CDP browser, rclone clouds and NAS, drives and the clipboard.
```

## PR

Title:

```
Add filebridge
```

Body:

```
Adds filebridge to File Systems.

filebridge is an MIT-licensed Python MCP server (stdio, 28 tools) for moving files from disk to places agents usually cannot reach: file inputs and drop zones in the user's own signed-in browser or an attached Chrome/Brave, rclone remotes (S3/R2, SFTP/NAS, Drive, Dropbox, OneDrive), drives, and the clipboard. No API keys needed. Tested on macOS only; Linux and Windows are best effort, so the entry carries only the macOS icon.

Repo: https://github.com/vincesector/filebridge
```

Prerequisites: repo public. Listing on Glama first is useful because this README is synced with the Glama directory. PyPI is not required, but the README install line should work for a reader (it does today, via the git URL).
