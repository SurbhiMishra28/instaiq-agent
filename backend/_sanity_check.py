"""Offline sanity checks for the REAL-ONLY data layer (no network, no keys).

All data in the system is real; these tests verify the provider wiring and
that failures surface honestly instead of ever serving simulated rows.
"""
import asyncio
import os
import time

for _k in ("APIFY_TOKEN", "APIFY_TOKENS", *[(f"APIFY_TOKEN_{i}") for i in range(2, 10)], "RAPIDAPI_KEY", "IG_ACCESS_TOKEN", "IG_BUSINESS_ID"):
    os.environ.pop(_k, None)

import scraper  # noqa: E402
from models import Post, ProfileData  # noqa: E402


def test_normalize():
    cases = {
        "cristiano": "cristiano",
        " @Cristiano ": "cristiano",
        "https://www.instagram.com/cristiano/": "cristiano",
        "instagram.com/nike?hl=en": "nike",
        "https://www.instagram.com/glow.beauty.co/": "glow.beauty.co",
    }
    for raw, expected in cases.items():
        got = scraper.normalize_username(raw)
        assert got == expected, f"normalize({raw!r}) = {got!r}, want {expected!r}"

    for bad in ["", "instagram.com/explore/", "instagram.com/p/Cxyz/", "has space!"]:
        try:
            scraper.normalize_username(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"normalize({bad!r}) should have raised ValueError")
    print("normalize_username: OK")


def _make_profile(username: str, followers: int = 1000) -> ProfileData:
    return ProfileData(
        username=username,
        full_name=username.replace("_", " ").title(),
        bio=f"{username} bio",
        followers=followers,
        following=100,
        posts_count=3,
        is_verified=False,
        is_business=False,
        category="Tech",
        recent_posts=[
            Post(id=f"{username}_{i}", caption=f"post {i} @friend{i} #tech",
                 likes=100 + i, comments=5, posted_days_ago=i + 1,
                 hashtags=["#tech"], media_type="image")
            for i in range(3)
        ],
    )


def test_cache_roundtrip():
    p = _make_profile("cacheuser")
    scraper._disk_profile_set(p.username, p)
    got = scraper._disk_profile_get(p.username)
    assert got is not None and got.username == "cacheuser"
    assert got.followers == 1000
    assert got.recent_posts[0].likes == 100
    # Fresh lookup (inside TTL) must NOT be stamped as stale.
    assert got.data_age_hours is None
    print("disk cache roundtrip: OK")


async def test_get_profile_cache_first():
    scraper._profile_cache.clear()
    p = _make_profile("cacheduser")
    scraper._disk_profile_set(p.username, p)
    got = await scraper.get_profile("CachedUser")
    assert got.username == "cacheduser"
    print("get_profile serves real cached data: OK")


async def test_batch_mixed():
    """Cached handles are served; unknown handles simply stay missing —
    the caller surfaces them as errors/warnings. No simulated rows are
    invented. (Providers are stubbed so the test stays offline.)"""
    scraper._profile_cache.clear()
    known = _make_profile("batchknown")
    scraper._disk_profile_set("batchknown", known)

    async def not_found_direct(username):
        raise ValueError(f"Instagram profile '@{username}' not found (stubbed)")

    real_direct = scraper._fetch_direct_profile
    scraper._fetch_direct_profile = not_found_direct
    try:
        res = await scraper.get_profiles_batch(["batchknown", "batchunknown_99"])
    finally:
        scraper._fetch_direct_profile = real_direct
        scraper._profile_cache.clear()
    assert res["batchknown"].username == "batchknown"
    assert "batchunknown_99" not in res, "unknown handles must not get invented rows"
    print("batch fetch (cached real; unknown stays honestly missing): OK")


def test_local_discovery():
    import sqlite3
    # Seed cache with a target and two same-niche accounts that mention each other.
    # NOTE: write via _disk_profile_set FIRST (it manages its own connections),
    # then refresh timestamps in a separate short-lived connection. Holding an
    # open write transaction across the _disk_profile_set calls would lock the
    # DB and silently drop the seed rows (writes are best-effort by design).
    for uname in ("disco_main", "disco_rival1", "disco_rival2"):
        p = _make_profile(uname, followers=5000)
        p.recent_posts[0].caption = "collab with @disco_rival1 #tech"
        scraper._disk_profile_set(uname, p)
    conn = sqlite3.connect(scraper._CACHE_DB)
    now = time.time()
    for uname in ("disco_main", "disco_rival1", "disco_rival2"):
        conn.execute("UPDATE cache SET cached_at = ? WHERE key = ?", (now, f"profile:{uname}"))
    conn.commit()
    conn.close()

    # Large limit: the cache may hold many real rows from live use, and the
    # assertion is about PRESENCE of the seeded mention/overlap rivals, not
    # their rank against unrelated cached accounts.
    rows = scraper._local_discover_sync("disco_main", 50)
    names = [r["username"] for r in rows]
    assert "disco_rival1" in names and "disco_rival2" in names, names
    print("local discovery mines cached mentions: OK")


