---
name: upload-file-to-website
description: Upload or attach a local file to a website (Google Drive, X/Twitter, Typefully, YouTube, a web form, any <input type=file> or drag-and-drop zone) from the agent, with the filebridge MCP server. Use whenever the user asks to upload, attach, drop or send a file from disk to a site, when the browser extension's file_upload tool is unavailable or fails, when a file is over 10 MB, or when a native file picker would open.
---

# Upload a local file to a website with filebridge

Agents cannot drive the native file picker, browser-extension uploads stop at about 10 MB, and a fresh headless browser
is signed out of everything. filebridge (MCP server `filebridge`, https://github.com/vincesector/filebridge) hands the
browser real disk paths instead, so any size works. Never fall back to the extension's `file_upload` or a file picker.

If the filebridge tools are missing, tell the user to install them:
`claude mcp add filebridge --scope user -- uvx --from git+https://github.com/vincesector/filebridge filebridge mcp`

## Pick the path

| Situation | Tool |
|---|---|
| The user is signed in to the site in their own browser, and you can run JavaScript in that tab (Claude in Chrome's javascript tool or similar) | `browser_upload_own` |
| No login needed, or the user signed in once in filebridge's automation profile | `browser_launch` + `browser_snapshot` + `browser_set_files` / `browser_drop_files` / `browser_click(files=)` |
| The user runs Chrome/Brave with `--remote-debugging-port` | `browser_launch(mode="attach")`, then the same tools |
| Destination is cloud storage or a NAS with an rclone remote (S3, R2, SFTP, Dropbox...) | `remotes`, then `transfer` (skip the website) |

## Path 1: the user's own signed-in browser

1. Open or find the upload page in the user's browser (the folder in Drive, the compose box on X, the Typefully draft).
2. Call `browser_upload_own(paths=["/abs/or/~/path/file.mp4"], verify_text="file.mp4")`.
   - Add `selector="input[type=file]"` (or a more specific selector) when the page has a file input you can target.
     Without a selector the files are dropped on the element at the page centre, which is how Drive, X and Typefully
     listen.
3. Run the returned `snippet` in that tab with the browser extension's JavaScript tool, **exactly as returned**. It uses
   top-level `await`: do not wrap it in a function or IIFE. Its result is a JSON string:
   - `{"ok": true, "action": "set" | "dropped", "files": N, "bytes": B}`: the page has the file.
   - `{"ok": false, "code": "FetchBlocked"}`: the browser blocked localhost (the site's CSP, or the browser's
     local-network permission; ask the user to allow "Local network access" for the site), or the server already
     stopped. `"LinkUsed"`: each link works once. For either, call `browser_upload_own` again for fresh links.
   - `{"ok": false, "code": "SelectorNotFound"}`: fix the selector, or drop it to use drag-and-drop.
4. Run the returned `verify_snippet` in the same tab. `{"verified": true}` means the page shows the file name.
5. Call `serve_status(server_id)`: `state: "done"` with every file `served` confirms the browser fetched the bytes.
   The site may still be uploading; for big files wait for its own progress to finish before saying it is done.

## Path 2: automation or attached browser (CDP)

1. `browser_launch(url="https://site/upload")` (headless, dedicated profile). If `browser_navigate` or an upload call reports
   `NotSignedIn`, ask the user to sign in once: `browser_launch(mode="visible", url=...)`, they log in, then
   `browser_launch()` again. Do not type their password.
2. `browser_snapshot(tab="site")` lists buttons, file inputs (even hidden) and drop zones with refs like `e12`.
3. Upload:
   - File input: `browser_set_files(tab, paths, ref="e12", verify_text="file.mp4")`.
   - Drop zone: `browser_drop_files(tab, paths, ref="e7", verify_text="file.mp4")`.
   - "Upload" button that opens a picker: `browser_click(tab, ref="e3", files=[...])`.
4. Check `ok`, `sizes_match_disk` and `verified`. Use `browser_text` or `browser_screenshot` to see the page, and
   `browser_wait(tab, "Upload complete")` for slow uploads. Refs expire on navigation: snapshot again.

## Rules

- Paths must be absolute or start with `~`.
- `PathNotAllowed`: the file is outside the user's allowed folders and they declined (or could not be asked). Ask them
  to approve, or to add the folder to `allowed_roots` in `~/.filebridge/config.toml`. Never copy the file elsewhere to
  get around it.
- `PathBlocked`: a credential file (`.ssh`, `.env`, keys, cookies). Refuse; do not work around it.
- Do not paste the snippet or URLs anywhere except the target tab; they carry a one-time token.
- Do not post, publish or submit the form unless the user asked for that too. Attaching the file is the job.
- Report what happened with evidence: the snippet result, `verified`, and `serve_status`.
