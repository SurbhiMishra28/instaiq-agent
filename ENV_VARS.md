# Environment variables — deployment quick reference

Set these in your hosting dashboard (Render → Environment / Vercel →
Settings → Environment Variables). Never commit real keys to git — the
`.env` files are gitignored on purpose.

## Backend (Render web service)

| Variable | Required | What it does |
|---|---|---|
| `LLM_API_KEY` | recommended | OpenRouter (or any OpenAI-compatible) key — powers all AI-written reports and chat. Free tier available at openrouter.ai/keys. |
| `TAVILY_API_KEY` | recommended | Tavily key (tavily.com, free 1k credits/month). Powers **web-grounded chat answers** AND **keyless competitor discovery** (no Apify credits needed). Verified working: `tvly-dev-...` keys. |
| `APIFY_TOKEN` | optional | Apify actor token. The app works without it (keyless direct Instagram fetch + Tavily discovery). Add only if you prefer Apify as first-choice provider. |
| `LLM_FALLBACK_*` | optional | `LLM_FALLBACK_BASE_URL` / `LLM_FALLBACK_API_KEY` / `LLM_FALLBACK_MODEL` — second provider when the primary's quota dies. |
| `IG_PROXY_URL` | optional | Residential proxy for Instagram fetches from cloud IPs (datacenter IPs get hard-blocked by Instagram; local dev needs nothing). |
| `IG_PW_FETCH` | optional | `true` (default) enables the Playwright session-harvest rung: when Apify tokens are exhausted and plain-HTTP fetches are throttled, ONE randomized-UA headless page load harvests Instagram's own `doc_id` / `lsd` / `csrftoken` from outbound traffic (captured via `page.on("request")`/`response`), after which every profile is fetched at the pure API level (`POST /api/graphql` with the harvested doc_id) — no browser render per profile. On HTTP 400/403/429 or `{"status": "fail"}` the session is invalidated, fresh tokens are auto-re-extracted, and the request is retried once. Set `false` to disable. Needs `pip install -r requirements-playwright.txt && playwright install chromium`. |
| `IG_SESSION_TTL` | optional | Seconds a harvested Instagram API session stays cached (in memory + `backend/ig_session.json`) before re-extraction — default 43200 = 12h; set 21600 for a 6h rotation. Refused sessions are re-extracted immediately regardless of TTL. |
| `IG_PW_HARVEST_TIMEOUT` | optional | Seconds allowed for one harvest page load (default 45). |
| `IG_PW_HARVEST_SEED` | optional | Public handle loaded during harvest (default `instagram`). |
| `IG_PW_SESSION_USES` | optional | Pure-API fetches served per harvested session before a fresh harvest (default 400). |
| `IG_PW_PREFERRED` | optional | `false` (default). When `true`, `get_profile` tries the Playwright pure-API rung FIRST (before Graph/Apify/plain-HTTP) — diagnostic switch for verifying the rung end-to-end; one rung-first attempt per handle per process. |
| `IG_GQL_DOC_ID` | optional | Explicit profile-query `doc_id` for the `POST /api/graphql` rung. The logged-out web app server-renders profiles, so its outbound traffic rarely carries the arbitrary-handle profile query; set this to a captured hash to enable the POST path. Default: the rung uses the harvested session on `web_profile_info`. |
| `COMPETITOR_CANDIDATE_PROVIDER` | optional | Candidate source for profession+location discovery: `pool` (curated + local cache, zero external calls), `websearch` (live search API), or `both` (default). |
| `IG_POOL_SEEDS` | optional | Curated candidate seeds for discovery, e.g. `dermatologist:noida=handle1,handle2;dentist:delhi=handle3`. |

Minimum viable backend env: **`LLM_API_KEY` + `TAVILY_API_KEY`**. Everything
else has working defaults.

## Frontend (Vercel project, root directory `frontend`)

| Variable | Required | What it does |
|---|---|---|
| `VITE_API_URL` | recommended | Full URL of the backend (e.g. `https://instaiq-api.onrender.com`). Leave empty only when frontend and backend share one origin. |

## Security checklist

- [ ] `.env` is NOT in git (`git check-ignore backend/.env` → prints the file)
- [ ] No real keys anywhere in tracked files (only in `.env.example` as empty placeholders)
- [ ] Mark `LLM_API_KEY` / `TAVILY_API_KEY` / `APIFY_TOKEN` as **secret** in hosting dashboards
- [ ] Rotate any key that was ever committed to a public repo
