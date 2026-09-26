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