async def test_live_mode_failover_to_direct_provider():
    """Provider wiring: no Apify tokens must call the keyless direct provider
    (provider #2) — not silently serve fake data."""
    scraper._profile_cache.clear()
    _purge_handle("directonlyhandle")

    old = list(scraper.APIFY_TOKENS)
    scraper.APIFY_TOKENS = []
    try:
        calls = {"direct": 0}

        async def fake_direct(username):
            calls["direct"] += 1
            return _make_profile(username, followers=4242)

        real_direct = scraper._fetch_direct_profile
        scraper._fetch_direct_profile = fake_direct
        got = await scraper.get_profile("DirectOnlyHandle")
        assert calls["direct"] == 1
        assert got.username == "directonlyhandle" and got.followers == 4242
        print("no-token -> keyless direct provider: OK")
    finally:
        scraper._fetch_direct_profile = real_direct
        scraper.APIFY_TOKENS = old
        scraper._profile_cache.clear()
        _purge_handle("directonlyhandle")


async def test_live_mode_pool_exhausted_falls_over_to_direct():
    """Latency policy (direct-first): tokens EXIST but the direct path
    succeeds -> get_profile must serve from direct and never pay for an
    Apify actor run. The reverse case (direct failing -> Apify serves) is
    covered right below."""
    scraper._profile_cache.clear()
    _purge_handle("exhaustedhandle")

    old = list(scraper.APIFY_TOKENS)
    scraper.APIFY_TOKENS = ["fake-exhausted-token"]
    try:
        calls = {"apify": 0, "direct": 0}

        async def fake_apify(username):
            calls["apify"] += 1
            return _make_profile(username, followers=999)

        async def fake_direct(username):
            calls["direct"] += 1
            return _make_profile(username, followers=777)

        real_apify, real_direct = scraper._fetch_live_profile, scraper._fetch_direct_profile
        scraper._fetch_live_profile = fake_apify
        scraper._fetch_direct_profile = fake_direct
        scraper._api_block_until = 0.0
        got = await scraper.get_profile("exhaustedhandle")
        assert calls["direct"] == 1 and calls["apify"] == 0, calls
        assert got.followers == 777
        print("direct-first fast path (Apify untouched): OK")
    finally:
        scraper._fetch_live_profile, scraper._fetch_direct_profile = real_apify, real_direct
        scraper.APIFY_TOKENS = old
        scraper._profile_cache.clear()
        _purge_handle("exhaustedhandle")


async def test_direct_failure_falls_over_to_apify():
    """Direct-first policy, failure side: the keyless direct provider failing
    (blocked/errored) must fall through to the Apify pool — never dead-end."""
    scraper._profile_cache.clear()
    _purge_handle("directdownhandle")

    old = list(scraper.APIFY_TOKENS)
    scraper.APIFY_TOKENS = ["fake-ok-token"]
    try:
        calls = {"apify": 0, "direct": 0}

        async def fake_apify(username):
            calls["apify"] += 1
            return _make_profile(username, followers=999)

        async def fake_direct(username):
            calls["direct"] += 1
            raise RuntimeError("Instagram rate-limiting this IP (simulated)")

        real_apify, real_direct = scraper._fetch_live_profile, scraper._fetch_direct_profile
        scraper._fetch_live_profile = fake_apify
        scraper._fetch_direct_profile = fake_direct
        scraper._api_block_until = 0.0
        got = await scraper.get_profile("directdownhandle")
        assert calls["direct"] == 1 and calls["apify"] == 1, calls
        assert got.followers == 999
        print("direct failure -> Apify fallback: OK")
    finally:
        scraper._fetch_live_profile, scraper._fetch_direct_profile = real_apify, real_direct
        scraper.APIFY_TOKENS = old
        scraper._profile_cache.clear()
        _purge_handle("directdownhandle")
        scraper._api_block_until = 0.0


