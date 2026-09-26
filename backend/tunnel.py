#!/usr/bin/env python
"""One-command free tunnel: expose this PC's real-Chrome fetcher to the
deployed Vercel backend. Zero API keys, zero accounts.

What it does:
  1. Generates/loads a random relay token (saved to backend/.tunnel_state.json).
  2. Starts relay_server.py on 127.0.0.1:<port> (fetch-only, token-guarded).
  3. Starts cloudflared (backend/tools/cloudflared.exe) as a free Quick Tunnel.
  4. AUTO-SYNC: when the public URL (or token) differs from what was last
     pushed to Vercel, it updates IG_HOME_RELAY_URL / IG_HOME_RELAY_TOKEN on
     the linked project (backend/.vercel) and redeploys production. Disable
     with --no-deploy to only print the manual commands.
  5. Waits (Ctrl+C stops everything cleanly).

Quick Tunnel URLs are ephemeral: they change every run — but with auto-sync
you just re-run `python tunnel.py` and the deployed backend is repointed
automatically. (A stable named tunnel needs a free Cloudflare account.)

Usage:  python tunnel.py [--port 8765] [--no-deploy]
"""
import argparse
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATE = os.path.join(_HERE, ".tunnel_state.json")
_RELAY = os.path.join(_HERE, "relay_server.py")
_CF = os.path.join(_HERE, "tools",
                   "cloudflared.exe" if os.name == "nt" else "cloudflared")
_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _vercel(cli_args: list, *, input_text: str = None, timeout: int = 120):
    """Run the vercel CLI from backend/ (linked project). Returns (ok, output)."""
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        return False, "npx not found on PATH"
    try:
        p = subprocess.run(
            [npx, "--no-install", "vercel", *cli_args],
            cwd=_HERE, input=input_text, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        return p.returncode == 0, (p.stdout or "") + (p.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)[:200]


def _set_vercel_env(name: str, value: str) -> bool:
    """Overwrite one production env var (rm ignores not-found, then add)."""
    _vercel(["env", "rm", name, "production", "--yes"])
    ok, out = _vercel(["env", "add", name, "production"], input_text=value + "\n")
    if not ok:
        print(f"[tunnel] vercel env add {name} failed: {out.strip()[-160:]}")
    return ok


def _sync_vercel(url: str, token: str, state: dict) -> bool:
    """Point the deployed backend at this relay (env vars + prod redeploy).
    Skips everything when Vercel already matches. Returns True when the
    deployment is live with the current URL/token."""
    needs_url = state.get("deployed_url") != url
    needs_token = state.get("deployed_token") != token
    if not needs_url and not needs_token:
        print("[tunnel] Vercel already points at this URL/token — no redeploy needed")
        return True

    print("[tunnel] syncing Vercel env vars + redeploying (a couple of minutes)...")
    if needs_url and not _set_vercel_env("IG_HOME_RELAY_URL", url):
        return False
    if needs_token and not _set_vercel_env("IG_HOME_RELAY_TOKEN", token):
        return False

    ok, out = _vercel(["--prod", "--yes"], timeout=420)
    if not ok:
        print(f"[tunnel] vercel deploy failed: {out.strip()[-200:]}")
        return False
    m = re.search(r"https://[a-z0-9-]+\.vercel\.app", out)
    print(f"[tunnel] deployed: {m.group(0) if m else 'production updated'}")
    return True


def _print_manual(url: str, token: str) -> None:
    print("\n" + "=" * 74)
    print("HOME RELAY IS LIVE — update the deployed backend manually:\n")
    print(f"  IG_HOME_RELAY_URL   = {url}")
    print(f"  IG_HOME_RELAY_TOKEN = {token}\n")
    print("Run from backend/ (project 'instaiq-api'), then redeploy:\n")
    print(f'  echo {url} | npx vercel env add IG_HOME_RELAY_URL production')
    print(f'  echo {token} | npx vercel env add IG_HOME_RELAY_TOKEN production')
    print("  npx --no-install vercel --prod --yes\n")
    print("Keep this window open — closing it takes the relay offline.")
    print("(Quick Tunnel URLs are ephemeral; re-run to get a new URL/teach Vercel.)")
    print("=" * 74 + "\n", flush=True)


def _load_state() -> dict:
    try:
        with open(_STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    with open(_STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def main() -> None:
    ap = argparse.ArgumentParser(description="InstaIQ free home-relay tunnel")
    ap.add_argument("--port", type=int, default=int(os.getenv("RELAY_PORT", "8765")))
    ap.add_argument("--no-deploy", action="store_true",
                    help="do not touch Vercel — just print the manual commands")
    args = ap.parse_args()

    if not os.path.isfile(_CF):
        sys.exit(f"cloudflared not found at {_CF} — see README in backend/tools/")
    state = _load_state()
    token = state.get("token") or secrets.token_urlsafe(24)

    procs: list = []

    def _cleanup(*_a) -> None:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    print("[tunnel] starting local relay (token-guarded, fetch-only)...")
    relay = subprocess.Popen(
        [sys.executable, _RELAY, "--port", str(args.port), "--token", token],
        cwd=_HERE,
    )
    procs.append(relay)
    time.sleep(1.0)
    if relay.poll() is not None:
        sys.exit("[tunnel] relay failed to start — check port availability")

    print("[tunnel] starting cloudflared Quick Tunnel (free, no account)...")
    cf = subprocess.Popen(
        [_CF, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{args.port}"],
        cwd=_HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    procs.append(cf)

    url = None
    deadline = time.time() + 60
    buf = []
    while url is None and time.time() < deadline and cf.poll() is None:
        line = cf.stdout.readline()
        if not line:
            time.sleep(0.2)
            continue
        buf.append(line)
        sys.stdout.write(f"  {line}")
        sys.stdout.flush()
        m = _URL_RE.search(line)
        if m:
            url = m.group(0)
    if not url:
        _cleanup()

    state.update({"url": url, "token": token, "port": args.port})
    _save_state(state)

    if args.no_deploy:
        _print_manual(url, token)
    else:
        if _sync_vercel(url, token, state):
            state.update({"deployed_url": url, "deployed_token": token})
            _save_state(state)
            print("=" * 74)
            print("HOME RELAY IS LIVE AND DEPLOYED\n")
            print(f"  IG_HOME_RELAY_URL   = {url}")
            print(f"  IG_HOME_RELAY_TOKEN = {token}")
            print(f"  deployed backend    = https://instaiq-api.vercel.app (live fetch via this PC)")
            print("=" * 74 + "\n", flush=True)
        else:
            _print_manual(url, token)

    try:
        cf.wait()
    except KeyboardInterrupt:
        _cleanup()


if __name__ == "__main__":
    main()
