/**
 * Browser regression tests for curbcall_netlify_v4/app.html.
 *
 * These cover two field-reliability bugs that are easy to reintroduce and
 * invisible in code review:
 *
 *   1. Offline boot. app.html loads supabase-js and Leaflet from CDNs. If
 *      supabase-js is missing, `supabase.createClient()` throws at the top
 *      level and kills the whole script — a signed-in contractor with no
 *      signal gets a dead sign-in screen instead of the bids already on their
 *      phone. The app must fall back to a read-only local mode.
 *
 *   2. Bid ids containing apostrophes. bidId() builds an id out of the city +
 *      title text, so a bid in a town like O'Fallon produces an id with a
 *      quote in it. Any handler wired as onclick="fn('<id>')" becomes a
 *      syntax error and the button silently does nothing. Bid actions must be
 *      wired as real listeners, never interpolated into inline handlers.
 *
 *   3. Saved-search throttling. Automatic re-scans used to fire on every app
 *      open, one 150s request per saved search — minutes of mobile data per
 *      launch. They must be rate-limited.
 *
 * Unlike the Python suite these need a browser, so they are not part of
 * `pytest`. Run them manually:
 *
 *     npm install -g playwright && npx playwright install chromium
 *     node tests/test_web_app.js
 *
 * Exits non-zero if any check fails.
 */
const http = require("http");
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const ROOT = path.join(__dirname, "..", "curbcall_netlify_v4");
const PORT = Number(process.env.PORT || 8177);
const BASE = `http://127.0.0.1:${PORT}`;

const CDN_HOSTS = ["unpkg.com", "cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com"];
const MIME = { ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
               ".png": "image/png", ".webmanifest": "application/manifest+json" };

// A bid in a town whose name carries an apostrophe — precisely the input that
// used to generate a broken onclick handler.
const SEED_CITY = "O'Fallon, MO";
const SEED_BID = {
  title: "O'Fallon Sidewalk & ADA Ramp Replacement",
  scope: "Remove and replace 1,200 LF of sidewalk plus 8 ADA ramps.",
  value: "$310k",
  deadline: "2026-12-01",
  status: "open",
  url: "https://example.gov/bid/1",
};

// Stands in for supabase-js. The real CDN may be unreachable from CI, and for
// the throttle test the page specifically needs to believe it is online.
const SB_STUB = `
  window.supabase = {
    createClient: function () {
      return {
        auth: {
          onAuthStateChange: function () {
            return { data: { subscription: { unsubscribe: function () {} } } };
          },
          getSession: async function () { return { data: { session: null } }; },
          signOut: async function () { return {}; }
        },
        from: function () {
          var c = {
            select: async function () { return { data: null, error: {} }; },
            upsert: async function () { return {}; },
            insert: async function () { return {}; },
            delete: function () { return c; },
            eq: function () { return c; },
            maybeSingle: async function () { return { data: null, error: {} }; }
          };
          return c;
        },
        storage: { from: function () { return {}; } }
      };
    }
  };`;

function startServer() {
  const server = http.createServer((req, res) => {
    const rel = decodeURIComponent(req.url.split("?")[0]).replace(/^\/+/, "") || "index.html";
    const file = path.join(ROOT, rel);
    if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
      res.writeHead(404); return res.end("not found");
    }
    res.writeHead(200, { "Content-Type": MIME[path.extname(file)] || "application/octet-stream" });
    fs.createReadStream(file).pipe(res);
  });
  return new Promise((resolve) => server.listen(PORT, "127.0.0.1", () => resolve(server)));
}

let failures = 0;
function check(name, cond, detail) {
  if (cond) console.log(`  PASS  ${name}`);
  else { failures++; console.log(`  FAIL  ${name}${detail ? " — " + detail : ""}`); }
}

function seedSignedIn({ city, bid, searches, checkedAt }) {
  localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
  if (bid) localStorage.setItem("last_feed", JSON.stringify({ [city]: [bid] }));
  if (searches) localStorage.setItem("saved_searches", JSON.stringify(searches));
  if (checkedAt != null) localStorage.setItem("saved_search_checked_at", JSON.stringify(checkedAt));
}