async def test_live_mode_honest_error_when_all_providers_fail():
    """All providers failing must raise — NEVER fall back to a simulated row
    (the bug that showed fake numbers for real handles)."""
    scraper._profile_cache.clear()
    _purge_handle("bothfailhandle")

    old = list(scraper.APIFY_TOKENS)
    scraper.APIFY_TOKENS = []
    try:
        async def failing_direct(username):
            raise RuntimeError("Instagram rate-limiting this IP (simulated)")

        async def failing_direct(username):
            raise RuntimeError("Keyless HTTP fetch failed (simulated)")

        scraper._fetch_direct_profile = failing_direct
        scraper._fetch_direct_profile = failing_direct
        try:
            await scraper.get_profile("bothfailhandle")
        except RuntimeError:
            print("all-providers-failed -> honest RuntimeError: OK")
        else:
            raise AssertionError("expected RuntimeError, got a (fake) profile instead")
    finally:
        scraper._fetch_direct_profile = _restore_direct
        scraper._fetch_direct_profile = failing_direct  # stays failing (provider removed)
        scraper.APIFY_TOKENS = old
        scraper._profile_cache.clear()
        _purge_handle("bothfailhandle")


def _purge_handle(handle: str) -> None:
    import sqlite3
    try:
        conn = sqlite3.connect(scraper._CACHE_DB)
        conn.execute("DELETE FROM cache WHERE key LIKE ?", (f"profile:{handle}%",))
        conn.execute("DELETE FROM cache WHERE key LIKE ?", (f"related:{handle}%",))
        conn.commit()
        conn.close()
    except Exception:
        pass


def _restore_direct(username: str):
    raise RuntimeError("direct provider not stubbed in this test")




def test_no_demo_machinery():
    """The demo/simulated subsystem must not exist anymore."""
    assert not hasattr(scraper, "generate_demo_profile"), "demo generator must be removed"
    assert not hasattr(scraper, "_demo_related"), "demo competitor generator must be removed"
    assert not hasattr(scraper, "is_demo_row"), "demo-row badge helper must be removed"
    src = open(scraper.__file__, encoding="utf-8").read()
    assert "FALLBACK_TO_DEMO" not in src, "demo fallback flag must be removed"
    assert "DATA_MODE" not in src, "data-mode switch must be removed (live is the only mode)"
    print("no demo/simulated machinery in scraper: OK")


def test_pw_doc_id_extraction():
    """doc_id extraction from real-shaped GraphQL request bodies."""
    cases = {
        'av=0&doc_id=23996118476800820&lsd=ABC123': "23996118476800820",
        'variables={"username":"gymshark"}&doc_id=9510064595728286': "9510064595728286",
        '{"doc_id":"17911194528752944","variables":"{"username":"x"}"}': "17911194528752944",
        'doc_id=12345': "",  # too short -> not a persisted-query hash
        'no doc id here': "",
        '': "",
    }
    for body, expected in cases.items():
        got = scraper._extract_doc_id(body)
        assert got == expected, f"_extract_doc_id({body!r}) = {got!r}, want {expected!r}"
    print("doc_id extraction: OK")


def test_pw_session_roundtrip():
    """Harvested-session storage -> disk persistence -> reload, and that an
    empty session is honestly inactive."""
    old_file = scraper._PW_HARVEST_FILE
    try:
        scraper._PW_HARVEST_FILE = old_file + ".sanity_tmp"
        cookies = [{"name": "csrftoken", "value": "testcsrf123", "domain": ".instagram.com", "path": "/"}]
        scraper._store_pw_session(cookies, "test-lsd-token", "23996118476800820", "TestUA/1.0")
        assert scraper._pw_session_active(), "stored session must be active"
        # Disk reload path: wipe memory, load from file.
        scraper._pw_session.update({"cookies": None, "lsd": "", "doc_id": "", "ua": "", "ts": 0.0})
        scraper._load_pw_session()
        assert scraper._pw_session_active(), "session must reload from disk"
        assert scraper._pw_session["doc_id"] == "23996118476800820"
        assert scraper._pw_session["ua"] == "TestUA/1.0"
        # httpx cookie jar conversion.
        jar = scraper._pw_cookies_to_httpx()
        assert jar is not None and jar.get("csrftoken") == "testcsrf123"
        # UA randomizer returns realistic pairs.
        for _ in range(6):
            ua, plat = scraper._pw_random_ua()
            assert ua.startswith("Mozilla/5.0") and plat in ("Windows", "Mac OS X", "Linux")
    finally:
        scraper._pw_session.update({"cookies": None, "lsd": "", "doc_id": "", "ua": "", "ts": 0.0})
        try:
            import os as _os
            _os.remove(scraper._PW_HARVEST_FILE)
        except OSError:
            pass
        scraper._PW_HARVEST_FILE = old_file
    print("harvested-session roundtrip (store/persist/reload/jar): OK")


