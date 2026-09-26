// InstaIQ keyless Instagram relay v3 (free Cloudflare Worker).
//
// Instagram hard-blocks datacenter IPs (Vercel/Render get 401/429 on every
// call). Cloudflare Workers run on Cloudflare's edge, which Instagram serves
// logged-out pages to. This relay offers TWO modes:
//
//   ?url=https://www.instagram.com/<handle>/            -> raw HTML (v2 behavior)
//   ?api=1&username=<handle>                            -> web_profile_info JSON
//                                                          with full browser
//                                                          headers (v3, exact
//                                                          stats + posts)
//
// Anti-throttle: cookie bootstrap, retry with backoff, edge caching. Reports
// Instagram's true status via x-relay-status either way. No API token anywhere.

const ALLOWED_HOST = "www.instagram.com";
const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36";
const APP_ID = "936619743392459";
const EDGE_CACHE_TTL = 600; // seconds

function browserHeaders(extra) {
  return {
    "user-agent": UA,
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "upgrade-insecure-requests": "1",
    ...extra,
  };
}

function collectCookies(setCookies) {
  const out = [];
  for (const c of setCookies || []) {
    const pair = c.split(";")[0];
    if (pair.includes("=")) out.push(pair);
  }
  return out.join("; ");
}

// Warm-up GET grants the anonymous cookies a real browser would have.
async function bootstrap() {
  try {
    const warm = await fetch(`https://${ALLOWED_HOST}/`, {
      headers: browserHeaders(),
      redirect: "follow",
      cf: { cacheTtl: 0, cacheEverything: false },
    });
    const cookieHeader = collectCookies(warm.headers.getSetCookie());
    let lsd = "";
    if (warm.ok) {
      const m = (await warm.text()).match(/"LSD",\[\],\{"token":"([^"]+)"\}/);
      if (m) lsd = m[1];
    }
    return { cookieHeader, lsd };
  } catch {
    return { cookieHeader: "", lsd: "" };
  }
}

async function fetchWithRetry(url, headers) {
  let lastStatus = 0;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt > 0) await new Promise((r) => setTimeout(r, 1500));
    const resp = await fetch(url, {
      headers,
      redirect: "follow",
      cf: { cacheTtl: 0, cacheEverything: false },
    });
    lastStatus = resp.status;
    if (resp.ok) {
      const body = await resp.text();
      if (body && body.length > 2000) return { body, upstreamStatus: lastStatus };
      lastStatus = 0; // too-small body = challenge/empty shell
      continue;
    }
  }
  return { body: "", upstreamStatus: lastStatus };
}

// API mode: Instagram's own web_profile_info endpoint, called with the exact
// headers a logged-out real browser sends (app id, CSRF, LSD, cookies).
async function fetchProfileApi(username) {
  const { cookieHeader, lsd } = await bootstrap();
  const csrf = (cookieHeader.match(/csrftoken=([^;]+)/) || [])[1] || "";
  const headers = browserHeaders({
    "accept": "*/*",
    "x-ig-app-id": APP_ID,
    "x-requested-with": "XMLHttpRequest",
    "x-csrftoken": csrf,
    ...(cookieHeader ? { cookie: cookieHeader } : {}),
    ...(lsd ? { "x-fb-lsd": lsd, "x-asbd-id": "129477" } : {}),
    "referer": `https://www.instagram.com/${username}/`,
  });
  const url = `https://www.instagram.com/api/v1/users/web_profile_info/?username=${encodeURIComponent(username)}`;
  return fetchWithRetry(url, headers);
}

async function fetchProfileHtml(target) {
  const { cookieHeader } = await bootstrap();
  return fetchWithRetry(
    target,
    browserHeaders(cookieHeader ? { cookie: cookieHeader } : {})
  );
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors() });
    }

    const apiUser = url.searchParams.get("api");
    const target = url.searchParams.get("url");

    // ---- API mode: ?api=1&username=<handle> -> web_profile_info JSON ----
    if (apiUser !== null) {
      const username = (url.searchParams.get("username") || "").trim();
      if (!/^[A-Za-z0-9._]{1,30}$/.test(username)) {
        return json({ error: "invalid username" }, 400);
      }
      const cache = caches.default;
      const cacheKey = new Request(`${url.origin}/api/${username.toLowerCase()}`, request);
      const cached = await cache.match(cacheKey);
      if (cached) {
        const h = new Headers(cached.headers);
        h.set("x-relay-cache", "hit");
        return new Response(cached.body, { status: 200, headers: h });
      }
      const { body, upstreamStatus } = await fetchProfileApi(username);
      const h = cors();
      h.set("x-relay-status", String(upstreamStatus));
      h.set("content-type", "application/json; charset=utf-8");
      if (body) {
        const cacheable = new Response(body, {
          status: 200,
          headers: { "content-type": "application/json; charset=utf-8",
                     "cache-control": `public, max-age=${EDGE_CACHE_TTL}` },
        });
        await cache.put(cacheKey, cacheable.clone());
        h.set("x-relay-cache", "miss");
        return new Response(body, { status: 200, headers: h });
      }
      return new Response("", { status: 200, headers: h });
    }

    // ---- HTML mode (v2): ?url=https://www.instagram.com/<handle>/ ----
    if (!target) return json({ error: "missing ?url= or ?api=1&username=" }, 400);
    let parsed;
    try { parsed = new URL(target); } catch { return json({ error: "invalid url" }, 400); }
    if (parsed.hostname !== ALLOWED_HOST) {
      return json({ error: `only ${ALLOWED_HOST} is allowed` }, 403);
    }

    const cache = caches.default;
    let cached = await cache.match(request);
    if (cached) {
      const h = new Headers(cached.headers);
      h.set("x-relay-cache", "hit");
      return new Response(cached.body, { status: 200, headers: h });
    }

    let result;
    try { result = await fetchProfileHtml(parsed.toString()); }
    catch (err) { return json({ error: "upstream fetch failed", detail: String(err) }, 502); }

    const h = cors();
    h.set("x-relay-status", String(result.upstreamStatus));
    h.set("content-type", "text/plain; charset=utf-8");
    if (result.body) {
      const cacheable = new Response(result.body, {
        status: 200,
        headers: { "content-type": "text/plain; charset=utf-8",
                   "cache-control": `public, max-age=${EDGE_CACHE_TTL}` },
      });
      await cache.put(request, cacheable.clone());
      h.set("x-relay-cache", "miss");
      return new Response(result.body, { status: 200, headers: h });
    }
    return new Response("", { status: 200, headers: h });
  },
};

function cors() {
  const hr = new Headers();
  hr.set("access-control-allow-origin", "*");
  hr.set("access-control-allow-methods", "GET,OPTIONS");
  return hr;
}

function json(obj, status) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json", ...Object.fromEntries(cors()) },
  });
}
