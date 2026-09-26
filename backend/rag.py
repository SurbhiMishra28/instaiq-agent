"""Retrieval-Augmented Generation (RAG) over the agent's REAL data.

Every real profile the fetchers return (via /api/analyze or the chat's live
grounding) is chunked into a local SQLite index: one account-level chunk
(stats + AI summary) and one chunk per stored post (caption + engagement).
Chat queries retrieve the most relevant chunks (SQLite FTS5, with a LIKE
fallback) and the LLM must answer FROM those chunks — so answers quote real
fetched numbers instead of generic advice, and questions like "what did we
learn about X last week?" work across the whole corpus.

Design notes:
- stdlib-only (sqlite3 + FTS5, json, re). No embeddings service, no API keys
  — lexical BM25 ranking is sufficient at this corpus size and keeps the
  free-tier promise. FTS5 ships with CPython's sqlite3 on Windows/Linux.
- Indexing is synchronous best-effort and never raises upward: retrieval is
  an enhancement, its failure must not break chat.
- All indexed content originates from REAL fetched data (the system refuses
  simulated data), so the retrieval corpus never poisons the LLM.
"""

import json
import os
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional

_DB_PATH = os.getenv(
    "RAG_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "rag_index.db")
)
MAX_CHUNKS_PER_QUERY = int(os.getenv("RAG_TOP_K", "8"))

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema() -> None:
    try:
        with _connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS chunks (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       kind TEXT NOT NULL,          -- 'account' | 'post'
                       username TEXT NOT NULL,      -- instagram handle
                       chunk_key TEXT NOT NULL,     -- unique per (username, kind, post)
                       title TEXT,
                       body TEXT NOT NULL,
                       meta TEXT,                   -- JSON blob (metrics, urls)
                       ts REAL NOT NULL,            -- indexing time
                       UNIQUE(chunk_key)
                   )"""
            )
            # FTS5 virtual table kept in sync by triggers (tokenizer tolerant
            # of handles/captions; unicode61 folds case, splits on punct).
            conn.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                       title, body, meta, content='chunks', content_rowid='id',
                       tokenize='unicode61 remove_diacritics 1'
                   )"""
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN\n"
                "  INSERT INTO chunks_fts(rowid, title, body, meta) VALUES (new.id, new.title, new.body, new.meta);\n"
                "END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN\n"
                "  INSERT INTO chunks_fts(chunks_fts, rowid, title, body, meta) VALUES ('delete', old.id, old.title, old.body, old.meta);\n"
                "END"
            )
            conn.execute(
                "CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN\n"
                "  INSERT INTO chunks_fts(chunks_fts, rowid, title, body, meta) VALUES ('delete', old.id, old.title, old.body, old.meta);\n"
                "  INSERT INTO chunks_fts(rowid, title, body, meta) VALUES (new.id, new.title, new.body, new.meta);\n"
                "END"
            )
    except sqlite3.Error:
        # e.g. FTS5 unavailable in a stripped Python build — rag functions
        # degrade to LIKE search below rather than crash the app.
        pass


_ensure_schema()


# ---------------------------------------------------------------------------
# Indexing (called on every real fetch)
# ---------------------------------------------------------------------------

def _chunk_account(username: str, profile: Any, metrics: Any = None,
                   summary: str = "") -> Dict[str, Any]:
    parts = [
        f"Account @{username}",
        f"Followers {profile.followers:,}, following {profile.following:,}, "
        f"{profile.posts_count:,} posts",
    ]
    if profile.is_verified:
        parts.append("verified")
    if profile.category:
        parts.append(f"category {profile.category}")
    if profile.bio:
        parts.append(f"bio: {profile.bio}")
    if metrics is not None:
        parts.append(
            f"engagement rate {metrics.engagement_rate}%, "
            f"avg likes/post {metrics.avg_likes:,.0f}, "
            f"avg comments/post {metrics.avg_comments:,.1f}, "
            f"posting {metrics.posting_frequency_per_week}/week"
        )
        if getattr(metrics, "best_content_type", None):
            parts.append(f"best format {metrics.best_content_type}")
        if getattr(metrics, "top_hashtags", None):
            parts.append("top hashtags " + ", ".join(metrics.top_hashtags[:8]))
    if summary:
        parts.append(f"analysis: {summary}")
    return {
        "kind": "account",
        "username": username,
        "chunk_key": f"account:{username}",
        "title": f"@{username} — account",
        "body": ". ".join(parts),
        "meta": json.dumps({"followers": profile.followers,
                            "engagement_rate": getattr(metrics, "engagement_rate", None),
                            "verified": bool(profile.is_verified)}),
    }


def _chunk_post(username: str, post: Any) -> Dict[str, Any]:
    caption = " ".join((post.caption or "(no caption)").split())[:400]
    eng = f"{post.likes:,} likes, {post.comments:,} comments"
    if getattr(post, "views", None):
        eng += f", {post.views:,} views"
    when = getattr(post, "posted_at", None) or getattr(post, "posted_days_ago", None)
    title = f"@{username} post"
    return {
        "kind": "post",
        "username": username,
        "chunk_key": f"post:{username}:{getattr(post, 'id', None) or getattr(post, 'shortcode', None) or caption[:40]}",
        "title": title,
        "body": f"{title} ({when if when is not None else 'date n/a'}): {caption} — {eng}",
        "meta": json.dumps({"likes": post.likes, "comments": post.comments,
                            "media_type": getattr(post, "media_type", None),
                            "url": getattr(post, "url", None)}),
    }