def test_pw_fetch_disabled_or_sessionless_is_safe():
    """The pure-API rung must be a no-op (return None) when disabled or when
    no harvested session exists — never raise, never fabricate."""
    old = scraper.PW_FETCH_ENABLED
    try:
        scraper.PW_FETCH_ENABLED = False
        assert scraper._fetch_pw_api_profile("anyhandle") is None
        scraper.PW_FETCH_ENABLED = True
        scraper._pw_session.update({"cookies": None, "lsd": "", "doc_id": "", "ua": "", "ts": 0.0})
        assert scraper._fetch_pw_api_profile("anyhandle") is None
    finally:
        scraper.PW_FETCH_ENABLED = old
    print("pure-API rung safe when disabled/sessionless: OK")


def test_profession_pipeline_offline():
    """The 4-step profession+location pipeline on cached real data only:
    bio keyword fallback understands the account, curated-pool candidates
    flow through the REAL fetch/verify ladder, scoring + match_reason are
    attached, and the input handle is excluded. Zero external calls."""
    async def run():
        # Seed the disk cache with a Noida dermatologist + two rivals.
        scraper._profile_cache.clear()
        main_p = _make_profile("noida_skin_clinic", followers=8000)
        main_p.full_name = "Noida Skin Clinic"
        main_p.bio = "Dermatologist in Noida, Uttar Pradesh. Skin & hair treatments. Book a consultation."
        scraper._disk_profile_set("noida_skin_clinic", main_p)

        r1 = _make_profile("dr_skin_noida", followers=15000)
        r1.full_name = "Dr Skin — Dermatologist Noida"
        r1.bio = "Dermatologist, Noida. Acne, hairfall, laser treatments."
        r1.recent_posts[0].posted_days_ago = 2
        scraper._disk_profile_set("dr_skin_noida", r1)

        r2 = _make_profile("city_gym_noida", followers=9000)
        r2.full_name = "City Gym Noida"
        r2.bio = "Best gym in Noida. Personal training."
        scraper._disk_profile_set("city_gym_noida", r2)

        rows, meta = await scraper.discover_profession_competitors("noida_skin_clinic", limit=5)
        return rows, meta, r1, r2

    old_provider = scraper._CANDIDATE_PROVIDER
    try:
        scraper._CANDIDATE_PROVIDER = "pool"  # offline provider only
        rows, meta, r1, r2 = asyncio.run(run())
    finally:
        scraper._CANDIDATE_PROVIDER = old_provider

    # Either the LLM or the bio-keyword fallback may have run — both must
    # detect dermatologist + Noida from the seeded bio text.
    assert meta["understanding_source"] in ("llm", "keywords"), meta
    assert (meta.get("profession") or "").lower() == "dermatologist", meta
    assert (meta.get("city") or "").lower() == "noida", meta
    assert meta.get("location_detected") is True
    names = {r["username"] for r in rows}
    assert "noida_skin_clinic" not in names, "input handle must be excluded"
    # Both candidates flow through verification; the gym may be dropped by
    # the profession check — but a matched dermatologist must survive.
    assert "dr_skin_noida" in names, f"matched rival missing: {names}"
    for r in rows:
        assert isinstance(r.get("match_score"), int) and 0 <= r["match_score"] <= 100
        assert r.get("match_reason")
        assert r.get("city") in ("Noida", None)
    # Ranking among the SEEDED rows: the dermatologist (profession+city
    # match) must outrank the gym (wrong profession) — and the gym must be
    # gone entirely if the profession gate dropped it. The cache may also
    # hold real accounts from live use; if such a row outranks the seed it
    # must itself be a viable, verified row (the gate below guarantees it).
    seeded = {r["username"]: r for r in rows if r["username"] in {"dr_skin_noida", "city_gym_noida"}}
    assert "dr_skin_noida" in seeded
    if "city_gym_noida" in seeded:
        assert seeded["dr_skin_noida"]["match_score"] > seeded["city_gym_noida"]["match_score"]
    # Viability gate: every returned row is researchable (audience + content).
    for r in rows:
        assert r["followers"] >= 100, r
    print("profession+location pipeline (offline, real data): OK")


def test_pw_status_is_fail():
    """Instagram's app-level refusal shapes are recognized (HTTP 200 but
    status fail), and success shapes are not flagged."""
    assert scraper._pw_status_is_fail({"status": "fail", "message": "please wait"})
    assert scraper._pw_status_is_fail({"meta": {"status": "fail"}})
    assert not scraper._pw_status_is_fail({"status": "ok", "data": {"user": {}}})
    assert not scraper._pw_status_is_fail(None)
    assert not scraper._pw_status_is_fail("nonsense")
    print("status-fail interception shapes: OK")


