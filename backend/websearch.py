"""Real-time web search grounding for the AI agent (free-tier APIs).

Primary: Tavily (https://tavily.com) — purpose-built for LLM/RAG agents,
free plan = 1,000 API credits/month, no credit card. Tavily is also the
competitor-discovery engine: `discover_competitors()` finds real, current
niche Instagram competitor handles for any account without spending Apify
credits (the Apify token limit is a thing of the past for this app).
Optional fallback: Serper.dev (Google SERP) — set SERPER_API_KEY instead.

Keys are read at CALL time, not import time, so editing backend/.env takes
effect without a process restart. The chat endpoint uses web search when a
question is about current/evolving information (trends, benchmarks,
"latest", "2026…") or when the local RAG corpus has nothing relevant — so
the agent answers with fresh, cited web results on top of the real fetched
Instagram data. Results are plain dicts; every failure returns [] and never
breaks the chat path.
"""

import os
import re
from typing import Any, Dict, List

import httpx
from dotenv import load_dotenv

# Self-sufficient env loading: works whether or not the importing app has
# already loaded backend/.env (call-time reads below see the same values).
load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))

_TIMEOUT = float(os.getenv("WEBSEARCH_TIMEOUT", "12"))


def _tavily_key() -> str:
    return (os.getenv("TAVILY_API_KEY") or "").strip()


def _serper_key() -> str:
    return (os.getenv("SERPER_API_KEY") or "").strip()


def available() -> bool:
    """True when at least one web-search provider key is configured."""
    return bool(_tavily_key() or _serper_key())


def provider_label() -> str:
    if _tavily_key():
        return "tavily"
    if _serper_key():
        return "serper"
    return "none"


def _tavily_search(
    query: str,
    max_results: int,
    include_domains: List[str] | None = None,
) -> List[Dict[str, Any]]:
    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": _tavily_key(),
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            **({"include_domains": include_domains} if include_domains else {}),
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for r in (resp.json() or {}).get("results") or []:
        out.append({
            "title": str(r.get("title") or "")[:120],
            "url": r.get("url") or "",
            "snippet": " ".join(str(r.get("content") or "").split())[:300],
            "provider": "tavily",
        })
    return out


def _serper_search(query: str, max_results: int) -> List[Dict[str, Any]]:
    resp = httpx.post(
        "https://google.serper.dev/search",
        json={"q": query, "num": max_results},
        headers={"X-API-KEY": _serper_key()},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    out = []
    for r in (resp.json() or {}).get("organic") or []:
        out.append({
            "title": str(r.get("title") or "")[:120],
            "url": r.get("link") or "",
            "snippet": " ".join(str(r.get("snippet") or "").split())[:300],
            "provider": "serper",
        })
    return out


def search_web(query: str, max_results: int = 5) -> List[Dict[str, Any]]:
    """Web results for one query: Tavily first, Serper fallback, [] on any
    failure (quota exhausted, network down, bad key) — callers must treat
    this as best-effort enrichment, never a dependency."""
    query = (query or "").strip()
    if not query or not available():
        return []
    for fn in (_tavily_search, _serper_search):
        try:
            return fn(query, max_results)[:max_results]
        except Exception:
            continue
    return []


# ---------------------------------------------------------------------------
# Tavily-powered competitor discovery (zero Apify credits)
# ---------------------------------------------------------------------------

_IG_HANDLE_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]{2,30})")


def _clean_term(term: str) -> str:
    """Free-text niche term -> clean search phrase ('#skincare!' -> 'skincare')."""
    t = re.sub(r"[#@\u2018\u2019\"'()\[\]{}|.,!?;:]", " ", term or "")
    return " ".join(t.split())[:60]


# Reserved Instagram paths that are NOT profile handles — plus 'popular',
# the one this app's own listicle URLs actually hit in practice.
_RESERVED_PATHS = {
    "p", "reel", "reels", "explore", "tv", "stories", "popular", "accounts",
    "about", "legal", "developer", "directory", "web", "s", "topics",
}


def _handle_from_url(url: str) -> str:
    m = _IG_HANDLE_RE.search(url or "")
    return m.group(1).lower().rstrip(".") if m else ""


def discover_competitors(
    username: str,
    niche_terms: List[str],
    limit: int = 20,
) -> List[Dict[str, Any]]:
    """Real competitor candidates for `username`, discovered via Tavily.

    Two complementary probes per niche term:
      1. An Instagram-scoped search (include_domains=['instagram.com']) whose
         result URLs ARE profile links — the most reliable handle source.
      2. Organic 'top <niche> instagram accounts' listicles — handles mined
         from any instagram.com links inside their titles/snippets.

    The target handle itself, reserved Instagram paths and duplicates are
    filtered out. Returns rows shaped like scraper's candidate rows:

        {username, full_name, bio, followers, verified, private, source}

    `followers` is always 0 here (discovery signal only — the fetch layer
    gets real numbers when a candidate is actually fetched and analyzed).
    Every failure returns [] — discovery must never break a request. With
    no Tavily/Serper key configured this returns [] immediately.
    """
    handle = (username or "").strip().lstrip("@").lower()
    terms = [_clean_term(t) for t in (niche_terms or []) if _clean_term(t)]
    if not handle or not terms or not available():
        return []

    base = terms[0]
    found: List[Dict[str, Any]] = []
    seen = {handle} | _RESERVED_PATHS

    def _consider(raw: str) -> bool:
        cand = (raw or "").lower().rstrip("./")
        if not cand or cand in seen or len(cand) < 2:
            return False
        seen.add(cand)
        found.append({
            "username": cand,
            "full_name": "",
            "bio": f"Discovered via web search for '{base}' niche",
            "followers": 0,
            "verified": False,
            "private": False,
            "source": "websearch",
        })
        return len(found) >= limit

    probes: List[Dict[str, Any]] = [
        {"q": f"{base} instagram profile", "domains": ["instagram.com"]},
        {"q": f"top {base} instagram accounts to follow"},
    ]
    if len(terms) > 1:
        probes.append({"q": f"best {terms[1]} instagram influencers"})

    for probe in probes[:3]:
        try:
            if probe.get("domains") and _tavily_key():
                results = _tavily_search(probe["q"], 8, probe["domains"])
            else:
                results = search_web(probe["q"], 6)
        except Exception:
            results = []
        for r in results:
            # URLs first (scoped probe) — most reliable.
            h = _handle_from_url(r.get("url", ""))
            if h and h not in _RESERVED_PATHS and _consider(h):
                return found
            blob = f"{r.get('title', '')} {r.get('snippet', '')}"
            for m in _IG_HANDLE_RE.finditer(blob):
                if _consider(m.group(1)):
                    return found
    return found
