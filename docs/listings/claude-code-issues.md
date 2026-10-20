# anthropics/claude-code issues

Status: nothing posted. Checked 2026-10-20.

## What each issue asks

- **#31210**: "file_upload tool not exposed via Claude Code MCP connection" asks that the Claude in Chrome extension's `file_upload` tool be exposed to Claude Code (only `upload_image` is). State: closed as a duplicate of #25871 and **locked**. #25871 is also closed and locked.
- **#91164**: "Browser pane cannot upload files: no equivalent of chrome-devtools upload_file" asks for a file-upload tool in the in-app Browser pane that takes a ref and local paths. State: open, one comment (a +1 that also mentions Claude in Chrome's 10 MB cap).

## #31210: recommend not replying

The issue is locked, so a comment cannot be posted, and #25871 is locked too. Only a new issue would work, and a new issue that just links a tool is spam. Skip it.

## #91164: draft reply

The thread is open and the problem matches what filebridge does, so a reply is reasonable. Keep it as one comment, no follow-ups unless someone asks.

```
The gap is real, and the base64 and public-host workarounds scale badly, as you describe. For what it is worth, I built a small open-source MCP server, filebridge, that covers the case where the signed-in browser is your own. Its browser_upload_own tool serves the file on a one-time link bound to 127.0.0.1 and returns a JavaScript snippet; the agent runs that snippet with the browser tool's JavaScript execution, and the signed-in tab fetches the file itself and fills the input or drops it on the page, so no bytes pass through the tool call and there is no size cap. For a browser you can start with a debugging port, browser_set_files uses CDP DOM.setFileInputFiles and browser_click(files=[...]) intercepts the file chooser, which is the chrome-devtools shape you describe, including multiple files.

Limits, to be straight about them: a page's Content Security Policy or the browser's local-network protection can block the fetch from localhost, and I have only tested on macOS. I have not tried it in the Browser pane specifically, so I do not know whether the pane allows an https page to fetch from 127.0.0.1. The CDP tools also need a debugging port, so they will not reach the pane's own session. That is still why a native tool would be better than this.

I am the author. Repo: https://github.com/vincesector/filebridge
```

Before posting, confirm the repo is public and the quoted behaviour still matches the README.
