/* Keyless Instagram fetch via real Chrome (puppeteer-core).
 *
 * WHY: Instagram hard-blocks datacenter IPs and periodically throttles even
 * residential ones at the HTTP level (web_profile_info returns 401, the HTML
 * page login-walls). A real browser passes those checks and gets the real
 * page: exact stats in the embedded Relay JSON, plus the numeric user id.
 *
 * Output (stdout, single JSON line):
 *   {"ok":true,  "html": "...", "feed": {...}|null, "user_id": "..."}
 *   {"ok":false, "error": "...", "kind": "notfound"|"blocked"|"browser"|"internal"}
 *
 * Optional argv:
 *   --url=https://www.instagram.com/<handle>/   (profile page to render)
 *   --feed-url=<full graphql/query URL>         (fetch this JSON inside the page)
 *
 * The feed URL is built and validated by the Python side; this helper only
 * ever fetches instagram.com URLs (it refuses anything else).
 */

const fs = require("fs");

function out(obj) {
  process.stdout.write(JSON.stringify(obj));
  process.exit(0);
}

function arg(name) {
  const hit = process.argv.find((a) => a.startsWith(name + "="));
  return hit ? hit.slice(name.length + 1) : null;
}

async function main() {
  let puppeteer;
  try {
    puppeteer = require("puppeteer-core");
  } catch {
    // Backend runs from backend/; frontend/node_modules is the known install.
    const path = require("path");
    const alt = path.join(__dirname, "..", "frontend", "node_modules", "puppeteer-core");
    try {
      puppeteer = require(alt);
  } catch (e2) {
      out({ ok: false, kind: "browser", error: "puppeteer-core not installed" });
  }
  }

  const CHROME_CANDIDATES = [
    process.env.IG_CHROME_PATH || "",
    "C:\\Users\\DELL\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    process.env["ProgramFiles"] ? process.env["ProgramFiles"] + "\\Google\\Chrome\\Application\\chrome.exe" : "",
    process.env["ProgramFiles(x86)"] ? process.env["ProgramFiles(x86)"] + "\\Google\\Chrome\\Application\\chrome.exe" : "",
    process.env["LOCALAPPDATA"] ? process.env["LOCALAPPDATA"] + "\\Google\\Chrome\\Application\\chrome.exe" : "",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);
  const CHROME = CHROME_CANDIDATES.find((p) => {
    try { return fs.existsSync(p); } catch { return false; }
  });
  if (!CHROME) {
    out({ ok: false, kind: "browser", error: "Chrome executable not found (set IG_CHROME_PATH)" });
  }

  const targetUrl = arg("--url") || "https://www.instagram.com/";
  const feedUrl = arg("--feed-url");

  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: "new",
    args: [
      "--no-sandbox",
      "--disable-blink-features=AutomationControlled",
      "--window-size=1366,900",
    ],
  });

  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1366, height: 900 });
    await page.setUserAgent(
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    );

    // 1) Warm-up: the homepage grants the anonymous cookies a real visit has.
    await page.goto("https://www.instagram.com/", {
      waitUntil: "domcontentloaded",
      timeout: 45000,
    });
    await sleep(2000);

    // 2) Profile page.
    await page.goto(targetUrl, { waitUntil: "domcontentloaded", timeout: 45000 });
    await page
      .waitForFunction(
        () => {
          const og = document.querySelector('meta[property="og:description"]');
          return (og && og.content && og.content.length > 10);
        },
        { timeout: 20000 }
      )
      .catch(() => {});
    await sleep(3000);

    const info = await page.evaluate(() => {
      const og = document.querySelector('meta[property="og:description"]');
      const html = document.documentElement.outerHTML;
      const m = html.match(/profilePage_(\d{5,})/);
      return {
        ogDesc: og ? og.content : null,
        bytes: html.length,
        isLoginPage: !!document.querySelector('input[name="username"]'),
        hasGql: html.includes("edge_followed_by") || html.includes("follower_count"),
        userId: m ? m[1] : null,
      };
    });

    if (info.isLoginPage && !info.hasGql) {
      out({ ok: false, kind: "blocked", error: "Chrome render got the login wall" });
    }
    if (!info.hasGql && !info.ogDesc) {
      // No profile JSON and no og tags: either the handle does not exist or
      // Instagram refused. Distinguish by the login-wall signature above.
      out({ ok: false, kind: "notfound", error: "profile page had no parseable data" });
    }

    const html = await page.content();

    // 3) Try Instagram's own web_profile_info API from INSIDE the page
    //    (same-origin, real browser TLS/session). Succeeds whenever the
    //    visitor's IP is not hard API-blocked — returns exact stats AND
    //    the latest posts with engagement. Best-effort: 401s are common
    //    for throttled IPs and simply mean posts stay empty.
    let profileApi = null;
    {
      profileApi = await page
        .evaluate(async (h) => {
          try {
            const html = document.documentElement.innerHTML;
            const lsdM = html.match(/"LSD",\[\],\{"token":"([^"]+)"\}/);
            const csrfM = document.cookie.match(/csrftoken=([^;]+)/);
            const r = await fetch(
              `/api/v1/users/web_profile_info/?username=${encodeURIComponent(h)}`,
              {
                credentials: "include",
                headers: {
                  "x-ig-app-id": "936619743392459",
                  "x-requested-with": "XMLHttpRequest",
                  accept: "*/*",
                  ...(lsdM ? { "x-fb-lsd": lsdM[1] } : {}),
                  ...(csrfM ? { "x-csrftoken": csrfM[1] } : {}),
                },
              }
            );
            if (!r.ok) return { status: r.status };
            return await r.json();
          } catch (e) {
            return { error: String(e) };
          }
        }, handleFromUrl(targetUrl))
        .catch(() => null);
    }

    // 4) The classic GraphQL feed query INSIDE the page for real post
    //    engagement. Built here from the user id found in the DOM (or taken
    //    from --feed-url); refused unless it is an instagram.com URL.
    //    Best-effort: throttled IPs get 401 and posts stay empty.
    const queryId = arg("--query-id") || "17842794232208280";
    const feedTarget =
      feedUrl ||
      (info.userId
        ? `https://www.instagram.com/graphql/query/?query_id=${queryId}&variables=${encodeURIComponent(
            JSON.stringify({ id: info.userId, first: 12 })
          )}`
        : null);
    let feed = null;
    if (feedTarget) {
      if (!/^https:\/\/(www\.)?instagram\.com\//.test(feedTarget)) {
        out({ ok: false, kind: "internal", error: "feed URL must be an instagram.com URL" });
      }
      feed = await page
        .evaluate(async (u) => {
          try {
            const html = document.documentElement.innerHTML;
            const lsdM = html.match(/"LSD",\[\],\{"token":"([^"]+)"\}/);
            const csrfM = document.cookie.match(/csrftoken=([^;]+)/);
            const r = await fetch(u, {
              credentials: "include",
              headers: {
                "x-ig-app-id": "936619743392459",
                "x-requested-with": "XMLHttpRequest",
                ...(lsdM ? { "x-fb-lsd": lsdM[1] } : {}),
                ...(csrfM ? { "x-csrftoken": csrfM[1] } : {}),
              },
            });
            if (!r.ok) return { status: r.status };
            return await r.json();
          } catch (e) {
            return { error: String(e) };
          }
        }, feedTarget)
        .catch(() => null);
    }

    out({ ok: true, html, profile_api: profileApi, feed, user_id: info.userId || null });
  } catch (e) {
    out({ ok: false, kind: "browser", error: String((e && e.message) || e) });
  } finally {
    try { await browser.close(); } catch {}
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function handleFromUrl(u) {
  try {
    const p = new URL(u).pathname.replace(/^\//, "").replace(/\/$/, "");
    return p || "";
  } catch {
    return "";
 }
}

main();
