"""Serve local files to the user's own (already signed-in) browser, so a page can pull them in by script.

Why: the Claude-in-Chrome upload tool caps files at 10 MB, and the headless filebridge browser has no logins.
This runs a tiny HTTP server on 127.0.0.1 with a random token in every URL and CORS + Private Network Access
headers, then prints a JS snippet. Run the snippet in the page (e.g. via the browser extension's javascript tool):
it fetches the files, wraps them as File objects and either fills an <input type=file> or drops them on a zone.
"""

import json
import mimetypes
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote


def _snippet(urls: list[str], selector: str | None) -> str:
    # Top-level-await statements (the browser extension's javascript tool returns the last expression; an IIFE's
    # promise is not awaited there). selector -> fill that file input; otherwise drop on the element under the
    # page centre (or document.body), which is how drag-and-drop upload zones (Drive, Typefully, X) listen.
    return (
        "const urls=" + json.dumps(urls) + ";const sel=" + json.dumps(selector) + ";"
        "const dt=new DataTransfer();let bytes=0;"
        "for(const u of urls){const r=await fetch(u);if(!r.ok)throw new Error('fetch '+r.status+' '+u);"
        "const b=await r.blob();bytes+=b.size;const name=decodeURIComponent(u.split('/').pop());"
        "dt.items.add(new File([b],name,{type:b.type||'application/octet-stream',lastModified:Date.now()}));}"
        "if(sel){const i=document.querySelector(sel);if(!i)throw new Error('no element '+sel);"
        "i.files=dt.files;i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new Event('change',{bubbles:true}));"
        "'set '+dt.files.length+' file(s), '+bytes+' bytes on '+sel;}else{"
        "const t=document.elementFromPoint(innerWidth/2,innerHeight/2)||document.body;"
        "for(const ev of ['dragenter','dragover','drop']){t.dispatchEvent(new DragEvent(ev,{bubbles:true,cancelable:true,dataTransfer:dt}));}"
        "for(const ev of ['dragleave']){t.dispatchEvent(new DragEvent(ev,{bubbles:true}));}'dropped '+dt.files.length+' file(s), '+bytes+' bytes on '+(t.tagName||'?');}"
    )


def serve(paths: list[str], port: int = 0, ttl: float = 1800, selector: str | None = None, ready=None) -> dict:
    files = {}
    for p in paths:
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isfile(p):
            raise FileNotFoundError(p)
        files[os.path.basename(p)] = p
    token = secrets.token_urlsafe(16)
    served: dict[str, int] = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", self.headers.get("Origin") or "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_GET(self):
            parts = self.path.split("/", 3)  # '', token, name
            if len(parts) < 3 or parts[1] != token or unquote(parts[2]) not in files:
                self.send_response(404)
                self._cors()
                self.end_headers()
                return
            name = unquote(parts[2])
            fp = files[name]
            size = os.path.getsize(fp)
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", mimetypes.guess_type(fp)[0] or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(fp, "rb") as f:
                while chunk := f.read(1 << 20):
                    self.wfile.write(chunk)
            served[name] = served.get(name, 0) + 1

    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    base = f"http://127.0.0.1:{httpd.server_address[1]}/{token}/"
    urls = [base + quote(n) for n in files]
    info = {"urls": urls, "snippet": _snippet(urls, selector), "expires_in_s": ttl}
    if ready:
        ready(info)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    deadline = time.time() + ttl
    try:
        while time.time() < deadline and not (served and all(served.get(n) for n in files)):
            time.sleep(0.5)
        time.sleep(2)  # let the last response finish streaming
    finally:
        httpd.shutdown()
    info["served"] = served
    return info