def test_pw_auto_reextract_on_refusal():
    """400/403/status-fail interception: the cached session is invalidated,
    a fresh session is auto-extracted, and the SAME request is retried —
    total exactly 2 attempts (1 + 1 retry). All with a mocked HTTP layer."""
    old_file = scraper._PW_HARVEST_FILE
    real_ensure = scraper._pw_ensure_session
    real_post = scraper._pw_graphql_post
    try:
        scraper._PW_HARVEST_FILE = old_file + ".sanity_tmp"
        calls = {"n": 0}

        def fake_post(doc_id, username, headers, cookies, lsd, raw_body=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return 403, None  # cached session refused by Instagram
            return 200, {  # fresh tokens work
                "data": {"user": {
                    "username": "retryuser",
                    "follower_count": 4321,
                    "edge_owner_to_timeline_media": {"edges": []},
                }},
                "status": "ok",
            }

        def fake_ensure(force=False):
            # Simulates a successful headless re-extraction (with template).
            scraper._store_pw_session(
                [{"name": "csrftoken", "value": "fresh", "domain": ".instagram.com", "path": "/"}],
                "fresh-lsd", "23996118476800820", "FreshUA/2.0",
                template={"headers": {"user-agent": "FreshUA/2.0"},
                          "body": "av=0&doc_id=23996118476800820"
                                  "&variables=%7B%22username%22%3A%22seed%22%7D"},
            )
            return True

        scraper._pw_graphql_post = fake_post
        scraper._pw_ensure_session = fake_ensure
        # Seed the STALE session that Instagram will refuse. The template is
        # required so the GraphQL POST path (the mocked call) is exercised.
        scraper._store_pw_session(
            [{"name": "csrftoken", "value": "stale", "domain": ".instagram.com", "path": "/"}],
            "stale-lsd", "23996118476800820", "StaleUA/1.0",
            template={"headers": {"user-agent": "StaleUA/1.0"},
                      "body": "av=0&doc_id=23996118476800820"
                              "&variables=%7B%22username%22%3A%22seed%22%7D"},
        )
        p = scraper._fetch_pw_api_profile("retryuser")
        assert p is not None, "retry with fresh tokens must succeed"
        assert p.username == "retryuser" and p.followers == 4321
        assert calls["n"] == 2, f"expected exactly 2 attempts (refuse + retry), got {calls['n']}"
    finally:
        scraper._pw_graphql_post = real_post
        scraper._pw_ensure_session = real_ensure
        scraper._pw_session.update({"cookies": None, "lsd": "", "doc_id": "", "ua": "", "ts": 0.0})
        try:
            import os as _os
            _os.remove(scraper._PW_HARVEST_FILE)
        except OSError:
            pass
        scraper._PW_HARVEST_FILE = old_file
    print("auto re-extract + retry on 403 refusal: OK")


def _cleanup_test_rows():
    """Remove test rows so the real-data cache stays tidy."""
    import sqlite3
    conn = sqlite3.connect(scraper._CACHE_DB)
    for prefix in ("cacheuser", "cacheduser", "batchknown", "batchunknown",
                   "disco_main", "disco_rival1", "disco_rival2",
                   "directonlyhandle", "exhaustedhandle", "directdownhandle", "bothfailhandle"):
        conn.execute("DELETE FROM cache WHERE key LIKE ?", (f"profile:{prefix}%",))
        conn.execute("DELETE FROM cache WHERE key LIKE ?", (f"related:{prefix}%",))
    conn.commit()
    conn.close()


if __name__ == "__main__":
    test_normalize()
    test_cache_roundtrip()
    asyncio.run(test_get_profile_cache_first())
    asyncio.run(test_batch_mixed())
    asyncio.run(test_live_mode_failover_to_direct_provider())
    asyncio.run(test_live_mode_pool_exhausted_falls_over_to_direct())
    asyncio.run(test_direct_failure_falls_over_to_apify())
    asyncio.run(test_live_mode_honest_error_when_all_providers_fail())
    test_local_discovery()
    test_no_demo_machinery()
    test_pw_doc_id_extraction()
    test_pw_session_roundtrip()
    test_pw_fetch_disabled_or_sessionless_is_safe()
    test_pw_status_is_fail()
    test_pw_auto_reextract_on_refusal()
    test_profession_pipeline_offline()
    _cleanup_test_rows()
    print("\nAll offline checks passed.")