def index_profile(username: str, profile: Any, metrics: Any = None,
                  summary: str = "") -> int:
    """Chunk + upsert one real profile (account chunk + one chunk per post).
    Returns the number of chunks written. Never raises."""
    try:
        if profile is None or not getattr(profile, "username", username):
            return 0
        chunks = [_chunk_account(username, profile, metrics, summary)]
        for p in (getattr(profile, "recent_posts", None) or [])[:12]:
            try:
                chunks.append(_chunk_post(username, p))
            except Exception:
                continue
        now = time.time()
        with _connect() as conn:
            for c in chunks:
                conn.execute(
                    "INSERT INTO chunks (kind, username, chunk_key, title, body, meta, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(chunk_key) DO UPDATE SET "
                    "title=excluded.title, body=excluded.body, meta=excluded.meta, ts=excluded.ts",
                    (c["kind"], c["username"], c["chunk_key"], c["title"], c["body"], c["meta"], now),
                )
        return len(chunks)
    except Exception:
        return 0


def index_summary(username: str, summary: str, extra: Optional[Dict[str, Any]] = None) -> int:
    """Merge an AI/rule-based summary into the account chunk (no-op when the
    account isn't indexed yet — index_profile will add it on the next fetch)."""
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, body FROM chunks WHERE chunk_key = ?", (f"account:{username}",)
            ).fetchone()
            if not row:
                return 0
            body = row[1] if f"analysis: {summary[:60]}" in row[1] else f"{row[1]}. analysis: {summary}"
            conn.execute(
                "UPDATE chunks SET body = ?, ts = ? WHERE id = ?",
                (body[:4000], time.time(), row[0]),
            )
        return 1
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")


def _fts_query(q: str) -> str:
    """Turn a natural-language question into a safe FTS5 MATCH expression.
    Quoting each token avoids FTS5 syntax errors on odd input."""
    words = _WORD_RE.findall(q or "")[:24]
    return " OR ".join(f'"{w}"' for w in words)


def retrieve(query: str, username: Optional[str] = None, k: int = MAX_CHUNKS_PER_QUERY) -> List[Dict[str, Any]]:
    """Top-k relevant chunks: FTS5 BM25 ranking, LIKE fallback, account-chunk
    boost, recency tie-break. Returns [] on any failure (never raises)."""
    if not (query or "").strip():
        return []
    try:
        with _connect() as conn:
            results: List[Dict[str, Any]] = []
            match = _fts_query(query)
            try:
                rows = conn.execute(
                    "SELECT c.kind, c.username, c.title, c.body, c.meta, "
                    "       bm25(chunks_fts) AS rank "
                    "FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
                    "WHERE chunks_fts MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (match, k * 3),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            if not rows:
                like = f"%{query.strip()[:80]}%"
                rows = conn.execute(
                    "SELECT kind, username, title, body, meta, 0 AS rank "
                    "FROM chunks WHERE body LIKE ? ORDER BY ts DESC LIMIT ?",
                    (like, k * 3),
                ).fetchall()
            for kind, uname, title, body, meta, rank in rows:
                results.append({
                    "kind": kind, "username": uname, "title": title,
                    "body": body, "meta": json.loads(meta) if meta else {},
                    "score": float(rank) if isinstance(rank, (int, float)) else 0.0,
                })
            # Prefer the asked-about account's chunks + account-level chunks.
            want = (username or "").strip().lstrip("@").lower()
            results.sort(key=lambda r: (
                0 if (want and r["username"] == want) else 1,
                0 if r["kind"] == "account" else 1,
                r["score"],
            ))
            # Keep at most ~half the budget per account so one hot account
            # doesn't crowd out everything else.
            per_cap = max(2, k // 2)
            counts: Dict[str, int] = {}
            picked: List[Dict[str, Any] ] = []
            for r in results:
                c = counts.get(r["username"], 0)
                if c >= per_cap and r["username"] != want:
                    continue
                counts[r["username"]] = c + 1
                picked.append(r)
                if len(picked) >= k:
                    break
            return picked
    except Exception:
        return []


def stats() -> Dict[str, Any]:
    """Corpus snapshot for /api/diagnostics: chunk counts by kind/account."""
    try:
        with _connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            accounts = conn.execute(
                "SELECT COUNT(DISTINCT username) FROM chunks"
            ).fetchone()[0]
            kinds = conn.execute(
                "SELECT kind, COUNT(*) FROM chunks GROUP BY kind"
            ).fetchall()
        return {"chunks": total, "accounts": accounts,
                "by_kind": {k: n for k, n in kinds}}
    except Exception:
        return {"chunks": 0, "accounts": 0, "by_kind": {}}


def forget(username: str) -> int:
    """Drop one account's chunks (used when a handle's data is purged)."""
    try:
        with _connect() as conn:
            cur = conn.execute("DELETE FROM chunks WHERE username = ?", (username,))
            return cur.rowcount or 0
    except Exception:
        return 0
