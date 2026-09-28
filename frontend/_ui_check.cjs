/* Headless-Chrome end-to-end check of the deployed InstaIQ UI.
 * Run from frontend/ :  node _ui_check.cjs
 * Env: FE_URL (default deployed Vercel URL), UI_HANDLE (default gymshark),
 *      WAIT_TIMEOUT (default 300s), HEADLESS (default 1)
 */
const puppeteer = require('puppeteer-core');

const FE = process.env.FE_URL || 'https://frontend-psi-ivory-18.vercel.app';
const HANDLE = process.env.UI_HANDLE || 'gymshark';
const WAIT_TIMEOUT = parseInt(process.env.WAIT_TIMEOUT || '300', 10) * 1000;
const HEADLESS = (process.env.HEADLESS || '1') !== '0';

function findChrome() {
  const candidates = [
    process.env.IG_CHROME_PATH,
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    (process.env.LOCALAPPDATA || '') + '\\Google\\Chrome\\Application\\chrome.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
  ].filter(Boolean);
  const fs = require('fs');
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return null;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
const consoleErrors = [];
const failedApiCalls = [];

function ok(name, pass, detail = '') {
  results.push({ name, pass, detail });
  console.log(`  ${pass ? '[ OK ]' : '[FAIL]'} ${name}${detail ? ' — ' + detail : ''}`);
}

(async () => {
  const exe = findChrome();
  if (!exe) { console.error('No Chrome found'); process.exit(2); }
  console.log(`UI check → ${FE} (handle: @${HANDLE})\n`);

  const browser = await puppeteer.launch({
    executablePath: exe,
    headless: HEADLESS ? 'new' : false,
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--window-size=1440,900'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });

  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)); });
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + String(e).slice(0, 200)));
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) {
      failedApiCalls.push(`${r.status()} ${r.url().replace(/^https?:\/\/[^/]+/, '')}`);
    }
  });

  try {
    // 1 — App loads
    await page.goto(FE, { waitUntil: 'networkidle2', timeout: 60000 });
    const h1 = await page.$eval('h1', (el) => el.textContent).catch(() => null);
    ok('App loads (hero heading)', !!h1, h1 || 'no <h1>');

    // 2 — Input + button present
    const hasInput = !!(await page.$('.scanner-row input'));
    const btnText = await page.$eval('.scanner-submit', (el) => el.textContent).catch(() => null);
    ok('Scanner input present', hasInput);
    ok('Submit button present', btnText === 'Do everything', btnText || '');

    // 3 — Run the analyze flow for real
    await page.click('.scanner-row input', { clickCount: 3 });
    await page.type('.scanner-row input', HANDLE, { delay: 20 });
    await page.click('.scanner-submit');
    console.log('  … submitted. Waiting for the dashboard (fresh fetch + LLM can take minutes on free tiers)…');

    await page.waitForSelector('.dash-nav', { timeout: WAIT_TIMEOUT });
    ok('Dashboard rendered', true);

    // 4 — Key dashboard widgets
    const score = await page.$eval('.dash-hero', (el) => el.textContent.slice(0, 300)).catch(() => null);
    ok('Score hero rendered', !!score);
    const bodyText = await page.evaluate(() => document.body.innerText);
    ok('Metrics present (followers/ER)', /followers|engagement/i.test(bodyText));
    ok('AI report section rendered', /strengths|weaknesses|recommendations|strategy/i.test(bodyText));
    ok('Competitors section rendered', /competitor|rival|market/i.test(bodyText));

    await page.screenshot({ path: 'ui_check_dashboard.png', fullPage: false });

    // 5 — Chat: open the floating bubble, ask, wait for the AI answer
    const chatOpen = await page.$('[aria-label="Open chat"]');
    if (chatOpen) {
      await chatOpen.click();
      await page.waitForSelector('textarea', { timeout: 10000 });
      await page.click('textarea');
      await page.type('textarea', 'In one sentence: is my engagement good?', { delay: 15 });
      await page.click('[aria-label="Send message"]');
      console.log('  … chat question sent, waiting for the AI answer…');
      const answered = await page.waitForFunction(
        () => {
          // an assistant reply appeared after the user's own message
          const thinking = document.body.innerText.includes('thinking…');
          const t = document.body.innerText;
          return !thinking && /engagement|rate|good|account/i.test(t) &&
            document.querySelectorAll('textarea').length > 0 &&
            t.split('\n').length > 30; // reply text added to the panel
        },
        { timeout: 120000, polling: 1000 },
      ).then(() => true).catch(() => false);
      ok('AI chat answered', answered);
    } else {
      ok('AI chat answered', false, 'chat launcher button not found');
    }

    await page.screenshot({ path: 'ui_check_chat.png', fullPage: false });

    // 6 — Console / API health
    const realErrors = consoleErrors.filter((e) => !/favicon|manifest|sourcemap/i.test(e));
    ok('No console errors', realErrors.length === 0, realErrors.slice(0, 3).join(' | '));
    ok('No failed API calls (4xx/5xx)', failedApiCalls.length === 0, failedApiCalls.slice(0, 3).join(' | '));
  } catch (e) {
    ok('Flow completed', false, String(e).slice(0, 160));
    await page.screenshot({ path: 'ui_check_failure.png', fullPage: true }).catch(() => {});
  } finally {
    await browser.close();
  }

  const passed = results.filter((r) => r.pass).length;
  console.log(`\n${passed}/${results.length} checks passed. Screenshots: ui_check_dashboard.png, ui_check_chat.png`);
  process.exit(passed === results.length ? 0 : 1);
})();
