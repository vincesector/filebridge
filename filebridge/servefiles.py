"""Serve local files to the user's own (already signed-in) browser, so a page can pull them in by script.

Why: browser-extension upload tools cap files at about 10 MB, and the headless filebridge browser has no logins.
This runs a tiny HTTP server on 127.0.0.1 with a random token in every URL and CORS + Private Network Access
headers, then hands out a JS snippet. Run the snippet in the page (e.g. via the browser extension's javascript
tool): it fetches the files, wraps them as File objects and either fills an <input type=file> or drops them on a zone.

Every file URL works exactly once (a second GET gets 410 Gone), and the server stops when every file was fetched
or the TTL runs out, whichever is first.
"""

import json
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

from .common import FBError

BIG_FILE = 1 << 30  # above this, warn: the tab holds the whole file in memory


def snippet(urls: list[str], selector: str | None) -> str:
    # Top-level-await statements, not an IIFE (the browser extension's javascript tool returns the last expression;
    # an IIFE's promise is not awaited there). `var` and the __fb prefix let it run twice in one page.
    # selector -> fill that file input; otherwise drop on the element under the page centre (or document.body),
    # which is how drag-and-drop upload zones (Drive, Typefully, X) listen. The last expression is a JSON string:
    # {ok:true,...} or {ok:false, code, message}.
    return (
        "var __fbUrls=" + json.dumps(urls) + ";var __fbSel=" + json.dumps(selector) + ";var __fbRes;"
        "try{var __fbDt=new DataTransfer(),__fbBytes=0;"
        "for(const u of __fbUrls){let r;"
        "try{r=await fetch(u);}catch(e){throw Object.assign(new Error('Could not reach '+u+' ('+e.message+'). Either the browser blocked localhost: "
        "allow this site to access devices on your local network (site settings: Local network access) and run the snippet again; "
        "or the filebridge server already stopped (TTL over, or every link used): ask the agent for a new snippet.'),{fbCode:'FetchBlocked'});}"
        "if(r.status===410)throw Object.assign(new Error('This link was already used (each link works once). Ask the agent for a new snippet.'),{fbCode:'LinkUsed'});"
        "if(!r.ok)throw Object.assign(new Error('fetch '+r.status+' '+u+' (expired or stopped server?)'),{fbCode:'FetchFailed'});"
        "const b=await r.blob();__fbBytes+=b.size;const name=decodeURIComponent(u.split('/').pop());"
        "__fbDt.items.add(new File([b],name,{type:b.type||'application/octet-stream',lastModified:Date.now()}));}"
        "if(__fbSel){const i=document.querySelector(__fbSel);"
        "if(!i)throw Object.assign(new Error('no element matches '+__fbSel),{fbCode:'SelectorNotFound'});"
        "i.files=__fbDt.files;i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));"
        "__fbRes={ok:true,action:'set',files:__fbDt.files.length,bytes:__fbBytes,target:__fbSel};}else{"
        "const t=document.elementFromPoint(innerWidth/2,innerHeight/2)||document.body;"
        "for(const ev of ['dragenter','dragover','drop']){t.dispatchEvent(new DragEvent(ev,{bubbles:true,cancelable:true,dataTransfer:__fbDt}));}"
        "for(const x of [t,document.body,document.documentElement,document,window]){for(const ev of ['dragleave','dragend']){try{x.dispatchEvent(new DragEvent(ev,{bubbles:true,clientX:0,clientY:0}));}catch(e){}}}"
        "__fbRes={ok:true,action:'dropped',files:__fbDt.files.length,bytes:__fbBytes,target:t.tagName||'?'};}"
        "}catch(e){__fbRes={ok:false,code:e.fbCode||'Failed',message:e.message};}"
        "JSON.stringify(__fbRes);"
    )


def verify_snippet(text: str | None, selector: str | None, timeout: float = 60) -> str:
    """Polls the page until the text and/or selector shows up; last expression is a JSON string {verified, waited_s}."""
    return (
        "var __fbT=" + json.dumps(text) + ",__fbS=" + json.dumps(selector) + ",__fbT0=Date.now(),__fbOk=false;"
        "while(Date.now()-__fbT0<" + str(int(timeout * 1000)) + "){"
        "__fbOk=(__fbS===null||!!document.querySelector(__fbS))&&(__fbT===null||(document.body&&document.body.innerText.includes(__fbT)));"
        "if(__fbOk)break;await new Promise(r=>setTimeout(r,250));}"
        "JSON.stringify({verified:__fbOk,waited_s:Math.round((Date.now()-__fbT0)/100)/10});"
    )


