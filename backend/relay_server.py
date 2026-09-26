#!/usr/bin/env python
"""Fetch-only local relay for the deployed InstaIQ backend (zero API keys).

Instagram hard-blocks datacenter IPs, so the deployed backend cannot fetch
live data — but this machine (real Chrome on a residential IP) is not
blocked. relay_server.py exposes exactly one endpoint:

    GET /?username=<instagram_handle>
    header: x-relay-token: <token>   (required when --token is set)

It runs the same real-Chrome helper the local backend uses
(chrome_fetch.cjs: puppeteer-core + installed Chrome) and returns the JSON
payload {ok, html, profile_api, feed} that scraper.py already knows how to
parse. Nothing else: no keys, no storage, no other routes.

Started automatically by tunnel.py — you rarely need to run this directly:

    python relay_server.py --port 8765 --token <token>
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_HERE = os.path.dirname(os.path.abspath(__file__))
_HELPER = os.path.join(_HERE, "chrome_fetch.cjs")
_TIMEOUT = int(os.getenv("IG_CHROME_FETCH_TIMEOUT", "120")) + 15
_TOKEN = ""
_SEM = threading.BoundedSemaphore(1)  # one Chrome render at a time; others queue

_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")

_CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.getenv("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
)


def _find_chrome() -> str:
    env_path = os.getenv("IG_CHROME_PATH", "")
    for c in (env_path,) + _CHROME_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    found = shutil.which("chrome") or shutil.which("chrome.exe") or shutil.which("chromium")
    return found or ""


def _find_node_modules() -> str:
    for rel in ("node_modules", os.path.join("..", "frontend", "node_modules"),
                os.path.join("..", "..", "node_modules")):
        if os.path.isfile(os.path.join(_HERE, rel, "puppeteer-core", "package.json")):
            return os.path.abspath(os.path.join(_HERE, rel))
    return ""


def _render(username: str) -> dict:
    """Run chrome_fetch.cjs for one handle and return its JSON payload."""
    chrome = _find_chrome()
    if not chrome:
        return {"ok": False, "kind": "nochrome", "error": "local Chrome not found on relay host"}
    node_modules = _find_node_modules()
    if not node_modules:
        return {"ok": False, "kind": "nopuppeteer", "error": "puppeteer-core not found on relay host"}
    node = shutil.which("node") or shutil.which("node.exe")
    if not node:
        return {"ok": False, "kind": "nonode", "error": "node not on PATH on relay host"}

    env = dict(os.environ)
    env["IG_CHROME_PATH"] = chrome
    env["NODE_PATH"] = node_modules
    cmd = [node, _HELPER, f"--url=https://www.instagram.com/{username}/"]
    with _SEM:
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=_TIMEOUT,
                                  env=env, cwd=_HERE)
        except subprocess.TimeoutExpired:
            return {"ok": False, "kind": "timeout", "error": f"render exceeded {_TIMEOUT}s"}
        except OSError as e:
            return {"ok": False, "kind": "spawn", "error": str(e)[:160]}
    try:
        return json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except Exception:
        tail = (proc.stderr or b"").decode("utf-8", errors="replace")[-200:]
        return {"ok": False, "kind": "helper",
                "error": f"unparseable helper output (rc={proc.returncode}): {tail}"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "InstaIQHomeRelay/1.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802 (http.server API name)
        if _TOKEN and self.headers.get("x-relay-token") != _TOKEN:
            self._send(401, {"ok": False, "kind": "unauthorized",
                             "error": "bad or missing x-relay-token"})
            return
        parsed = urlparse(self.path)
        if parsed.path not in ("", "/"):
            self._send(404, {"ok": False, "kind": "noroute",
                             "error": "only GET /?username=<handle> is served"})
            return
        username = (parse_qs(parsed.query).get("username") or [""])[0].strip().lstrip("@")
        if not _HANDLE_RE.match(username):
            self._send(400, {"ok": False, "kind": "badhandle",
                             "error": "query param ?username=<instagram_handle> required"})
            return
        result = _render(username)
        self._send(404 if result.get("kind") == "notfound" else 200, result)

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write(f"[relay] {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()


def main() -> None:
    global _TOKEN
    ap = argparse.ArgumentParser(description="InstaIQ fetch-only home relay")
    ap.add_argument("--port", type=int, default=int(os.getenv("RELAY_PORT", "8765")))
    ap.add_argument("--token", default=os.getenv("IG_RELAY_PROFILE_TOKEN", ""))
    args = ap.parse_args()
    _TOKEN = args.token
    if not _TOKEN:
        print("[relay] WARNING: no token set — anyone with the URL can render fetches")
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"[relay] listening on http://127.0.0.1:{args.port} "
          f"(token {'set' if _TOKEN else 'NOT SET'}, helper={_HELPER})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
