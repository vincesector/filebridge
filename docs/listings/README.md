# Directory listings (drafts)

Drafting only. Nothing here has been submitted, committed or pushed. Research date: 2026-10-20.

Facts used everywhere: name `filebridge`, MIT, Python 3.12+, v0.2.0, stdio, no API keys. GitHub https://github.com/vincesector/filebridge, homepage https://filebridge.evinho.xyz (not live). PyPI distribution `filebridge-mcp` (not published yet). MCP Registry name `io.github.vincesector/filebridge`. Tested on macOS only.

| Directory | How to submit | URL | Needed first | Status |
|---|---|---|---|---|
| Official MCP Registry (feeds PulseMCP) | `mcp-publisher` CLI with `server.json`, GitHub login | https://registry.modelcontextprotocol.io | PyPI release of `filebridge-mcp`, public repo (`server.json` and the README `mcp-name` line are done) | not submitted |
| PulseMCP | Submit form is **closed** right now; publish to the Official MCP Registry and it is picked up automatically | https://www.pulsemcp.com/submit | Same as the Official MCP Registry | not submitted |
| Glama | "Add Server" on the directory, then `glama.json` to claim | https://glama.ai/mcp/servers | Public repo. Dockerfile only if the form asks | not submitted |
| Smithery | MCPB bundle via `smithery mcp publish` or the web flow | https://smithery.ai/new | Public repo, built and tested `.mcpb` bundle; no PyPI needed | not submitted |
| mcp.so | Form with repo URL and name (free reviewed tier, $39 instant tier) | https://mcp.so/submit | Public repo | not submitted |
| awesome-mcp-servers | Pull request adding one line to File Systems | https://github.com/punkpeye/awesome-mcp-servers | Public repo; Glama listing first is helpful | not submitted |
| anthropics/claude-code #91164 | One helpful comment | https://github.com/anthropics/claude-code/issues/91164 | Public repo | not posted |
| anthropics/claude-code #31210 | Do not reply (closed as duplicate and locked) | https://github.com/anthropics/claude-code/issues/31210 | n/a | skip |

Suggested order: make repo public, PyPI release, Official MCP Registry, Glama, mcp.so, awesome-mcp-servers PR (after Glama), Smithery (needs a tested bundle), then the #91164 comment.

Files: `smithery.md`, `glama.md`, `pulsemcp.md`, `mcp-so.md`, `awesome-mcp-servers.md`, `claude-code-issues.md`.

Open items I could not verify from public pages: Glama's Add Server form fields, whether Smithery accepts a `uv`-type MCPB bundle for a CLI entry point. The MCP Registry `server.json` at the repo root is validated against the 2025-12-11 schema.
