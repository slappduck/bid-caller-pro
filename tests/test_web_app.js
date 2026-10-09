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

// Playwright's own default viewport (1280x720) happens to sit past this
// app's 960px desktop breakpoint. That was harmless while the desktop nav
// was just a repositioned sidebar -- always visible either way -- but the
// nav dropdown it became only renders its .nav-btn's once opened, so a
// context left at that default now hides them from every check here that
// clicks one directly. Phone is what nearly all of these are actually
// about anyway (offline boot, bid ids, feed merge, hostile URLs...), so
// contexts that don't care about a specific width get a real phone size
// instead of an unexamined desktop-shaped accident.
const MOBILE_VIEWPORT = { viewport: { width: 390, height: 844 } };

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
    let rel = decodeURIComponent(req.url.split("?")[0]).replace(/^\/+/, "") || "index.html";
    let file = path.join(ROOT, rel);
    // Mirror _redirects' "/go/<slug> /index.html 200" rewrite: production
    // never 404s a /go/ link, it serves the marketing page with the slug
    // still in the address bar for the page's own JS to read.
    if (/^go\/[a-z0-9][a-z0-9_-]{0,31}\/?$/i.test(rel)) {
      rel = "index.html";
      file = path.join(ROOT, rel);
    }
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
  const browser = await chromium.launch(process.env.PW_CHROME ? { executablePath: process.env.PW_CHROME } : {});

  // ── 1. The truck-with-no-bars case ──
  console.log("\nOffline boot with Leaflet + supabase-js unreachable");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
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
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
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
    // The city and title still carry an apostrophe; the id no longer does,
    // because ids are hashed now. Both halves of the original bug are covered:
    // hashing keeps quotes out of the id, and the delegated listeners keep the
    // buttons working regardless of what the text contains.
    check("the bid's own text still contains the apostrophe",
      SEED_CITY.includes("'") && SEED_BID.title.includes("'"));
    check("the id is a clean hash", /^[0-9a-f]{12}$/.test(keys[0] || ""), keys[0]);
    check("save action is wired", (await page.locator('#modal-content [data-act="save"]').count()) === 1);
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));

    // Deadlines are usually prose, not a bare ISO date. Parsing only ISO cost
    // the urgency chip, deadline sorting, and Add to Calendar.
    const deadlines = await page.evaluate(() => {
      const texts = ["2026-12-01", "12/01/2026", "December 1, 2026",
                     "Due by 12/01/2026 at 2:00 PM",
                     "Bids due December 1, 2026 at 2:00 p.m.",
                     "Thursday, December 1, 2026", "12/01/2026 2:00 PM CST",
                     "Submit no later than 3:00 PM on 12/1/2026"];
      return {
        parsed: texts.filter((t) => deadlineDate({ deadline: t }) !== null).length,
        total: texts.length,
        ics: icsDate("Bids due December 1, 2026 at 2:00 p.m."),
        junk: ["", "TBD", "n/a"].filter((t) => deadlineDate({ deadline: t }) !== null).length,
      };
    });
    check("every realistic deadline format parses",
      deadlines.parsed === deadlines.total, `${deadlines.parsed}/${deadlines.total}`);
    check("Add to Calendar works on a prose deadline",
      deadlines.ics === "20261201", String(deadlines.ics));
    check("non-dates still parse to nothing", deadlines.junk === 0);

    await ctx.close();
  }

  // ── 3. Bid ids: collision-free and identical to the desktop app ──
  console.log("\nBid ids match the desktop scheme and survive migration");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host.endsWith("supabase.co") || host.endsWith("onrender.com")) return route.abort();
      return route.continue();
    });

    // Seed data written by the PREVIOUS id scheme, exactly as a real user's
    // phone would already have it.
    const legacy = (city, b) =>
      (city + (b.title || "") + (b.scope || "")).replace(/\s/g, "").slice(0, 40);
    const CITY = "Springfield";
    const P1 = { title: "City of Springfield Sidewalk Replacement Project Phase 1",
                 scope: "Remove and replace sidewalk on Main St.", status: "open" };
    const oldId = legacy(CITY, P1);

    // Seeded once only. addInitScript runs on every navigation, so without the
    // guard the reload below would rewrite the legacy fixture and the
    // idempotence check would be measuring the fixture, not the migration.
    await page.addInitScript(({ city, p1, oldId }) => {
      if (localStorage.getItem("__seeded")) return;
      localStorage.setItem("__seeded", "1");
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
      localStorage.setItem("last_feed", JSON.stringify({ [city]: [p1] }));
      localStorage.setItem("saved", JSON.stringify({ [oldId]: { ...p1, _city: city } }));
      localStorage.setItem("pipeline", JSON.stringify({ [oldId]: "submitted" }));
      localStorage.setItem("notes", JSON.stringify({ [oldId]: "called the city clerk" }));
    }, { city: CITY, p1: P1, oldId });

    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(1000);

    const r = await page.evaluate(({ city, p1 }) => {
      const p2 = { title: "City of Springfield Sidewalk Replacement Project Phase 2",
                   scope: "Remove and replace sidewalk on Elm St.", status: "open" };
      return {
        id1: bidId(city, p1),
        id2: bidId(city, p2),
        // Known-good value from Python: md5(city+title+scope)[:12]
        aurora: bidId("Aurora, MO", { title: "Aurora Sidewalk & ADA Ramp Replacement",
                                      scope: "1,200 LF sidewalk, 8 ADA ramps" }),
        saved: JSON.parse(localStorage.getItem("saved")),
        pipeline: JSON.parse(localStorage.getItem("pipeline")),
        notes: JSON.parse(localStorage.getItem("notes")),
      };
    }, { city: CITY, p1: P1 });

    check("Phase 1 and Phase 2 get different ids", r.id1 !== r.id2, `${r.id1} vs ${r.id2}`);
    check("id matches the desktop md5 scheme", r.aurora === "b7e81f74ffb3", r.aurora);
    check("migrated saved bid is re-keyed to the new id",
      Object.keys(r.saved).length === 1 && Object.keys(r.saved)[0] === r.id1,
      Object.keys(r.saved).join(","));
    check("migrated pipeline status follows the bid",
      r.pipeline[r.id1] === "submitted", JSON.stringify(r.pipeline));
    check("migrated note follows the bid",
      r.notes[r.id1] === "called the city clerk", JSON.stringify(r.notes));

    // A second load must not re-run the migration or disturb anything.
    await page.reload({ waitUntil: "load" });
    await page.waitForTimeout(800);
    const after = await page.evaluate(() => JSON.parse(localStorage.getItem("saved")));
    check("migration is idempotent across reloads",
      Object.keys(after).length === 1 && Object.keys(after)[0] === r.id1,
      Object.keys(after).join(","));
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── 4. Hostile bid URLs ──
  // A bid's url is AI-extracted from whatever page a search engine pointed at,
  // so it is untrusted. It must never reach an href as a javascript: URL or
  // break out of the attribute.
  console.log("\nHostile bid URLs cannot inject into the detail view");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host.endsWith("supabase.co") || host.endsWith("onrender.com")) return route.abort();
      return route.continue();
    });

    const CITY = "Testville";
    const HOSTILE = [
      { title: "Scheme injection", scope: "s", status: "open",
        url: "javascript:window.__pwned=1" },
      { title: "Attribute break-out", scope: "s", status: "open",
        url: 'https://ok.example/" onmouseover="window.__pwned=1' },
      { title: "Control char smuggling", scope: "s", status: "open",
        url: "java\tscript:window.__pwned=1" },
      { title: "Hostile mailto", scope: "s", status: "open",
        email: 'a@b.com" onclick="window.__pwned=1' },
      { title: "Benign control", scope: "s", status: "open",
        url: "https://ok.example/bid/1" },
    ];
    await page.addInitScript(({ city, bids }) => {
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
      localStorage.setItem("last_feed", JSON.stringify({ [city]: bids }));
    }, { city: CITY, bids: HOSTILE });

    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(1000);
    await page.evaluate(() => { if (typeof bootOffline === "function") bootOffline(); });
    await page.locator('.nav-btn[data-s="feed"]').click();
    await page.waitForTimeout(300);

    const cards = page.locator("#feed-list .bid");
    check("all hostile bids still render", (await cards.count()) === HOSTILE.length,
      String(await cards.count()));

    let sawJsHref = false, sawInjectedAttr = false, benignLinkWorked = false;
    for (let i = 0; i < HOSTILE.length; i++) {
      await cards.nth(i).click();
      await page.waitForTimeout(200);
      const info = await page.evaluate(() => {
        const anchors = Array.from(document.querySelectorAll("#modal-content a"));
        return {
          hrefs: anchors.map((a) => a.getAttribute("href") || ""),
          injected: anchors.some((a) => a.hasAttribute("onmouseover") || a.hasAttribute("onclick")),
        };
      });
      if (info.hrefs.some((h) => /^\s*javascript:/i.test(h))) sawJsHref = true;
      if (info.injected) sawInjectedAttr = true;
      if (info.hrefs.includes("https://ok.example/bid/1")) benignLinkWorked = true;
      await page.evaluate(() => closeModal());
      await page.waitForTimeout(120);
    }
    const pwned = await page.evaluate(() => typeof window.__pwned !== "undefined");

    check("no javascript: URL ever reaches an href", !sawJsHref);
    check("no event-handler attribute is injected", !sawInjectedAttr);
    check("no script executed", !pwned);
    check("a legitimate https link still renders", benignLinkWorked);
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── 5. Feed merge: de-duplication and a real "Newest" order ──
  console.log("\nFeed merge de-duplicates and tracks when a bid first appeared");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host.endsWith("supabase.co") || host.endsWith("onrender.com")) return route.abort();
      return route.continue();
    });
    await page.addInitScript(() =>
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com")));
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(1000);

    const r = await page.evaluate(async () => {
      const city = "Testville";
      const A = { title: "Alpha sidewalk job", scope: "a", status: "open" };
      const B = { title: "Beta curb job", scope: "b", status: "open" };
      // A arrives twice in one payload, as it would from two source pages.
      const firstAdded = mergeOpenBids({ [city]: [A, { ...A }, B] }, [city]);
      const afterFirst = bidData[city].length;
      const stampA = bidData[city].find((x) => x.title === A.title)._first_seen;

      await new Promise((res) => setTimeout(res, 25));
      // Rescan: A is unchanged, B is gone, C is new.
      const C = { title: "Gamma ADA ramp job", scope: "c", status: "open" };
      const secondAdded = mergeOpenBids({ [city]: [A, C] }, [city]);
      const rows = bidData[city];
      return {
        firstAdded, afterFirst, secondAdded,
        titles: rows.map((x) => x.title),
        stampPreserved: rows.find((x) => x.title === A.title)._first_seen === stampA,
        newerIsC: rows.find((x) => x.title === C.title)._first_seen >
                  rows.find((x) => x.title === A.title)._first_seen,
      };
    });

    check("a bid repeated in one payload is stored once", r.afterFirst === 2, String(r.afterFirst));
    check("the duplicate is not counted as a new find", r.firstAdded === 2, String(r.firstAdded));
    check("a rescan only counts genuinely new bids", r.secondAdded === 1, String(r.secondAdded));
    check("a bid gone from the rescan is dropped", !r.titles.includes("Beta curb job"));
    check("first-seen survives a rescan", r.stampPreserved);
    check("a newly found bid sorts as newer", r.newerIsC);
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── 6. Landing-page checkout links carry the device id ──
  // The Stripe webhook records client_reference_id as the buyer's device, and
  // the app asks /mykey for the key belonging to its device. Without the tag,
  // a subscription bought from the marketing page left the buyer looking at a
  // "trial expired, subscribe" screen.
  console.log("\nLanding-page checkout links are tagged with the device id");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host.endsWith("fonts.googleapis.com") || host.endsWith("fonts.gstatic.com")) {
        return route.abort();
      }
      return route.continue();
    });

    // Arrive at the marketing page first, as a new visitor would.
    await page.goto(`${BASE}/index.html`, { waitUntil: "load" });
    await page.waitForTimeout(500);

    const links = await page.evaluate(() =>
      Array.from(document.querySelectorAll('a[href*="buy.stripe.com"]'))
        .map((a) => a.getAttribute("href")));
    const landingId = await page.evaluate(() => JSON.parse(localStorage.getItem("device_id")));

    check("stripe links exist on the landing page", links.length >= 2, String(links.length));
    check("every checkout link carries a client_reference_id",
      links.length > 0 && links.every((h) => h.includes("client_reference_id=")),
      links.join(" | "));
    check("the id is stored under the key app.html uses", typeof landingId === "string" && !!landingId,
      String(landingId));

    // The app must then adopt that same id, not mint a second one.
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(800);
    const appId = await page.evaluate(() => deviceId());
    check("the app reuses the landing page's device id", appId === landingId,
      `${landingId} vs ${appId}`);
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── 7. Diagnostics card ──
  // A quiet area and a pipeline discarding everything it found look identical
  // from the outside. This is what tells them apart during a live test.
  console.log("\nDiagnostics card reports the scan funnel and server health");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));

    const SCAN_BODY = {
      ok: true, location: "Aurora, MO", total_bids: 2,
      bids: { Aurora: [{ title: "Sidewalk repair", scope: "s", status: "open" },
                       { title: "ADA ramps", scope: "r", status: "open" }] },
      city_coords: { Aurora: { lat: 36.97, lon: -93.72 } },
      center: { lat: 36.97, lon: -93.72, label: "Aurora, MO" },
      debug: { raw_local: 41, kept: 2,
               funnel: { kept: 2, unresolvable_place: 9, out_of_radius: 30 } },
    };
    const HEALTH_BODY = {
      service: "Bid Caller Pro License Server", status: "degraded",
      backends: { openai: true, tavily: true, sam_gov: false, supabase: true,
                  upstash_redis: true, resend_email: true, saved_search_alerts: false },
      local_search: { consecutive_empty_searches: 0, degraded: false,
                      is_sole_local_search: false },
      problems: ["SAM_API_KEY unset — no federal bids in results"],
    };

    await page.route("**/*", (route) => {
      const u = route.request().url();
      const host = new URL(u).hostname;
      if (u.includes("/health")) {
        return route.fulfill({ status: 200, contentType: "application/json",
                               body: JSON.stringify(HEALTH_BODY) });
      }
      if (u.includes("/scan")) {
        return route.fulfill({ status: 200, contentType: "application/json",
                               body: JSON.stringify(SCAN_BODY) });
      }
      if (u.includes("/trial")) {
        return route.fulfill({ status: 200, contentType: "application/json",
                               body: '{"ok":true,"active":true,"days_left":5}' });
      }
      if (u.includes("/mykey")) {
        return route.fulfill({ status: 200, contentType: "application/json",
                               body: '{"ok":false,"reason":"no_key"}' });
      }
      // The Diagnostics card is admin-only (see the isAdmin guard in
      // renderAccount). This block used to sign in as an ordinary user and
      // then assert the card's contents, so it had been failing since the
      // card was gated -- five checks reporting an app fault that was a
      // deliberate access rule.
      if (u.includes("/admin/whoami")) {
        return route.fulfill({ status: 200, contentType: "application/json",
                               body: '{"ok":true,"is_admin":true}' });
      }
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript",
                               body: SB_STUB });
      }
      if (host.endsWith("supabase.co") || host.endsWith("onrender.com")) return route.abort();
      return route.continue();
    });
    await page.addInitScript(seedSignedIn, {});
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(900);
    await page.evaluate(() => { if (typeof bootOffline === "function") bootOffline(); });

    // Run a scan so there is something to report.
    await page.locator('.nav-btn[data-s="scan"]').click();
    await page.fill("#loc-input", "Aurora, MO");
    await page.locator("#scan-btn").click();
    await page.waitForTimeout(1200);

    // The Diagnostics card is admin-only, and this block boots through
    // bootOffline() -- supabase-js never loads, so checkAdminStatus() returns
    // before it can ask the server and isAdmin stays false. Stubbing
    // /admin/whoami cannot help because nothing calls it. Set the flag at the
    // seam instead: what is under test here is the card's CONTENTS, not the
    // access rule that decides who sees it.
    await page.evaluate(() => { isAdmin = true; });
    await page.evaluate(() => goTo("account"));
    await page.waitForTimeout(600);
    // The backend lines come from /health, which the card fetches on demand
    // rather than at render.
    await page.evaluate(async () => {
      if (typeof loadHealth === "function") await loadHealth();
    });
    await page.waitForTimeout(900);

    const card = await page.locator("#account-body").innerText();
    check("the discard count is surfaced, not just the kept count",
      /unresolvable place: 9/.test(card) && /out of radius: 30/.test(card), card.slice(0, 400));
    check("pre-filter total is shown", /41/.test(card));
    check("scan duration is shown", /\d+\.\ds/.test(card));
    check("an unset backend is called out",
      /SAM_API_KEY/.test(card) && /Degraded/i.test(card), card.slice(0, 400));

    const copied = await page.evaluate(() => diagnosticsText());
    check("copyable text includes the funnel", copied.includes("unresolvable_place: 9"), copied);
    check("copyable text includes backend state", copied.includes("sam_gov=OFF"), copied);
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── A /health response that parses but isn't a usable object ──
  // r.json() only throws on unparseable JSON. A response that parses fine but
  // comes back as `null` -- a sleeping Render instance answered by an edge
  // proxy/error page that happens to have a JSON content-type, or the server
  // itself misbehaving -- used to sail through loadHealth() as "success" and
  // then throw on lastHealth.backends, leaving the Diagnostics card stuck on
  // "Checking server..." forever with no way to tell it had already failed.
  console.log("\nA /health body that parses to null doesn't wedge the Diagnostics card");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const u = route.request().url();
      const host = new URL(u).hostname;
      if (u.includes("/health")) {
        return route.fulfill({ status: 200, contentType: "application/json", body: "null" });
      }
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      if (host.endsWith("supabase.co") || host.endsWith("onrender.com")) return route.abort();
      return route.continue();
    });
    await page.addInitScript(seedSignedIn, {});
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(900);
    await page.evaluate(() => { if (typeof bootOffline === "function") bootOffline(); });
    await page.evaluate(() => { isAdmin = true; });
    await page.evaluate(() => goTo("account"));
    await page.waitForTimeout(300);

    let threw = false;
    try {
      await page.evaluate(async () => { await loadHealth(); });
    } catch (e) { threw = true; }
    await page.waitForTimeout(200);

    check("loadHealth() does not throw on a null body", !threw);
    const card = await page.locator("#diag-health").innerText();
    check("the card says so instead of sitting on \"Checking server...\"",
      !/Checking server/i.test(card), card);
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── 8. Saved-search throttling ──
  console.log("\nSaved searches are not re-scanned on every app open");
  {
    async function openWith(checkedAt) {
      const ctx = await browser.newContext(MOBILE_VIEWPORT);
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

  // ── A bid whose status isn't the literal word "Open" ──
  // The server found seven of these, kept them, and reported zero; app.html
  // had the same exact-match bug in its own copy of the rule, so even once the
  // server counted them the feed would still have hidden them.
  console.log("\nBids whose status is phrased differently still show up");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host.endsWith("supabase.co") || host.endsWith("onrender.com")) return route.abort();
      return route.continue();
    });

    const LIVE = ["Open", "open", " Open ", "Accepting Bids", "Active", "Advertised",
                  "Open - Bids Due 12/1/2026", "Currently Open", "Bidding",
                  "Open for Bids", "Issued", "Posted", "", null, undefined];
    const DEAD = ["Closed", "closed", "Bid Closed", "Awarded", "Award to Acme",
                  "Cancelled", "Canceled", "Expired", "Withdrawn", "Archived",
                  "No Longer Accepting", "Not Accepting Bids", "Complete",
                  "Planned", "Upcoming", "Anticipated"];

    const feed = {};
    feed[SEED_CITY] = LIVE.map((s, i) => ({
      ...SEED_BID, title: `Live job ${i}`, status: s,
    }));
    await page.addInitScript(seedSignedIn, { city: SEED_CITY, bid: SEED_BID });
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(1200);

    const verdicts = await page.evaluate(({ live, dead }) => ({
      live: live.map((s) => isOpen({ status: s, deadline: "2026-12-01" })),
      dead: dead.map((s) => isOpen({ status: s, deadline: "2026-12-01" })),
    }), { live: LIVE, dead: DEAD });

    const liveMissed = LIVE.filter((_, i) => !verdicts.live[i]);
    const deadShown = DEAD.filter((_, i) => verdicts.dead[i]);
    check("every way of writing a live bid counts as open",
      liveMissed.length === 0, `hidden: ${JSON.stringify(liveMissed)}`);
    check("every way of writing a dead bid counts as closed",
      deadShown.length === 0, `shown: ${JSON.stringify(deadShown)}`);

    check("a past deadline still closes a bid",
      (await page.evaluate(() =>
        isOpen({ status: "Accepting Bids", deadline: "2019-03-01" }))) === false);

    // The rule has to survive the render path, not just the predicate.
    const shown = await page.evaluate((f) => {
      bidData = f; renderFeed();
      return document.querySelectorAll("#feed-list .bid").length;
    }, feed);
    check("variant-status bids actually render in the feed",
      shown === LIVE.length, `rendered ${shown} of ${LIVE.length}`);

    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── Account has to be reachable by tapping something ──
  // The screen and renderAccount() existed, but nothing navigated to it except
  // an automatic goTo("account") on a 403. Billing, the company profile and the
  // diagnostics card all live there and none of it could be reached on purpose
  // — the test below had to call goTo() directly, which was the tell.
  console.log("\nAccount is reachable from the UI");
  {
    const ctx = await browser.newContext({ viewport: { width: 320, height: 640 } });
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

    const navBtn = page.locator('.nav-btn[data-s="account"]');
    check("there is an Account item in the bottom nav", await navBtn.count() === 1);
    check("it is visible without scrolling", await navBtn.isVisible());

    await navBtn.click();
    await page.waitForTimeout(400);
    check("tapping it opens the Account screen",
      await page.locator("#screen-account").evaluate((el) => el.classList.contains("active")));
    check("it shows as the active tab",
      await navBtn.evaluate((el) => el.classList.contains("active")));

    // Six items on the narrowest phone still in use — labels must not wrap.
    const wrapped = await page.evaluate(() =>
      [...document.querySelectorAll(".nav-btn .lb")]
        .filter((el) => el.getBoundingClientRect().height > 20)
        .map((el) => el.textContent));
    check("no nav label wraps at 320px", wrapped.length === 0, wrapped.join(", "));

    const overflow = await page.evaluate(() => {
      const n = document.querySelector(".bottom-nav");
      return n.scrollWidth - n.clientWidth;
    });
    check("the nav bar does not overflow at 320px", overflow <= 1, `${overflow}px over`);

    // The topbar chip is the other way in; it has to look and behave like one.
    await page.locator('.nav-btn[data-s="scan"]').click();
    await page.waitForTimeout(200);
    const chip = page.locator("#user-chip");
    check("the topbar chip is a real button",
      await chip.evaluate((el) => el.tagName === "BUTTON"));
    const box = await chip.boundingBox();
    check("the chip meets a 44px touch target", box && box.height >= 44,
      box ? `${Math.round(box.height)}px` : "no box");
    check("the chip shows a chevron so it reads as tappable",
      await chip.locator(".chev").count() === 1);
    await chip.click();
    await page.waitForTimeout(400);
    check("the chip also opens Account",
      await page.locator("#screen-account").evaluate((el) => el.classList.contains("active")));

    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── Use My Location must name a town, not a coordinate ──
  console.log("\nAuto-locate shows the nearest town and state");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
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

    const label = (payload) => page.evaluate((d) => placeLabel(d), payload);
    const coarse = (payload) => page.evaluate((d) => coarseLabel(d), payload);

    const inCity = {
      principalSubdivisionCode: "US-MO", city: "Springfield",
      postcode: "65803",
      localityInfo: { administrative: [
        { name: "Greene County", adminLevel: 6 },
        { name: "Springfield", adminLevel: 8 }] },
    };
    check("a town beats the ZIP code",
      (await label(inCity)) === "Springfield, MO", await label(inCity));

    // The case that started this: a rural point outside any municipality.
    const rural = {
      principalSubdivisionCode: "US-MO", city: "", locality: "Township of Rock Prairie",
      postcode: "65714",
      localityInfo: { administrative: [
        { name: "Greene County", adminLevel: 6 },
        { name: "Township of Rock Prairie", adminLevel: 7 }] },
    };
    check("a township is never shown as your location",
      !/township/i.test(await label(rural)), await label(rural));
    // A county must NOT satisfy the first provider. Standing outside Bolivar,
    // BigDataCloud has no municipality and would answer "Polk County", which
    // stopped the chain before the two providers that can say "Bolivar".
    check("a county does not count as naming a town",
      (await label(rural)) === "", await label(rural));
    check("the county is still kept in reserve",
      (await coarse(rural)) === "Greene County, MO", await coarse(rural));

    const smallTown = {
      principalSubdivisionCode: "US-MO", city: "Fair Grove",
      localityInfo: { administrative: [{ name: "Township of Franklin", adminLevel: 7 }] },
    };
    check("a small town still wins over the township it sits in",
      (await label(smallTown)) === "Fair Grove, MO", await label(smallTown));

    check("a ZIP is still better than coordinates when there is no place name",
      (await coarse({ principalSubdivisionCode: "US-MO", postcode: "65714" })) === "65714");
    check("an unusable payload yields nothing, so the next provider is tried",
      (await label({})) === "", JSON.stringify(await label({})));

    // The live failure: BigDataCloud's free endpoint is rate-limited per IP and
    // answers 200 with an error object, which reads as "this place has no name".
    // A coordinate pair landed in the box because nothing else was consulted.
    const RATE_LIMITED = { status: 429, description: "Too many requests" };
    const NOMINATIM = {
      address: { town: "Bolivar", county: "Polk County", state: "Missouri",
                 postcode: "65613" },
    };
    async function withProviders(bdc, nom, overpass) {
      return page.evaluate(async ({ bdc, nom, overpass }) => {
        const real = window.fetchWithTimeout;
        window.fetchWithTimeout = async (url) => {
          const pick = url.includes("bigdatacloud") ? bdc
            : url.includes("nominatim") ? nom : overpass;
          if (pick === null) throw new Error("provider down");
          return { json: async () => pick };
        };
        try { return await resolvePlaceLabel(37.5910, -93.4020); }
        finally { window.fetchWithTimeout = real; }
      }, { bdc, nom, overpass });
    }

    check("a rate-limited first provider falls through to the second",
      (await withProviders(RATE_LIMITED, NOMINATIM, null)) === "Bolivar, MO",
      await withProviders(RATE_LIMITED, NOMINATIM, null));

    check("a first provider that throws does not end the chain",
      (await withProviders(null, NOMINATIM, null)) === "Bolivar, MO");

    const OVERPASS = { elements: [
      { lat: 37.6103, lon: -93.4102, tags: { name: "Bolivar", "addr:state": "MO" } },
      { lat: 37.9500, lon: -93.2900, tags: { name: "Wheatland", "addr:state": "MO" } },
    ] };
    check("with both geocoders dead it uses the nearest real town",
      (await withProviders(RATE_LIMITED, {}, OVERPASS)) === "Bolivar, MO",
      await withProviders(RATE_LIMITED, {}, OVERPASS));

    check("the state carries over from the first provider when OSM omits it",
      (await withProviders(
        { principalSubdivisionCode: "US-MO" }, {},
        { elements: [{ lat: 37.61, lon: -93.41, tags: { name: "Bolivar" } }] })) === "Bolivar, MO");

    check("all three failing returns empty, so callers can say so",
      (await withProviders(null, null, null)) === "");

    // Standing just outside Bolivar: the first provider knows only the county.
    // It must not win while a provider that can name the town is untried.
    const COUNTY_ONLY = {
      principalSubdivisionCode: "US-MO", city: "", locality: "Polk County",
      postcode: "65613",
      localityInfo: { administrative: [{ name: "Polk County", adminLevel: 6 }] },
    };
    check("a county from the first provider does not beat a town from the second",
      (await withProviders(COUNTY_ONLY, NOMINATIM, null)) === "Bolivar, MO",
      await withProviders(COUNTY_ONLY, NOMINATIM, null));
    check("nor does it beat the nearest town from OpenStreetMap",
      (await withProviders(COUNTY_ONLY, {}, OVERPASS)) === "Bolivar, MO",
      await withProviders(COUNTY_ONLY, {}, OVERPASS));
    check("the county is used once no provider can name a town",
      (await withProviders(COUNTY_ONLY, {}, { elements: [] })) === "Polk County, MO",
      await withProviders(COUNTY_ONLY, {}, { elements: [] }));
    check("a ZIP beats nothing when there is not even a county",
      (await withProviders({ principalSubdivisionCode: "US-MO", postcode: "65613" },
        {}, { elements: [] })) === "65613");

    for (const bad of [null, { localityInfo: null }, { localityInfo: { administrative: [null, 7, "x"] } }]) {
      check(`malformed payload does not throw: ${JSON.stringify(bad)}`,
        typeof (await label(bad)) === "string");
    }

    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── /go/<slug> auto-fills the coverage check ──
  // Every outreach email promises "check the count for your own area before
  // paying anything" as a one-click thing. Before this, /go/ only tracked
  // the click -- the visitor still had to retype their own city into the
  // box, even though the slug already names it. Found during a review of
  // what actually happens after the email's link is clicked.
  console.log("\nA /go/<slug> arrival auto-fills and runs the coverage check");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const u = route.request().url();
      const host = new URL(u).hostname;
      if (u.includes("/coverage")) {
        return route.fulfill({
          status: 200, contentType: "application/json",
          body: JSON.stringify({ ok: true, agencies: 104, radius: 125,
            location: "Kansas City, KS", nearest: [] }),
        });
      }
      if (host.endsWith("fonts.googleapis.com") || host.endsWith("fonts.gstatic.com")
          || host.endsWith("cloudflareinsights.com")) {
        return route.abort();
      }
      return route.continue();
    });

    await page.goto(`${BASE}/go/redbird-ks-kc`, { waitUntil: "load" });
    await page.waitForTimeout(600);

    const zipValue = await page.locator("#cov-zip").inputValue();
    const radiusValue = await page.locator("#cov-radius").inputValue();
    const outText = await page.locator("#cov-out").innerText();

    check("the slug's city fills the box", zipValue === "Kansas City, KS", zipValue);
    check("the radius matches what the email states", radiusValue === "125", radiusValue);
    check("the check actually ran, not just filled the box",
      /104 verified agencies/.test(outText), outText);

    // A slug with no entry in go_locations.json must not break the page --
    // it just falls through to the ordinary empty box.
    await page.goto(`${BASE}/go/some-unmapped-slug`, { waitUntil: "load" });
    await page.waitForTimeout(400);
    const unmappedZip = await page.locator("#cov-zip").inputValue();
    check("an unmapped slug leaves the box empty rather than guessing",
      unmappedZip === "", unmappedZip);

    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── The Find map must not snap back while the user is looking around ──
  // Reported as "it snaps me back to an area I clicked when I'm just looking
  // around". A tap moved the map, then the place-name lookup (up to three
  // geocoders, seconds) finished and moved it AGAIN -- by which time the user
  // was often panning elsewhere. Separately, every token refresh re-ran
  // showApp(), which reset the map to its default view. The geocoder is
  // replaced with one the test resolves by hand, so "the name arrives after
  // the user has panned away" is deterministic rather than a timing race.
  console.log("\nThe Find map doesn't snap back while you're looking around");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    // Leaflet from a local install when there is one, so the check doesn't
    // hinge on reaching unpkg; otherwise the real CDN.
    let leafletDir = null;
    try { leafletDir = path.dirname(require.resolve("leaflet/dist/leaflet.js")); } catch (e) {}
    await page.route("**/*", (route) => {
      const u = route.request().url();
      const host = new URL(u).hostname;
      if (host === "unpkg.com" && leafletDir && /\/leaflet\.(js|css)$/.test(u)) {
        const f = path.join(leafletDir, path.basename(u));
        return route.fulfill({ status: 200, body: fs.readFileSync(f),
          contentType: f.endsWith(".css") ? "text/css" : "application/javascript" });
      }
      if (host === "127.0.0.1" || host === "unpkg.com") return route.continue();
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      return route.abort(); // tiles, Overpass, geocoders, backend
    });
    await page.addInitScript(seedSignedIn, {});
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(600);
    await page.evaluate(() => {
      showApp();
      // A geocoder that only answers when the test says so.
      window.__geo = [];
      resolvePlaceLabel = () => new Promise((r) => window.__geo.push(r));
      switchScreen("scan"); // Home is the opening screen; the map lives on Find
    });
    await page.waitForTimeout(600);

    if (!(await page.evaluate(() => typeof findMap !== "undefined" && !!findMap))) {
      console.log("  SKIP  Leaflet unavailable (unpkg unreachable) -- map checks not run");
    } else {
      const center = () => page.evaluate(() => {
        const c = findMap.getCenter(); return { lat: c.lat, lng: c.lng };
      });
      const meters = (a, b) => page.evaluate(([a, b]) => findMap.distance(a, b), [a, b]);

      // Tap the middle of the map, let the fit animation settle.
      const box = await page.locator("#find-map-view").boundingBox();
      await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
      await page.waitForTimeout(700);
      const picked = await center();

      // Pan away by dragging, while the name is still "resolving".
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + 40, box.y + 40, { steps: 8 });
      await page.mouse.up();
      await page.waitForTimeout(400);
      const panned = await center();
      check("the drag actually moved the map", (await meters(picked, panned)) > 5000);

      // Now the name arrives.
      await page.evaluate(() => window.__geo.shift()("Testville, MO"));
      await page.waitForTimeout(700);
      const after = await center();
      check("the late place name does not snap the map back",
        (await meters(after, panned)) < 1000,
        `moved ${Math.round(await meters(after, panned))}m from where the user panned`);
      check("the place name still lands in the box",
        (await page.locator("#loc-input").inputValue()) === "Testville, MO");

      // A token refresh re-enters showApp(); the map must stay put.
      await page.evaluate(() => showApp());
      await page.waitForTimeout(700);
      check("a session refresh does not reset the map view",
        (await meters(await center(), panned)) < 1000);

      // Tap A, then B before A's name returns: A's late answer is stale.
      await page.evaluate(() => {
        window.__geo = [];
        pickFindLocation(38.0, -94.0, null);
        pickFindLocation(39.0, -95.0, null);
      });
      await page.evaluate(() => window.__geo.shift()("Aville, MO"));
      await page.waitForTimeout(200);
      check("a superseded tap's name does not overwrite the newer one",
        (await page.locator("#loc-input").inputValue()) !== "Aville, MO");
      await page.evaluate(() => window.__geo.shift()("Bville, MO"));
      await page.waitForTimeout(200);
      check("the newest tap's name is what ends up in the box",
        (await page.locator("#loc-input").inputValue()) === "Bville, MO");
    }
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── A progress poll that lands after the scan has finished ──
  // The poll was stopped between requests but not mid-request, so one in
  // flight when the scan returned wrote "Reading town bid pages... 10 so
  // far" over the result. A live Columbia, MO scan ended exactly like that:
  // no result, no buttons, a status that looked stuck.
  console.log("\nA late progress update can't overwrite the scan's result");
  {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", async (route) => {
      const u = route.request().url();
      const host = new URL(u).hostname;
      if (u.endsWith("/scan/progress")) {
        // Asked at ~2.5s, answers at ~4.5s -- after the scan's ~3s.
        await new Promise((r) => setTimeout(r, 2000));
        return route.fulfill({ status: 200, contentType: "application/json",
          body: JSON.stringify({ ok: true, known: true, phase: "reading_towns", found: 10, done: false }) });
      }
      if (u.endsWith("/scan")) {
        await new Promise((r) => setTimeout(r, 3000));
        return route.fulfill({ status: 200, contentType: "application/json",
          body: JSON.stringify({ ok: true, location: "Columbia, MO", bids: {}, total_bids: 0, city_coords: {} }) });
      }
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      if (host === "127.0.0.1") return route.continue();
      return route.abort();
    });
    await page.addInitScript(seedSignedIn, {});
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(600);
    await page.evaluate(() => { showApp(); switchScreen("scan"); });
    await page.fill("#loc-input", "Columbia, MO");
    // Not awaited: the first read has to land between the scan's answer
    // and the late poll's.
    await page.evaluate(() => { radius = 75; runScan(false); });
    await page.waitForTimeout(3600);
    const atResult = await page.textContent("#scan-status");
    await page.waitForTimeout(2500); // the late poll has landed by now
    const after = await page.textContent("#scan-status");
    check("the empty result is shown when the scan returns", /Nothing open near Columbia/.test(atResult), atResult);
    check("a poll answered after that doesn't replace it", /Nothing open near Columbia/.test(after), after);
    check("its search-wider button survives too", (await page.locator("#empty-wider").count()) === 1);

    // Desktop: the status sits right under the Scan button, not below the
    // bottom of the much taller map beside it.
    const gap = await page.evaluate(() => {
      const btn = document.getElementById("scan-btn").getBoundingClientRect();
      const st = document.getElementById("scan-status").getBoundingClientRect();
      const map = document.querySelector("#screen-scan .map-card").getBoundingClientRect();
      return { gap: Math.round(st.top - btn.bottom), mapBottom: Math.round(map.bottom), statusTop: Math.round(st.top) };
    });
    check("on desktop the scan status follows the Scan button without a blank gap", gap.gap < 60, JSON.stringify(gap));
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── Home ──
  // The opening screen. Every number on it comes from state the app already
  // holds, so each check here pins a figure to the seed that produced it.
  console.log("\nHome shows what changed since the last visit");
  {
    const REVIEWS = [
      { rating: 5, quote: "Found two city jobs <img src=x onerror=window.__xss=1>", display_name: "Mike R.", company: "R&R Concrete" },
      { rating: 4, quote: "The deadline alerts paid for it.", display_name: "Dana K.", company: "" },
    ];
    // Same shape as SB_STUB, but query builders chain and resolve, so the
    // reviews query has something real to return.
    const SB_REVIEWS_STUB = `
      window.supabase = { createClient: function () {
        return {
          auth: {
            onAuthStateChange: function () { return { data: { subscription: { unsubscribe: function () {} } } }; },
            getSession: async function () { return { data: { session: null } }; },
            signOut: async function () { return {}; }
          },
          from: function (table) {
            var res = table === "reviews" ? { data: ${JSON.stringify(REVIEWS)}, error: null } : { data: null, error: {} };
            var c = {
              select: function () { return c; }, eq: function () { return c; },
              order: function () { return c; }, limit: function () { return c; },
              delete: function () { return c; },
              upsert: async function () { return {}; }, insert: async function () { return {}; },
              maybeSingle: async function () { return { data: null, error: {} }; },
              then: function (ok, bad) { return Promise.resolve(res).then(ok, bad); }
            };
            return c;
          },
          storage: { from: function () { return {}; } }
        };
      } };`;

    const openHome = async (viewport, lastVisitDaysAgo) => {
      const ctx = await browser.newContext(viewport);
      const page = await ctx.newPage();
      const pageErrors = [];
      page.on("pageerror", (e) => pageErrors.push(e.message));
      await page.route("**/*", (route) => {
        const host = new URL(route.request().url()).hostname;
        if (host === "127.0.0.1") return route.continue();
        if (host === "cdn.jsdelivr.net") {
          return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_REVIEWS_STUB });
        }
        return route.abort();
      });
      await page.addInitScript((daysAgo) => {
        const day = 86400000, now = Date.now();
        const iso = (d) => { const t = new Date(now + d * day); return t.getFullYear() + "-" +
          String(t.getMonth() + 1).padStart(2, "0") + "-" + String(t.getDate()).padStart(2, "0"); };
        const lastVisit = daysAgo == null ? null : now - daysAgo * day;
        // Two arrived after the last visit, one before it; one has closed.
        const bids = [
          { title: "Late Ramp Job", deadline: iso(5), status: "open", url: "https://example.gov/b/5", _first_seen: now - 3600000 },
          { title: "Soonest Ramp Job", deadline: iso(2), status: "open", url: "https://example.gov/b/2", _first_seen: now - 7200000 },
          { title: "Far Off Sidewalk", deadline: iso(20), status: "open", url: "https://example.gov/b/20", _first_seen: now - 10 * day },
          { title: "Already Closed", deadline: iso(-3), status: "open", url: "https://example.gov/b/x", _first_seen: now - 10 * day },
        ];
        localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
        localStorage.setItem("last_feed", JSON.stringify({ "Aurora, MO": bids }));
        localStorage.setItem("referral_code", JSON.stringify("tester-a7k2"));
        if (lastVisit != null) localStorage.setItem("last_visit_at", JSON.stringify(lastVisit));
      }, lastVisitDaysAgo);
      await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
      await page.waitForTimeout(600);
      await page.evaluate(() => showApp());
      await page.waitForTimeout(600);
      return { ctx, page, pageErrors };
    };
    const stats = (page) => page.$$eval("#home-main .stat-box", (els) =>
      els.map((e) => [e.querySelector(".l").textContent.trim(), e.querySelector(".n").textContent.trim()]));

    // First visit: no "since", so the headline count is everything open.
    {
      const { ctx, page, pageErrors } = await openHome(MOBILE_VIEWPORT, null);
      const active = await page.evaluate(() => ({
        screen: document.querySelector(".screen.active")?.id,
        nav: document.querySelector(".nav-btn.active")?.dataset.s,
      }));
      check("Home is the screen a signed-in user lands on", active.screen === "screen-home" && active.nav === "home",
            JSON.stringify(active));
      const s = Object.fromEntries(await stats(page));
      check("first visit counts every open bid, not a closed one", s["Open bids"] === "3", JSON.stringify(s));
      check("closing-in-7d counts the 2- and 5-day bids only", s["Closing in 7d"] === "2", JSON.stringify(s));
      const soon = await page.$$eval("#home-soon .bid-title", (els) => els.map((e) => e.textContent.trim()));
      check("closing soon lists the nearest deadline first, closed ones left out",
            soon[0] === "Soonest Ramp Job" && soon[1] === "Late Ramp Job" && !soon.includes("Already Closed"),
            JSON.stringify(soon));
      check("see-all counts the open bids", (await page.textContent("#home-all-btn")).includes("3 open bids"));

      const rev = await page.evaluate(() => {
        const card = document.getElementById("home-reviews");
        return { shown: getComputedStyle(card).display !== "none", n: card.querySelectorAll(".home-review").length,
                 img: !!card.querySelector("img"), xss: !!window.__xss, text: card.textContent };
      });
      check("approved reviews show on Home", rev.shown && rev.n === 2, JSON.stringify(rev));
      check("review text is escaped, not rendered as markup", !rev.img && !rev.xss && rev.text.includes("<img"));

      check("the invite card carries the referral link",
            (await page.textContent("#home-referral-body")).includes("ref=tester-a7k2"));
      await page.evaluate(() => switchScreen("account"));
      await page.waitForTimeout(300);
      const copies = await page.evaluate(() => ({
        home: document.querySelectorAll("#home-referral-body .referral-copy-btn").length,
        acct: document.querySelectorAll("#referral-body .referral-copy-btn").length,
        ids: document.querySelectorAll("#referral-copy-btn").length,
      }));
      check("Home and Account each get their own copy button, no shared id",
            copies.home === 1 && copies.acct === 1 && copies.ids === 0, JSON.stringify(copies));

      await page.evaluate(() => switchScreen("home"));
      await page.click("#home-all-btn");
      check("see-all opens Bids",
            await page.evaluate(() => document.querySelector(".screen.active").id === "screen-feed"));
      check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
      await ctx.close();
    }

    // Back a day later: only the two that arrived since count as new.
    {
      const { ctx, page, pageErrors } = await openHome(MOBILE_VIEWPORT, 1);
      const s = Object.fromEntries(await stats(page));
      check("a return visit counts only bids first seen since", s["New bids"] === "2", JSON.stringify(s));
      check("the subtitle says since when", (await page.textContent("#home-sub")).includes("since yesterday"));
      // A token refresh re-runs showApp; it must not move "since" up to now.
      await page.evaluate(() => showApp());
      await page.waitForTimeout(300);
      const again = Object.fromEntries(await stats(page));
      check("a session refresh doesn't reset what counts as new", again["New bids"] === "2", JSON.stringify(again));
      check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
      await ctx.close();
    }

    // Layout: nothing spills sideways on a small phone; side-by-side on desktop.
    {
      const { ctx, page } = await openHome({ viewport: { width: 320, height: 640 } }, null);
      const over = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      check("Home doesn't scroll sideways at 320px", over <= 0, `overflow ${over}px`);
      await ctx.close();
    }
    {
      const { ctx, page } = await openHome({ viewport: { width: 1440, height: 900 } }, null);
      const r = await page.evaluate(() => {
        const m = document.getElementById("home-main").getBoundingClientRect();
        const s = document.querySelector(".home-side").getBoundingClientRect();
        return { mainRight: m.right, sideLeft: s.left, dTop: Math.abs(m.top - s.top) };
      });
      check("on desktop the reviews/invite column sits beside the bids", r.sideLeft >= r.mainRight && r.dTop < 2,
            JSON.stringify(r));
      await ctx.close();
    }
  }

  // ── Going rates ──
  // Every figure shown must be one MoDOT printed, read from the committed
  // data file -- so the checks compare against that file, not hard-coded
  // numbers that would go stale when next year's book is added.
  console.log("\nGoing rates show MoDOT's real prices, with their range");
  {
    const rates = JSON.parse(fs.readFileSync(path.join(ROOT, "rates", "mo.json"), "utf8"));
    const latest = (item, d) => {
      const by = rates.prices[item][d]; const y = Object.keys(by).sort().pop(); return by[y];
    };
    const money = (n) => "$" + (n >= 1000 ? Math.round(n).toLocaleString("en-US") : n.toFixed(2));
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host === "127.0.0.1") return route.continue();
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      return route.abort();
    });
    await page.addInitScript(() => {
      const bids = [
        { title: "City Hall ADA Ramp", scope: "4 ADA ramps near the city hall entrance", deadline: "2099-01-01", status: "open", url: "https://e.gov/1" },
        { title: "Main St Curb and Gutter", scope: "800 LF curb and gutter", deadline: "2099-01-01", status: "open", url: "https://e.gov/2" },
        { title: "Elm St Sidewalk", scope: "1,200 LF sidewalk, 4 ADA ramps", value: "$120k", deadline: "2099-01-01", status: "open", url: "https://e.gov/3" },
      ];
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
      localStorage.setItem("last_feed", JSON.stringify({ "Aurora, MO": bids, "Topeka, KS": [bids[0]] }));
      localStorage.setItem("price_district", JSON.stringify("SW"));
    });
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(600);
    await page.evaluate(() => showApp());
    await page.waitForTimeout(900);

    const card = await page.textContent("#home-rates");
    const ramp = latest("6081010", "SW");
    check("Home shows the SW curb-ramp average MoDOT printed", card.includes(money(ramp[0])), card.slice(0, 200));
    check("...with its low-high range and bid count beside it",
          card.includes(`${money(ramp[1])}–${money(ramp[2])}`) && card.includes(`${ramp[3]} bids`));
    check("the source is named", /MoDOT Unit Bid Price Books/.test(card));

    await page.selectOption("#home-rate-district", "STATEWIDE");
    await page.waitForTimeout(300);
    const statewide = await page.textContent("#home-rates");
    check("switching district switches the numbers", statewide.includes(money(latest("6081010", "STATEWIDE")[0])));
    check("the district choice is remembered",
          (await page.evaluate(() => localStorage.getItem("price_district"))) === '"STATEWIDE"');
    await page.selectOption("#home-rate-district", "SW");
    await page.waitForTimeout(300);

    await page.evaluate(() => openRates());
    await page.waitForTimeout(400);
    const sheet = await page.textContent("#modal-content");
    const rows = await page.locator("#modal-content .rate-row").count();
    check("the full sheet lists every SW item MoDOT priced",
          rows === Object.keys(rates.prices).filter((i) => rates.prices[i].SW).length, `${rows} rows`);
    check("each year of the trend is shown", rates.years.every((y) => sheet.includes(String(y))));
    const thin = Object.keys(rates.prices).some((i) => rates.prices[i].SW && latest(i, "SW")[3] < 5);
    check("a price on few bids is flagged as a rough guide", !thin || sheet.includes("few bids"));

    const detailFor = async (city, i) => {
      await page.evaluate(([c, n]) => { closeModal(); openDetail(c, bidData[c][n]); }, [city, i]);
      await page.waitForTimeout(400);
      return page.textContent("#detail-rates");
    };
    // The going-rates part only; "Who bids this work" below it quotes real
    // contract descriptions, which can say anything.
    const ratesPart = (t) => t.split("Who bids this work")[0];
    const rampDetail = ratesPart(await detailFor("Aurora, MO", 0));
    check("an ADA ramp bid shows ramp and dome prices",
          rampDetail.includes("Concrete curb ramp") && rampDetail.includes("Truncated domes"), rampDetail.slice(0, 160));
    check("\"near the city hall entrance\" is not read as a driveway job", !rampDetail.includes("driveway"));
    const curbDetail = ratesPart(await detailFor("Aurora, MO", 1));
    check("a curb-and-gutter bid shows curb and gutter, not bare curb",
          curbDetail.includes("Curb and gutter") && !curbDetail.includes("Concrete curb, 6"), curbDetail.slice(0, 160));
    // ── Ballpark: stated quantity x the district's average, nothing guessed ──
    const cg = latest("6091052", "SW")[0], sw = latest("6086004", "SW")[0];
    const whole = (x) => Math.round(x).toLocaleString("en-US");
    const k = (n) => n >= 1000000 ? "$" + (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + "M"
      : n >= 1000 ? "$" + (n / 1000).toFixed(n % 1000 ? 1 : 0) + "k" : "$" + Math.round(n);
    check("curb-and-gutter detail prices 800 ft at the SW average",
          curbDetail.includes(`$${whole(800 * cg)}`), curbDetail.slice(0, 300));
    await page.evaluate(() => { closeModal(); switchScreen("feed"); });
    await page.waitForTimeout(400);
    const cards = await page.$$eval("#feed-list .bid", (els) => els.map((e) => e.textContent));
    const cgCard = cards.find((t) => t.includes("Main St Curb and Gutter")) || "";
    check("the bid card carries the ballpark", cgCard.includes(`\u2248 ${k(800 * cg)} ballpark`), cgCard.slice(0, 200));
    const rampCard = cards.find((t) => t.includes("City Hall ADA Ramp") && !t.includes("Topeka")) || "";
    check("a bid with no quantities gets no ballpark", !/ballpark/.test(rampCard));

    const swDetail = await detailFor("Aurora, MO", 2);
    check("sidewalk in feet uses the stated width assumption",
          swDetail.includes("5\u00a0ft wide (assumed)") && swDetail.includes(`$${whole(1200 * 5 / 9 * sw)}`), swDetail.slice(0, 300));
    check("a ramp count is listed as not counted, not guessed",
          /Not counted: 4 ramps/.test(swDetail));
    check("the posted value is shown beside the ballpark", swDetail.includes("Posted value") && swDetail.includes("$120k"));
    await page.fill("#est-width", "4");
    await page.dispatchEvent("#est-width", "change");
    await page.waitForTimeout(400);
    const sw4 = await page.textContent("#detail-rates");
    check("changing the width recalculates", sw4.includes(`$${whole(1200 * 4 / 9 * sw)}`), sw4.slice(0, 300));
    await page.fill("#est-width", "5");
    await page.dispatchEvent("#est-width", "change");
    await page.waitForTimeout(200);

    const ksDetail = await detailFor("Topeka, KS", 0);
    check("a bid outside Missouri shows no Missouri prices", ksDetail.trim() === "");
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── Other states: each bid priced with its own state's records ──
  console.log("\nGoing rates follow the bid's state; winning bids where the records name them");
  {
    const load = (f) => JSON.parse(fs.readFileSync(path.join(ROOT, f), "utf8"));
    const fl = load("rates/fl.json"), orr = load("rates/or.json");
    const last = (by) => by[Object.keys(by).sort().pop()];
    const money = (n) => "$" + (n >= 1000 ? Math.round(n).toLocaleString("en-US") : n.toFixed(2));
    const flWalk = last(fl.prices[fl.cats.sidewalk].STATEWIDE);
    const orWalk = last(orr.prices[orr.cats.sidewalk].STATEWIDE);
    const orWin = last(orr.wins[orr.cats.sidewalk].STATEWIDE);
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host === "127.0.0.1") return route.continue();
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      return route.abort();
    });
    await page.addInitScript(() => {
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
      localStorage.setItem("last_feed", JSON.stringify({
        "Davie, FL": [{ title: "Davie Sidewalk Repairs", scope: "500 SY concrete sidewalk", deadline: "2099-01-01", status: "open", url: "https://e.gov/fl" }],
        "Salem, OR": [{ title: "Salem Walks", scope: "2,000 SF sidewalk", deadline: "2099-01-01", status: "open", url: "https://e.gov/or" }],
        "Springfield, MO": [{ title: "Springfield Sidewalk", scope: "800 SY sidewalk", deadline: "2099-01-01", status: "open", url: "https://e.gov/mo" }],
      }));
      localStorage.setItem("home_location", JSON.stringify("Davie, FL"));
    });
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(600);
    await page.evaluate(() => showApp());
    await page.waitForTimeout(1200);

    const home = await page.textContent("#home-rates");
    check("Home shows the user's own state's rates", /Going rates in Florida/.test(home), home.slice(0, 120));
    check("...Florida's statewide sidewalk average as FDOT printed it", home.includes(money(flWalk[0])));
    check("...as winning prices, with no range FDOT never published",
          /winning bids/.test(home) && !home.includes(`${money(flWalk[0])}–`));

    await page.evaluate(() => switchScreen("feed"));
    await page.waitForTimeout(1200);
    const cards = await page.$$eval("#feed-list .bid", (els) => els.map((e) => e.textContent));
    const k = (n) => n >= 1000000 ? "$" + (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + "M"
      : n >= 1000 ? "$" + (n / 1000).toFixed(n % 1000 ? 1 : 0) + "k" : "$" + Math.round(n);
    const flCard = cards.find((t) => t.includes("Davie Sidewalk")) || "";
    check("a Florida bid's ballpark uses Florida's price", flCard.includes(`≈ ${k(500 * flWalk[0])} ballpark`), flCard.slice(0, 200));
    const orCard = cards.find((t) => t.includes("Salem Walks")) || "";
    check("an Oregon bid's ballpark uses Oregon's price (per sq ft)", orCard.includes(`≈ ${k(2000 * orWalk[0])} ballpark`), orCard.slice(0, 200));

    await page.evaluate(() => openDetail("Salem, OR", bidData["Salem, OR"][0]));
    await page.waitForTimeout(800);
    const orDetail = await page.textContent("#detail-rates");
    check("Oregon's detail names ODOT and its source", /ODOT/.test(orDetail));
    check("...and shows what the winning bids were",
          orDetail.includes(`Winning bids ${money(orWin[1])}–${money(orWin[2])}`), orDetail.slice(0, 400));

    const id = await page.evaluate(() => bidId("Salem, OR", bidData["Salem, OR"][0]));
    await page.evaluate((i) => openPrep("Salem, OR", i, "pricing"), id);
    await page.waitForTimeout(500);
    const line = await page.evaluate((i) => bidPrep[i].lines[0], id);
    check("Oregon pricing starts at the average winning bid",
          line && line.st === "OR" && line.price === Math.round(orWin[0] * 100) / 100, JSON.stringify(line));
    const sheet = await page.textContent("#prep-body");
    check("the line says where that price sits", /Inside the winning range|winning/.test(sheet), sheet.slice(0, 300));
    await page.fill('.prep-line[data-i="0"] input[data-f="price"]', String(Math.ceil(orWin[2] + 5)));
    await page.waitForTimeout(300);
    check("a price over every winning bid is called out as it's typed",
          /Above every winning bid/.test(await page.textContent('[data-pc="0"]')));

    const moId = await page.evaluate(() => { closeModal(); return bidId("Springfield, MO", bidData["Springfield, MO"][0]); });
    await page.evaluate(() => openDetail("Springfield, MO", bidData["Springfield, MO"][0]));
    await page.waitForTimeout(900);
    const moDetail = await page.textContent("#detail-rates");
    const resPath = path.join(ROOT, "results", "mo.json");
    if (fs.existsSync(resPath)) {
      const res = load("results/mo.json");
      const walkJobs = res.contracts.filter((c) => c.items["6086004"]);
      check("a Missouri bid shows who bids this work", /Who bids this work/.test(moDetail), moDetail.slice(-300));
      check("...naming contractors from MoDOT's bid tabulations",
            walkJobs.some((c) => moDetail.includes(c.bidders[0][0])));
    }
    check("prep for a Missouri bid is unaffected by the others", !!moId);
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // Where a pricing line starts: the average winning bid where the state's
  // records give one, else the average bid. Read from the committed files.
  const startPrice = (item, district) => {
    const rates = JSON.parse(fs.readFileSync(path.join(ROOT, "rates", "mo.json"), "utf8"));
    const resPath = path.join(ROOT, "results", "mo.json");
    const res = fs.existsSync(resPath) ? JSON.parse(fs.readFileSync(resPath, "utf8")) : {};
    const last = (by) => by && by[Object.keys(by).sort().pop()];
    const w = last(((res.wins || {})[item] || {})[district]);
    return Math.round((w ? w[0] : last(rates.prices[item][district])[0]) * 100) / 100;
  };

  // ── Prepare bid: checklist, pricing, summary ──
  console.log("\nPrepare bid keeps the whole bid in the app");
  {
    const swAvg = startPrice("6086004", "SW");
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host === "127.0.0.1") return route.continue();
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      return route.abort();
    });
    await page.addInitScript(() => {
      if (sessionStorage.getItem("seeded")) return;   // keep state across the reload below
      sessionStorage.setItem("seeded", "1");
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
      localStorage.setItem("last_feed", JSON.stringify({ "Aurora, MO": [
        { title: "Elm St Sidewalk", scope: "1,200 LF sidewalk, 4 ADA ramps", value: "$120k",
          deadline: "2099-01-01", status: "open", url: "https://e.gov/3", addenda: true,
          documents: [{ name: "Plans", url: "https://e.gov/plans.pdf" }] }] }));
      localStorage.setItem("price_district", JSON.stringify("SW"));
      localStorage.setItem("company_profile", JSON.stringify({ name: "Test Concrete LLC", contact: "Pat", phone: "417-555-0100" }));
    });
    const boot = async () => {
      await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
      await page.waitForTimeout(600);
      await page.evaluate(() => showApp());
      await page.waitForTimeout(800);
    };
    await boot();
    const id = await page.evaluate(() => bidId("Aurora, MO", bidData["Aurora, MO"][0]));
    await page.evaluate(() => openDetail("Aurora, MO", bidData["Aurora, MO"][0]));
    await page.waitForTimeout(300);
    check("bid details offer Prepare bid", /Prepare bid/.test(await page.textContent("#prep-open")));
    await page.click("#prep-open");
    await page.waitForTimeout(500);

    const checklist = await page.textContent("#prep-body");
    check("the checklist covers the posting's addenda and the submit step",
          /Read every addendum/.test(checklist) && /This bid has addenda/.test(checklist) && /Submit before the deadline/.test(checklist));
    check("the bid's documents are linked from the checklist", /Plans/.test(checklist));
    check("nothing is ticked yet", /0\/\d+/.test(await page.textContent(".prep-tabs")));
    await page.check('[data-check="docs"]');
    await page.waitForTimeout(300);
    check("ticking a step updates progress", /1\/\d+/.test(await page.textContent(".prep-tabs")));
    await page.fill("#prep-new", "Call the bonding company");
    await page.click("#prep-add-btn");
    await page.waitForTimeout(300);
    check("your own step can be added", /Call the bonding company/.test(await page.textContent("#prep-body")));
    check("preparing a bid saves it", await page.evaluate((i) => !!saved[i], id));

    await page.click('[data-step="pricing"]');
    await page.waitForTimeout(400);
    const lines = await page.evaluate((i) => bidPrep[i].lines, id);
    const swLine = lines.find((l) => /sidewalk/i.test(l.name));
    const rampLine = lines.find((l) => /ramp/i.test(l.name));
    check("pricing starts from the ballpark: sidewalk at the SW winning (else average) bid",
          swLine && Math.abs(swLine.qty - 666.7) < 0.1 && swLine.price === swAvg, JSON.stringify(swLine));
    check("the ramp count becomes a line for you to price, not a guess",
          rampLine && rampLine.qty === 4 && rampLine.unit === "each" && rampLine.price === "", JSON.stringify(rampLine));
    // The item is a dropdown: the state's items, other common work, or typed.
    const pick = await page.$$eval('.prep-line[data-i="0"] .pl-pick option', (os) => os.map((o) => o.textContent));
    check("the item is a dropdown of the state's items and other work",
          pick.some((t) => /Concrete sidewalk, 4 in\. \(sq yd\)/.test(t)) && pick.includes("Mobilization (lump sum)") && pick.includes("Other (type it)"));
    check("...with the line's current item selected",
          (await page.$eval('.prep-line[data-i="0"] .pl-pick', (s) => s.value)) === "s:6086004");
    await page.click("#pl-add");
    await page.waitForTimeout(300);
    const newIdx = (await page.evaluate((i) => bidPrep[i].lines.length, id)) - 1;
    await page.selectOption(`.prep-line[data-i="${newIdx}"] .pl-pick`, "s:6091052");
    await page.waitForTimeout(300);
    const picked = await page.evaluate(([i, n]) => bidPrep[i].lines[n], [id, newIdx]);
    check("picking a state item fills its unit and going price",
          picked.name === "Curb and gutter, type B" && picked.unit === "ft" && picked.item === "6091052" && picked.price === startPrice("6091052", "SW"),
          JSON.stringify(picked));
    await page.selectOption(`.prep-line[data-i="${newIdx}"] .pl-pick`, "custom");
    await page.waitForTimeout(300);
    await page.fill(`.prep-line[data-i="${newIdx}"] .pl-name`, "Tree root removal");
    await page.waitForTimeout(200);
    check("\"Other\" lets you type your own item",
          await page.evaluate(([i, n]) => bidPrep[i].lines[n].name === "Tree root removal" && !bidPrep[i].lines[n].item, [id, newIdx]));
    await page.click(`[data-del="${newIdx}"]`);
    await page.waitForTimeout(300);
    const rampIdx = lines.indexOf(rampLine);
    await page.fill(`.prep-line[data-i="${rampIdx}"] input[data-f="price"]`, "2000");
    await page.fill("#pt-mk", "10");
    await page.waitForTimeout(300);
    const expected = (swLine.qty * swLine.price + 4 * 2000) * 1.1;
    const shown = await page.textContent("#pt-total");
    check("the total is quantity x price plus markup", shown === "$" + Math.round(expected).toLocaleString("en-US"), `${shown} vs ${expected}`);

    await page.click('[data-step="summary"]');
    await page.waitForTimeout(300);
    const summary = await page.textContent("#prep-body");
    check("the summary carries company, lines and the total",
          /Test Concrete LLC/.test(summary) && /ADA curb ramp/.test(summary) && summary.includes(expected.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })));
    const text = await page.evaluate((i) => prepSummaryText(prepSummaryData(i, bidData["Aurora, MO"][0], "Aurora, MO")), id);
    check("copy-as-text has the total bid", /TOTAL BID: \$[\d,]+\.\d\d/.test(text) && /Bidder: Test Concrete LLC/.test(text));
    const printed = await page.evaluate(() => {
      let html = ""; const orig = window.open;
      window.open = () => ({ document: { open() {}, write(h) { html += h; }, close() {} } });
      document.getElementById("ps-print").click();
      window.open = orig; return html;
    });
    check("the printable page has the line items and total", /Total bid: \$/.test(printed) && /ADA curb ramp/.test(printed));
    check("printed text is escaped", !/<script>alert/.test(printed));

    await boot();   // a reload: the workspace has to survive it
    const after = await page.evaluate((i) => bidPrep[i], id);
    check("the workspace survives a reload", after && after.checks.docs === true && after.custom[0] === "Call the bonding company" && Number(after.markup) === 10);
    await page.evaluate(() => switchScreen("feed"));
    await page.waitForTimeout(300);
    check("the bid card shows prep progress", /Prep 1\/\d+/.test(await page.textContent("#feed-list")));
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── Phase 2: read the bid form's schedule of items ──
  console.log("\nThe bid form's own items fill the pricing sheet, after review");
  {
    const swAvg = startPrice("6086004", "SW");
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    page.on("dialog", (d) => d.accept());
    let readCalls = 0, readBody = null, failNext = false;
    await page.route("**/*", (route) => {
      const u = route.request().url();
      const host = new URL(u).hostname;
      if (u.endsWith("/bid-documents/read")) {
        readCalls++; readBody = JSON.parse(route.request().postData() || "{}");
        if (failNext) {
          failNext = false;
          return route.fulfill({ status: 200, contentType: "application/json",
            body: JSON.stringify({ ok: false, reason: "fetch_failed", detail: "robots_disallow" }) });
        }
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          ok: true, cached: false, pages_read: 6, truncated: false,
          line_items: [
            { item_no: "1", description: "Mobilization", quantity: 1, unit: "LS" },
            { item_no: "2", description: "4 in. concrete sidewalk", quantity: 1250, unit: "SY" },
            { item_no: "3", description: "Concrete sidewalk", quantity: 1, unit: "LS" },
          ],
          submission: "Sealed envelope to the City Clerk, 100 Main St",
          bid_security: "5% bid bond", prebid: "", required_forms: ["Non-collusion affidavit", "E-Verify affidavit"] }) });
      }
      if (host === "127.0.0.1") return route.continue();
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      return route.abort();
    });
    await page.addInitScript(() => {
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
      localStorage.setItem("last_feed", JSON.stringify({ "Aurora, MO": [
        { title: "Elm St Sidewalk", scope: "Sidewalk replacement", deadline: "2099-01-01", status: "open",
          url: "https://e.gov/3", documents: [{ name: "Bid form", url: "https://e.gov/form.pdf" }] }] }));
      localStorage.setItem("price_district", JSON.stringify("SW"));
    });
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(600);
    await page.evaluate(() => showApp());
    await page.waitForTimeout(800);
    const id = await page.evaluate(() => bidId("Aurora, MO", bidData["Aurora, MO"][0]));
    await page.evaluate((i) => openPrep("Aurora, MO", i, "pricing"), id);
    await page.waitForTimeout(400);
    check("pricing offers to read the bid form", (await page.locator("#pl-read").count()) === 1);

    await page.click("#pl-read");
    await page.waitForTimeout(200);
    failNext = true;
    await page.click('[data-doc="0"]');
    await page.waitForTimeout(500);
    check("a site that blocks robots says so plainly",
          /doesn't allow automated reading/.test(await page.textContent("#prep-body")));
    await page.click("#doc-back");
    await page.waitForTimeout(300);

    await page.click("#pl-read");
    await page.waitForTimeout(200);
    await page.click('[data-doc="0"]');
    await page.waitForTimeout(600);
    check("the server is asked for the document the user picked", readBody && readBody.url === "https://e.gov/form.pdf");
    const review = await page.textContent("#prep-body");
    check("what was read is shown for review first", /3 line items found/.test(review) && /4 in. concrete sidewalk/.test(review));
    check("nothing has replaced the lines yet", await page.evaluate((i) => !bidPrep[i].lines.some((l) => /Mobilization/.test(l.name)), id));
    await page.click("#doc-replace");
    await page.waitForTimeout(400);
    const lines = await page.evaluate((i) => bidPrep[i].lines, id);
    const sy = lines.find((l) => /^2\./.test(l.name)), ls = lines.find((l) => /^3\./.test(l.name));
    check("a sidewalk item in SY gets the SW sidewalk average",
          sy && sy.qty === 1250 && sy.unit === "sq yd" && sy.price === swAvg, JSON.stringify(sy));
    check("the same words in a lump-sum unit get no MoDOT price", ls && ls.unit === "lump sum" && ls.price === "", JSON.stringify(ls));
    check("mobilization comes through to be priced", lines.some((l) => /Mobilization/.test(l.name) && l.unit === "lump sum"));

    await page.click('[data-step="checklist"]');
    await page.waitForTimeout(300);
    const cl = await page.textContent("#prep-body");
    check("the checklist shows the bid security the documents require", /From the bid documents: 5% bid bond/.test(cl));
    check("...how to submit", /Sealed envelope to the City Clerk/.test(cl));
    check("...and the required forms", /Non-collusion affidavit; E-Verify affidavit/.test(cl));
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── Phase 3: your own pricing history ──
  console.log("\nPast bids feed your prices: history, copy, won vs lost");
  {
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    await page.route("**/*", (route) => {
      const host = new URL(route.request().url()).hostname;
      if (host === "127.0.0.1") return route.continue();
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      return route.abort();
    });
    await page.addInitScript(() => {
      const sw = (price) => ({ name: "Concrete sidewalk, 4 in.", item: "6086004", unit: "sq yd", qty: 100, price });
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
      localStorage.setItem("last_feed", JSON.stringify({ "Aurora, MO": [
        { title: "Elm St Sidewalk", scope: "1,200 LF sidewalk, 4 ADA ramps", deadline: "2099-01-01", status: "open", url: "https://e.gov/3" }] }));
      localStorage.setItem("price_district", JSON.stringify("SW"));
      localStorage.setItem("saved", JSON.stringify({
        pastwon: { title: "Oak St Ramps", _city: "Aurora, MO" }, pastlost: { title: "Pine Ave Walks", _city: "Aurora, MO" } }));
      localStorage.setItem("pipeline", JSON.stringify({ pastwon: "won", pastlost: "lost" }));
      localStorage.setItem("bid_prep", JSON.stringify({
        pastwon: { checks: {}, custom: [], markup: 0, addenda: "", updated: 1000,
          lines: [sw(60), { name: "ADA curb ramp", unit: "each", qty: 6, price: 1800 }] },
        pastlost: { checks: {}, custom: [], markup: 0, addenda: "", updated: 2000, lines: [sw(75)] } }));
    });
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(600);
    await page.evaluate(() => showApp());
    await page.waitForTimeout(800);
    const id = await page.evaluate(() => bidId("Aurora, MO", bidData["Aurora, MO"][0]));
    await page.evaluate((i) => openPrep("Aurora, MO", i, "pricing"), id);
    await page.waitForTimeout(400);
    const sheet = await page.textContent("#prep-body");
    check("each line shows your past prices, newest first, with the outcome",
          /Your past prices: \$75\.00 lost · \$60\.00 won/.test(sheet), sheet.slice(0, 400));
    check("the ramp line shows your past ramp price", /\$1,800\.00 won/.test(sheet));

    await page.click("#pl-past");
    await page.waitForTimeout(200);
    check("past bids are listed by name", /Oak St Ramps/.test(await page.textContent("#prep-body")));
    await page.click('[data-prices="pastwon"]');
    await page.waitForTimeout(300);
    const lines = await page.evaluate((i) => bidPrep[i].lines, id);
    check("copy my prices fills matching lines from that bid",
          lines.find((l) => l.item === "6086004").price === 60 && lines.find((l) => /ramp/i.test(l.name)).price === 1800, JSON.stringify(lines));
    const qtyBefore = lines.find((l) => l.item === "6086004").qty;
    check("...without touching this job's quantities", Math.abs(qtyBefore - 666.7) < 0.1);
    await page.click("#pl-past");
    await page.waitForTimeout(200);
    await page.click('[data-lines="pastlost"]');
    await page.waitForTimeout(300);
    const after = await page.evaluate((i) => bidPrep[i].lines, id);
    check("copy lines adds that bid's lines with quantities left blank",
          after.length === lines.length + 1 && after[after.length - 1].qty === "" && after[after.length - 1].price === 75);

    await page.evaluate(() => { closeModal(); openRates(); });
    await page.waitForTimeout(400);
    const rates = await page.textContent("#modal-content");
    check("the rates sheet compares your won and lost prices with MoDOT",
          /Your bids vs MoDOT/.test(rates) && /\$60\.00 \(1\)/.test(rates) && /\$75\.00 \(1\)/.test(rates));
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── Paperwork: fill the agency's form, then bond, concrete, dates ──
  console.log("\nThe agency's form gets filled in, and the next steps write themselves");
  {
    const ctx = await browser.newContext({ ...MOBILE_VIEWPORT, acceptDownloads: true });
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    let fillBody = null;
    await page.route("**/*", (route) => {
      const u = route.request().url();
      const host = new URL(u).hostname;
      if (u.endsWith("/bid-documents/fill")) {
        fillBody = JSON.parse(route.request().postData() || "{}");
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          ok: true, fields: 9, left_blank: 7, pdf_b64: Buffer.from("%PDF-1.4 filled").toString("base64"),
          filled: [{ field: "Bidder", label: "Name of bidder", value: "Test Concrete LLC", key: "company.name" },
                   { field: "Total", label: "Total base bid", value: "75,000.00", key: "total" }] }) });
      }
      if (host === "127.0.0.1") return route.continue();
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      return route.abort();
    });
    await page.addInitScript(() => {
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
      const bid = { title: "Elm St Sidewalk", scope: "Sidewalk replacement", deadline: "2099-11-01", status: "open",
        url: "https://e.gov/3", email: "clerk@e.gov", bid_number: "B-17",
        documents: [{ name: "Bid form", url: "https://e.gov/form.pdf" }] };
      localStorage.setItem("last_feed", JSON.stringify({ "Aurora, MO": [bid] }));
      localStorage.setItem("company_profile", JSON.stringify({ name: "Test Concrete LLC", contact: "Pat Lee", phone: "417-555-0100",
        bid_info: { title: "Owner", address: "1 Main St", bond_agent: "Sam Surety", bond_email: "sam@surety.example", supplier_email: "orders@mix.example" } }));
    });
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(600);
    await page.evaluate(() => showApp());
    await page.waitForTimeout(800);
    const id = await page.evaluate(() => bidId("Aurora, MO", bidData["Aurora, MO"][0]));
    await page.evaluate((i) => {
      const p = prepFor(i);
      p.lines = [{ name: "2. 4 in. concrete sidewalk", qty: 1250, unit: "sq yd", price: 60 },
                 { name: "3. Curb and gutter", qty: 300, unit: "ft", price: 0 }];
      p.addenda = "#1";
      p.docInfo = { source: "Bid form", bid_security: "5% bid bond", prebid: "Mandatory pre-bid meeting October 20, 2099 at 10 AM",
                    questions_due: "Questions due by October 25, 2099", submission: "", required_forms: [] };
      savePrep(i, "Aurora, MO");
      openPrep("Aurora, MO", i, "summary");
    }, id);
    await page.waitForTimeout(400);

    const bond = decodeURIComponent(await page.getAttribute("#ps-bond", "href"));
    check("the bond request goes to the bonding agent", bond.startsWith("mailto:sam@surety.example"), bond.slice(0, 60));
    check("...with the project, the bid and the bond the documents ask for",
          /Hi Sam,/.test(bond) && /Bid due: 2099-11-01/.test(bond) && /Our bid: about \$75,000/.test(bond) && /Bond required: 5% bid bond/.test(bond));
    const q = decodeURIComponent(await page.getAttribute("#ps-question", "href"));
    check("a question to the agency is addressed to its contact", q.startsWith("mailto:clerk@e.gov") && /B-17/.test(q));
    check("every date the documents give is offered for the calendar", /Add 3 dates/.test(await page.textContent("#ps-dates")));
    const [ics] = await Promise.all([page.waitForEvent("download"), page.click("#ps-dates")]);
    const icsText = fs.readFileSync(await ics.path(), "utf8");
    check("...due date, mandatory pre-bid meeting and questions deadline",
          /DTSTART;VALUE=DATE:20991101/.test(icsText) && /MANDATORY pre-bid meeting/.test(icsText) && /DTSTART;VALUE=DATE:20991025/.test(icsText));

    await page.click("#ps-concrete");
    await page.waitForTimeout(300);
    const conc = await page.textContent("#prep-body");
    check("concrete is worked out from area and thickness", conc.includes("138.9 CY"), conc.slice(0, 200));
    check("...with waste added for the order", /146 CY/.test(conc));
    check("...and curb is left to size from the plans, not guessed", /Not counted, size from the plans: 3\. Curb and gutter/.test(conc));
    const quote = decodeURIComponent(await page.getAttribute("#cq-send", "href"));
    check("the quote request goes to the supplier", quote.startsWith("mailto:orders@mix.example") && /about 146 CY/.test(quote));
    await page.click("#cq-back");
    await page.waitForTimeout(300);

    await page.click("#ps-fill");
    await page.waitForTimeout(300);
    await page.click('[data-doc="0"]');
    await page.waitForTimeout(600);
    check("the form filler is sent the company, the prices and the total",
          fillBody && fillBody.url === "https://e.gov/form.pdf" && fillBody.company.name === "Test Concrete LLC"
            && fillBody.company.title === "Owner" && fillBody.bid.total === 75000 && fillBody.bid.addenda === "#1"
            && fillBody.lines[0].item_no === "2" && fillBody.lines[0].unit_price === 60 && fillBody.lines[0].amount === 75000,
          JSON.stringify(fillBody && fillBody.lines));
    const filled = await page.textContent("#prep-body");
    check("every filled box is listed for checking", /Name of bidder/.test(filled) && /75,000\.00/.test(filled) && /7 boxes left blank/.test(filled));
    const [pdf] = await Promise.all([page.waitForEvent("download"), page.click("#ff-download")]);
    check("the filled form downloads", fs.readFileSync(await pdf.path(), "utf8").startsWith("%PDF") && /filled\.pdf$/.test(pdf.suggestedFilename()));

    await page.evaluate(() => { closeModal(); switchScreen("account"); });
    await page.waitForTimeout(500);
    check("Account shows how much of the bid paperwork is saved", /5 of 11 saved/.test(await page.textContent("#screen-account")));
    await page.click("#bi-edit-btn");
    await page.waitForTimeout(300);
    await page.fill('.bi-field[data-k="license"]', "MO-12345");
    await page.click("#bi-save");
    await page.waitForTimeout(300);
    check("bid details save with the company profile",
          await page.evaluate(() => JSON.parse(localStorage.getItem("company_profile")).bid_info.license === "MO-12345"));
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  // ── Win odds: should-I-bid, target price, addenda, did-you-win, fewest bidders ──
  console.log("\nHelping customers win: competition, target price, addenda, outcomes");
  {
    const load = (f) => JSON.parse(fs.readFileSync(path.join(ROOT, f), "utf8"));
    const orRes = load("results/or.json");
    const ctx = await browser.newContext(MOBILE_VIEWPORT);
    const page = await ctx.newPage();
    const pageErrors = [];
    page.on("pageerror", (e) => pageErrors.push(e.message));
    let watchCalls = 0, watchDocs = ["Bid Form"];
    await page.route("**/*", (route) => {
      const u = route.request().url();
      const host = new URL(u).hostname;
      if (u.endsWith("/bid-watch/check")) {
        watchCalls++;
        const urls = JSON.parse(route.request().postData() || "{}").urls || [];
        const results = {};
        urls.forEach((x) => { results[x] = { ok: true, docs: watchDocs, addenda: watchDocs.length > 1 ? [1] : [], hash: "h" + watchDocs.length }; });
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, results }) });
      }
      if (host === "127.0.0.1") return route.continue();
      if (host === "cdn.jsdelivr.net") {
        return route.fulfill({ status: 200, contentType: "application/javascript", body: SB_STUB });
      }
      return route.abort();
    });
    await page.addInitScript(() => {
      if (sessionStorage.getItem("seeded")) return;
      sessionStorage.setItem("seeded", "1");
      localStorage.setItem("last_user_email", JSON.stringify("tester@example.com"));
      localStorage.setItem("license_key", JSON.stringify("TEST-KEY"));
      localStorage.setItem("last_feed", JSON.stringify({
        "Salem, OR": [
          { title: "Salem Walks", scope: "2,000 SF sidewalk", deadline: "2099-01-01", status: "open", url: "https://e.gov/or" },
          { title: "Keizer Ramps", scope: "ADA ramps and sidewalk", deadline: "2099-01-01", status: "open", url: "https://e.gov/or2",
            plan_holders: [{ company: "Other Co" }] },
          { title: "Busy Job", scope: "sidewalk", deadline: "2099-01-01", status: "open", url: "https://e.gov/or3",
            plan_holders: [1, 2, 3, 4, 5, 6, 7].map((i) => ({ company: "Co " + i })) },
        ] }));
      localStorage.setItem("saved", JSON.stringify({
        oldjob: { title: "Oak St Walks", deadline: new Date(Date.now() - 20 * 864e5).toISOString().slice(0, 10),
          url: "https://e.gov/old", _city: "Salem, OR" },
        wonjob: { title: "Pine Ave Ramps", deadline: new Date(Date.now() - 10 * 864e5).toISOString().slice(0, 10),
          url: "https://e.gov/won", _city: "Salem, OR" } }));
      localStorage.setItem("bid_prep", JSON.stringify({
        oldjob: { checks: {}, custom: [], markup: 0, addenda: "", updated: 5,
          lines: [{ name: "Concrete walks", item: "0759-0128000J", st: "OR", unit: "sq ft", qty: 1000, price: 15 }] } }));
    });
    await page.goto(`${BASE}/app.html`, { waitUntil: "load" });
    await page.waitForTimeout(600);
    await page.evaluate(() => showApp());
    await page.waitForTimeout(1500);

    // Should you bid this?
    await page.evaluate(() => openDetail("Salem, OR", bidData["Salem, OR"][0]));
    await page.waitForTimeout(900);
    const odds = await page.textContent("#detail-rates");
    const walkJobs = orRes.contracts.filter((c) => c.items["0759-0128000J"]);
    check("the detail says how many usually bid this work", /Should you bid this\?/.test(odds) && /drew \d+(\.5)? bidders?/.test(odds), odds.slice(0, 300));
    check("...and how close second place comes", /beat second place by a median \d+\.\d%/.test(odds));
    check("...from Oregon's own records, labelled as state jobs", /ODOT bid tabulations/.test(odds) && walkJobs.length > 0);
    await page.evaluate(() => { closeModal(); openDetail("Salem, OR", bidData["Salem, OR"][1]); });
    await page.waitForTimeout(700);
    check("a posting's own plan-holder list is used first", /1 other company<\/b>|1 other company has taken out plans/.test(await page.innerHTML("#detail-rates")));

    // Fewest bidders sort and chips
    await page.evaluate(() => { closeModal(); switchScreen("feed"); });
    await page.waitForTimeout(600);
    await page.selectOption("#feed-sort", "fewest");
    await page.waitForTimeout(400);
    const order = await page.$$eval("#feed-list .bid .bid-title", (els) => els.map((e) => e.textContent));
    check("fewest bidders puts the one-holder job first and the crowded one last",
          order[0] === "Keizer Ramps" && order[order.length - 1] === "Busy Job", order.join(" | "));
    const cards = await page.$$eval("#feed-list .bid", (els) => els.map((e) => e.textContent));
    check("the cards say few bidders / crowded", cards.find((t) => t.includes("Keizer"))?.includes("Few bidders")
          && cards.find((t) => t.includes("Busy Job"))?.includes("Crowded"));

    // Target price
    const id = await page.evaluate(() => bidId("Salem, OR", bidData["Salem, OR"][0]));
    await page.evaluate((i) => openPrep("Salem, OR", i, "pricing"), id);
    await page.waitForTimeout(600);
    const target = await page.textContent("#pt-target");
    check("pricing shows the winning price for these lines", /Winning price for these lines\$[\d,]+–\$[\d,]+/.test(target), target.slice(0, 200));
    await page.fill('.prep-line[data-i="0"] input[data-f="price"]', "200");
    await page.waitForTimeout(300);
    check("...and says when your price is above it, as you type", /You're above that range/.test(await page.textContent("#pt-target")));

    // Addendum alerts
    // Preparing the bid saved it, so it's one of the bids being watched.
    await page.evaluate((i) => { closeModal(); delete bidWatch[i]; }, id);
    await page.evaluate(() => checkSavedBids());
    await page.waitForTimeout(500);
    check("saved open bids are checked, the first time only recorded",
          watchCalls >= 1 && await page.evaluate((i) => !!bidWatch[i] && !bidWatch[i].alert, id));
    watchDocs = ["Bid Form", "Addendum 1"];
    await page.evaluate((i) => { bidWatch[i].at = Date.now() - 7 * 3600e3; }, id);   // last checked 7 hours ago
    await page.evaluate(() => checkSavedBids());
    await page.waitForTimeout(500);
    const card = (await page.$$eval("#feed-list .bid", (els) => els.map((e) => e.textContent))).find((t) => t.includes("Salem Walks")) || "";
    check("a new addendum is flagged on the card", /Addendum 1 posted/.test(card), card.slice(0, 200));
    await page.evaluate(() => openDetail("Salem, OR", bidData["Salem, OR"][0]));
    await page.waitForTimeout(400);
    check("...and in the detail, then marked seen", /Addendum 1 posted/.test(await page.textContent("#modal-content"))
          && await page.evaluate((i) => bidWatch[i].seen === true, id));

    // Did you win?
    await page.evaluate(() => { closeModal(); switchScreen("home"); });
    await page.waitForTimeout(500);
    check("Home asks how a past-due bid went", /How did these go\?/.test(await page.textContent("#home-main")) && /Oak St Walks/.test(await page.textContent("#home-main")));
    await page.click('.outcome[data-oid="oldjob"] [data-out="lost"]');
    await page.waitForTimeout(200);
    await page.fill(".outcome [data-win]", "13500");
    await page.click(".outcome [data-save]");
    await page.waitForTimeout(300);
    check("a loss records the winning bid", await page.evaluate(() => pipeline.oldjob === "lost" && bidPrep.oldjob.result.winning_total === 13500));
    await page.click('.outcome[data-oid="wonjob"] [data-out="won"]');
    await page.waitForTimeout(200);
    check("a win is recorded and asks for a review", await page.evaluate(() => pipeline.wonjob === "won")
          && /Leave a review/.test(await page.textContent("#home-outcomes")));
    await page.evaluate(() => openRates("OR"));
    await page.waitForTimeout(500);
    check("the rates sheet says how far above the winner you were", /you were a median 11\.1% above the winner/.test(await page.textContent("#modal-content")));
    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  await browser.close();
  server.close();
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nAll checks passed");
  process.exit(failures ? 1 : 0);
})();