(async () => {
  const server = await startServer();
  const browser = await chromium.launch();

  // ── 1. The truck-with-no-bars case ──
  console.log("\nOffline boot with Leaflet + supabase-js unreachable");
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));

    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (CDN_HOSTS.includes(host) || host.endsWith("supabase.co") || host.endsWith("onrender.com")) {
        return route.abort();
      }
      return route.continue();
    });
    await page.addInitScript(seedSignedIn, { city: SEED_CITY, bid: SEED_BID });
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(1200);

    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    check("app shell is visible, not stuck on sign-in",
      (await page.locator("#app").isVisible()) && !(await page.locator("#auth-screen").isVisible()));
    check("offline banner is shown", await page.locator("#offline-bar").isVisible());
    const bidCount = await page.locator("#feed-list .bid").count();
    check("cached bid still renders", bidCount === 1, `found ${bidCount}`);

    await page.locator('.nav-btn[data-s="scan"]').click();
    await page.fill("#loc-input", "65605");
    await page.locator("#scan-btn").click();
    await page.waitForTimeout(400);
    const toastTxt = await page.locator("#toast").innerText();
    check("scanning refuses with an offline message", /offline/i.test(toastTxt), toastTxt);
    await ctx.close();
  }

  // ── 2. Apostrophes in bid ids ──
  console.log("\nBid actions on an id containing an apostrophe");
  {
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));

    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host.endsWith("supabase.co") || host.endsWith("onrender.com")) return route.abort();
      return route.continue();
    });
    await page.addInitScript(seedSignedIn, { city: SEED_CITY, bid: SEED_BID });
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(1200);
    await page.evaluate(() => { if (typeof bootOffline === "function") bootOffline(); });
    await page.locator('.nav-btn[data-s="feed"]').click();
    await page.waitForTimeout(300);

    check("bid card present", (await page.locator("#feed-list .bid").count()) === 1);
    await page.locator("#feed-list .bid").first().click();
    await page.waitForTimeout(300);
    check("detail modal opens",
      await page.locator("#modal").evaluate((el) => el.classList.contains("open")));

    const submitted = page.locator('#modal-content [data-pstatus="submitted"]');
    check("pipeline buttons rendered", (await submitted.count()) === 1);
    await submitted.click();
    await page.waitForTimeout(300);

    const stored = await page.evaluate(() => JSON.parse(localStorage.getItem("pipeline") || "{}"));
    const keys = Object.keys(stored);
    check("clicking Submitted persists the status",
      keys.length === 1 && stored[keys[0]] === "submitted", JSON.stringify(stored));
    check("the id really does contain an apostrophe", !!keys[0] && keys[0].includes("'"), keys[0]);
    check("save action is wired", (await page.locator('#modal-content [data-act="save"]').count()) === 1);
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── 3. Saved-search throttling ──
  console.log("\nSaved searches are not re-scanned on every app open");
  {
    async function openWith(checkedAt) {
      const ctx = await browser.newContext();
      const page = await ctx.newPage();
      const scanCalls = [];
      await page.route("**/*", (route) => {
        const u = route.request().url();
        const host = new URL(u).hostname;
        if (u.includes("/scan")) {
          scanCalls.push(u);
          return route.fulfill({ status: 200, contentType: "application/json", body: '{"ok":true,"bids":{}}' });
        }
        if (host === "cdn.jsdelivr.net") {
          return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
        }
        if (host.endsWith("supabase.co") || host.endsWith("onrender.com")) return route.abort();
        return route.continue();
      });
      await page.addInitScript(seedSignedIn,
        { searches: [{ location: "65605", radius: 25 }], checkedAt });
      await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
      await page.waitForTimeout(800);
      const online = await page.evaluate(() => !isOffline());
      await page.evaluate(() => checkSavedSearches());
      await page.waitForTimeout(800);
      await ctx.close();
      return { calls: scanCalls.length, online };
    }

    const recent = await openWith(Date.now() - 60 * 1000);
    check("page is treated as online", recent.online);
    check("a recent check does not re-scan", recent.calls === 0, `${recent.calls} scan call(s)`);

    const stale = await openWith(Date.now() - 12 * 60 * 60 * 1000);
    check("a stale check does re-scan", stale.calls === 1, `${stale.calls} scan call(s)`);
  }

  await browser.close();
  server.close();
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nAll checks passed");
  process.exit(failures ? 1 : 0);
})();
