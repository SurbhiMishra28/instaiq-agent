"""Real E2E check for the Playwright harvest + pure-API fetch rung.

Run from backend/:  ./venv/Scripts/python.exe _pw_e2e_check.py
Requires: pip install -r requirements-playwright.txt && playwright install chromium

Steps:
  1. Harvest a live session from instagram.com with a randomized UA
     (doc_id / lsd / csrftoken captured from the page's own traffic).
  2. Fetch a real profile purely at the API level with that session.
  3. Show the persisted session file + timing.
"""
import asyncio
import os
import time

import scraper


async def main() -> int:
    t0 = time.monotonic()

    print("== 1. session harvest (randomized UA, real page load) ==")
    ok = await asyncio.to_thread(scraper._pw_ensure_session)
    print(f"   harvest ok: {ok}  ({time.monotonic() - t0:.1f}s)")
    print(f"   session active: {scraper._pw_session_active()}")
    print(f"   doc_id: {scraper._pw_session.get('doc_id') or '(none captured)'}")
    print(f"   lsd present: {bool(scraper._pw_session.get('lsd'))}")
    print(f"   randomized UA: {scraper._pw_session.get('ua')}")
    print(f"   cookies captured: {len(scraper._pw_session.get('cookies') or [])}")
    if not ok:
        print("   -> harvest failed; pure-API rung will be skipped by the ladder")
        return 1

    print("== 2. pure-API profile fetch (no browser render) ==")
    t1 = time.monotonic()
    profile = await asyncio.to_thread(scraper._fetch_pw_api_profile, "natgeo")
    dt = time.monotonic() - t1
    if profile is None:
        print("   -> returned None (Instagram refused the harvested session)")
        return 1
    print(f"   handle: @{profile.username}  ({dt:.1f}s)")
    print(f"   followers: {profile.followers:,}")
    print(f"   posts captured: {len(profile.recent_posts)}")
    if profile.recent_posts:
        p0 = profile.recent_posts[0]
        print(f"   newest post likes/comments: {p0.likes:,} / {p0.comments:,}")
    print(f"   verified: {profile.is_verified} | category: {profile.category}")

    print("== 3. persisted session ==")
    print(f"   file exists: {os.path.isfile(scraper._PW_HARVEST_FILE)}")

    print(f"\nPW E2E CHECK PASSED ({time.monotonic() - t0:.1f}s total)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
