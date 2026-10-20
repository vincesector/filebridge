# Live tests (manual, real sites)

Run these after the offline suite passes (`tests/test7_policy_offline.py`, `tests/test8_browser_v02.py`). They need a
human: real accounts, the user's Brave and Chrome, and the browser's own prompts. Use a throwaway upload folder on each
site and delete the test uploads afterwards by hand. Record pass/fail and the filebridge result JSON for each line.

Test files: a small one (`~/Movies/fb-live/small.png`, under 1 MB), a medium one (`~/Movies/fb-live/med.mp4`, about
300 MB), a big one (`~/Movies/fb-live/big.mp4`, over 1 GiB) and one outside the allowed roots (`/tmp/fb-live/outside.mp4`).

## 1. browser_upload_own in the user's own browser

For each of Brave and Chrome, with the Claude in Chrome extension (or any JavaScript-capable tool) on the tab:

- [ ] **Google Drive** (signed in, a test folder open): `browser_upload_own(paths=[med.mp4])`, run `snippet` with no
      selector. The file shows up in the folder at full size. `serve_status` says `done`, served 1/1.
- [ ] Drive with `verify_text="med.mp4"`: `verify_snippet` returns `{verified: true}` once the upload row appears.
- [ ] **X** compose box: `browser_upload_own(paths=[small.png])`, snippet with no selector (drop) or with the compose
      file input selector. The image attaches to the draft. Do not post.
- [ ] **Typefully** draft: same as X with small.png; the image attaches.
- [ ] Big file to Drive (`big.mp4`): result has the 1 GiB `warnings` entry; upload completes; note tab memory.
- [ ] Run the same snippet a second time: result `{ok: false, code: "FetchBlocked"}` with the "already stopped" hint
      (server stopped after the first fetch). With two files and one link fetched by hand first: `LinkUsed`.
- [ ] **Local network permission**: in a browser version that prompts for local network access, the first run either
      shows the prompt or returns `FetchBlocked` with the "allow local network access" advice. After allowing it, a new
      `browser_upload_own` + snippet works.
- [ ] Page with a strict CSP (any site that blocks `connect-src` to localhost): `FetchBlocked`, and the message is
      understandable.
- [ ] `ttl=30`, wait 40 s, run the snippet: `FetchBlocked`; `serve_status` says `expired`.

## 2. Approval (elicitation) in real clients

- [ ] Claude Code, normal permission mode: `browser_upload_own(paths=["/tmp/fb-live/outside.mp4"])` shows an approval
      prompt with the path, size and destination. Yes uploads; No returns `PathNotAllowed`.
- [ ] Claude Code in an auto-accept / bypass mode: note whether the elicitation is auto-answered (expected per the
      README warning). Record the client version.
- [ ] Codex: same check; if Codex does not support elicitation, the result is `PathNotAllowed` naming `allowed_roots`.
- [ ] `browser_upload_own(paths=["~/.ssh/id_ed25519"])` and a project `.env`: `PathBlocked`, no prompt, in every client.
- [ ] A symlink in `~/Movies` pointing to `~/.ssh/id_ed25519`: `PathBlocked`.
- [ ] CLI on a terminal: `filebridge serve /tmp/fb-live/outside.mp4` asks `y/N`; `n` exits 1 with `[PathNotAllowed]`.

## 3. Attach mode

- [ ] Chrome with `--remote-debugging-port=9222 --user-data-dir=$HOME/.chrome-agent`, signed in to Drive in that
      window: `browser_launch(mode="attach")` lists its tabs; `browser_snapshot` on the Drive tab shows the upload
      button / drop zone; `browser_click(ref=<New / File upload>, files=[med.mp4])` or `browser_set_files` uploads.
- [ ] Same with Brave (`"/Applications/Brave Browser.app/Contents/MacOS/Brave Browser" --remote-debugging-port=9222 --user-data-dir=$HOME/.brave-agent`).
- [ ] `browser_close` in attach mode: the browser stays open, filebridge reports `detached`.
- [ ] Chrome's `chrome://inspect/#remote-debugging` toggle on the default profile (if this Chrome has it):
      `browser_launch(mode="attach")` with no `cdp_url` finds the `DevToolsActivePort` endpoint; Chrome shows its allow
      prompt; note whether every tool call prompts again. Mark this path supported or not in the README.
- [ ] Start Chrome with `--remote-debugging-address=0.0.0.0` (on a trusted network only, then close it): attach is
      refused because the port is not loopback-only.

## 4. Automation browser on real sites

- [ ] `browser_launch(mode="visible", url="https://drive.google.com")`, sign in, `browser_launch()` again (headless).
      `browser_snapshot` shows file inputs and the drop zone; `browser_drop_files(ref=..., verify_text="med.mp4")`
      uploads and returns `verified: true`.
- [ ] Headless on a site you are not signed in to in that profile: `browser_set_files` returns `NotSignedIn`.
- [ ] `filebridge mcp --slim` in Claude Code: only the 9 upload tools are listed, and an upload works end to end.

## 5. Install

- [ ] On a clean machine (or a fresh user): `claude mcp add filebridge --scope user -- uvx --from git+https://github.com/vincesector/filebridge filebridge mcp`
      after the branch is pushed; the server starts and `~/.filebridge/config.toml` is created on the first file call.
- [ ] Linux and Windows (if available): browser discovery, `clipboard_files`, `reveal`. Update the "untested" notes.
