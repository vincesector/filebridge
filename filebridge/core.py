"""The single action layer. The MCP server (server.py) and the CLI (cli.py) both call exactly these
functions, so behaviour, validation and result shapes can never drift apart."""

from typing import Any

from . import browser, jobs as jobs_mod, local, policy, rclone, servefiles

# ---- Lane 1: rclone


async def remotes() -> dict[str, Any]:
    r = await rclone.list_remotes()
    return {"remotes": [{"name": n, "type": t, "use_as": f"{n}:path"} for n, t in r.items()], "count": len(r)}


async def transfer(src: str, dest: str, mode: str = "copy", dry_run: bool = False, overwrite: bool = False,
                   confirm: bool = False, bwlimit: str | None = None) -> dict[str, Any]:
    return await rclone.start_transfer(src, dest, mode, dry_run, overwrite, confirm, bwlimit)


async def job_status(job_id: str) -> dict[str, Any]:
    return rclone.status(job_id)


async def jobs(limit: int = 20) -> dict[str, Any]:
    return {"jobs": rclone.summarise_jobs(limit)}


async def cancel(job_id: str) -> dict[str, Any]:
    return jobs_mod.cancel_job(job_id)


async def ls(remote_path: str, recursive: bool = False, max: int = 200) -> dict[str, Any]:
    return await rclone.ls(remote_path, recursive, max)


async def link(remote_path: str) -> dict[str, Any]:
    return await rclone.link(remote_path)


async def mkdir(remote_path: str) -> dict[str, Any]:
    return await rclone.mkdir(remote_path)


async def stat(remote_path: str) -> dict[str, Any]:
    return await rclone.stat(remote_path)


async def setup_instructions(backend: str | None = None) -> dict[str, Any]:
    return await rclone.setup_instructions(backend)


# ---- Lane 2: headless browser over CDP


async def browser_launch(url: str | None = None, browser_name: str = "auto", mode: str = "headless", cdp_url: str | None = None) -> dict[str, Any]:
    return await browser.launch(url, browser_name, mode, cdp_url)


async def browser_close() -> dict[str, Any]:
    return await browser.close()


async def browser_tabs() -> dict[str, Any]:
    return {"tabs": await browser.tabs()}


async def browser_snapshot(tab: str, max_items: int = 300) -> dict[str, Any]:
    return await browser.snapshot(tab, max_items)


async def browser_set_files(tab: str, paths: list[str], selector: str | None = None, ref: str | None = None, verify_text: str | None = None,
                            verify_selector: str | None = None, verify_timeout: float = 30) -> dict[str, Any]:
    return await browser.set_files(tab, selector, paths, ref, verify_text, verify_selector, verify_timeout)


async def browser_drop_files(tab: str, paths: list[str], selector: str | None = None, x: float | None = None, y: float | None = None,
                             method: str = "auto", ref: str | None = None, verify_text: str | None = None,
                             verify_selector: str | None = None, verify_timeout: float = 30) -> dict[str, Any]:
    return await browser.drop_files(tab, paths, selector, x, y, method, ref, verify_text, verify_selector, verify_timeout)


async def browser_click(tab: str, selector: str | None = None, files: list[str] | None = None, ref: str | None = None) -> dict[str, Any]:
    return await browser.click(tab, selector, files, ref)


async def browser_wait(tab: str, selector_or_text: str, timeout: float = 60) -> dict[str, Any]:
    return await browser.wait(tab, selector_or_text, timeout)


async def browser_navigate(tab: str, url: str, timeout: float = 30) -> dict[str, Any]:
    return await browser.navigate(tab, url, timeout)


async def browser_text(tab: str, selector: str | None = None, max_chars: int = 8000) -> dict[str, Any]:
    return await browser.page_text(tab, selector, max_chars)


async def browser_screenshot(tab: str, path: str | None = None, full_page: bool = False) -> dict[str, Any]:
    return await browser.screenshot(tab, path, full_page)


async def browser_type(tab: str, selector: str | None, text: str, clear: bool = False, press_enter: bool = False, ref: str | None = None) -> dict[str, Any]:
    return await browser.type_text(tab, selector, text, clear, press_enter, ref)


# ---- Lane 3: your own signed-in browser, through a localhost server


async def upload_own(paths: list[str], selector: str | None = None, ttl: float = 1800, port: int = 0) -> servefiles.FileServer:
    files = await policy.check_sources(paths, action="serve", destination="your own browser (one-time localhost links)", files_only=True)
    return servefiles.start(files, ttl, selector, port)


async def browser_upload_own(paths: list[str], selector: str | None = None, ttl: float = 1800, verify_text: str | None = None,
                             verify_selector: str | None = None, verify_timeout: float = 60) -> dict[str, Any]:
    srv = await upload_own(paths, selector, ttl)
    out = {"ok": True, **srv.info(),
           "next": "Run `snippet` in the signed-in tab (e.g. your browser extension's JavaScript tool). It returns a JSON string: "
                   "{ok:true,...} or {ok:false, code, message} (code FetchBlocked = the browser blocked localhost; LinkUsed = run browser_upload_own again). "
                   "Each URL works once. Poll serve_status(server_id) to see what was fetched."}
    if verify_text is not None or verify_selector is not None:
        out["verify_snippet"] = servefiles.verify_snippet(verify_text, verify_selector, verify_timeout)
        out["next"] += " Then run `verify_snippet`: it waits until the page shows the text/selector and returns {verified, waited_s}."
    return out


async def serve_status(server_id: str) -> dict[str, Any]:
    return servefiles.get(server_id).status()


async def serve_stop(server_id: str) -> dict[str, Any]:
    s = servefiles.get(server_id)
    s.stop("stopped")
    return s.status()


# ---- Lane 4: local helpers


async def local_copy(src: str, dest: str, overwrite: bool = False, dry_run: bool = False) -> dict[str, Any]:
    return await local.local_copy(src, dest, overwrite, dry_run)


async def clipboard_files(paths: list[str]) -> dict[str, Any]:
    return await local.clipboard_files(paths)


async def reveal(path: str) -> dict[str, Any]:
    return await local.reveal(path)