class FileServer:
    """One token-protected, single-use-per-file HTTP server on 127.0.0.1, running in background threads."""

    def __init__(self, paths: list[str], port: int = 0, ttl: float = 1800, selector: str | None = None):
        """paths must already be checked and resolved (policy.check_sources). The display name is the file's basename."""
        if not paths:
            raise FBError("Give at least one file.", "InvalidArgument")
        self.files: list[dict] = []
        for i, p in enumerate(paths):
            if not os.path.isfile(p):
                raise FBError(f"Not a file: {p}", "FileNotFound")
            self.files.append({"name": os.path.basename(p), "path": p, "bytes": os.path.getsize(p), "state": "pending"})
        self.token = secrets.token_urlsafe(24)
        self.ttl = ttl
        self.selector = selector
        self.id = secrets.token_hex(6)
        self._lock = threading.Lock()
        self._inflight = 0
        self.stopped_reason: str | None = None
        server = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")

            def _status(self, code: int):
                self.send_response(code)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_GET(self):
                parts = self.path.split("/")  # '', token, index, name
                if len(parts) != 4 or not secrets.compare_digest(parts[1], server.token) or not parts[2].isdigit() \
                        or int(parts[2]) >= len(server.files) or unquote(parts[3]) != server.files[int(parts[2])]["name"]:
                    return self._status(404)
                f = server.files[int(parts[2])]
                with server._lock:
                    if f["state"] != "pending" or server.stopped_reason:
                        return self._status(410)
                    f["state"] = "serving"
                    server._inflight += 1
                try:
                    self.send_response(200)
                    self._cors()
                    self.send_header("Content-Type", mimetypes.guess_type(f["name"])[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(f["bytes"]))
                    self.end_headers()
                    with open(f["path"], "rb") as fh:
                        while chunk := fh.read(1 << 20):
                            self.wfile.write(chunk)
                    f["state"] = "served"
                except OSError:
                    f["state"] = "failed"  # client went away mid-transfer; the link is still used up
                finally:
                    with server._lock:
                        server._inflight -= 1

        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        base = f"http://127.0.0.1:{self.port}/{self.token}/"
        self.urls = [f"{base}{i}/{quote(f['name'])}" for i, f in enumerate(self.files)]
        self.started = time.time()
        self.deadline = self.started + ttl
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def info(self) -> dict:
        warnings = [f"{f['name']} is {f['bytes'] / (1 << 30):.1f} GiB: the browser tab holds the whole file in memory while uploading. "
                    "Close other heavy tabs; for very large files prefer transfer (rclone) or the headless browser_set_files."
                    for f in self.files if f["bytes"] > BIG_FILE]
        out = {"server_id": self.id, "urls": self.urls, "snippet": snippet(self.urls, self.selector), "expires_in_s": round(self.deadline - time.time()),
               "files": [{"name": f["name"], "bytes": f["bytes"]} for f in self.files]}
        if warnings:
            out["warnings"] = warnings
        return out

    def _all_claimed_and_idle(self) -> bool:
        with self._lock:
            return self._inflight == 0 and all(f["state"] != "pending" for f in self.files)

    def _watch(self) -> None:
        while not self.stopped_reason:
            if self._all_claimed_and_idle():
                self.stop("done")
            elif time.time() >= self.deadline:
                self.stop("expired")
            time.sleep(0.25)

    def stop(self, reason: str = "stopped") -> None:
        with self._lock:
            if self.stopped_reason:
                return
            self.stopped_reason = reason
        def close():
            self._httpd.shutdown()
            self._httpd.server_close()  # without this, late requests hang in the listen backlog instead of being refused
        threading.Thread(target=close, daemon=True).start()

    def status(self) -> dict:
        state = self.stopped_reason or "running"
        files = [{"name": f["name"], "bytes": f["bytes"], "state": f["state"]} for f in self.files]
        return {"server_id": self.id, "state": state, "served": sum(f["state"] == "served" for f in self.files), "total": len(self.files),
                "failed": sum(f["state"] == "failed" for f in self.files), "files": files,
                "expires_in_s": max(0, round(self.deadline - time.time())) if state == "running" else 0}

    def wait(self) -> dict:
        while not self.stopped_reason or self._inflight:
            time.sleep(0.25)
        time.sleep(0.5)  # let the last response finish flushing
        return self.status()


_servers: dict[str, FileServer] = {}


def start(paths: list[str], ttl: float = 1800, selector: str | None = None, port: int = 0) -> FileServer:
    s = FileServer(paths, port, ttl, selector)
    _servers[s.id] = s
    return s


def get(server_id: str) -> FileServer:
    s = _servers.get(server_id)
    if not s:
        raise FBError(f"No serve session {server_id!r} in this filebridge process (ids: {', '.join(_servers) or 'none'}).", "InvalidArgument")
    return s
