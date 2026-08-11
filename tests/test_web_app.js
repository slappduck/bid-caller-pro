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
  const browser = await chromium.launch(process.env.PW_CHROME ? { executablePath: process.env.PW_CHROME } : {});

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
    const ctx = await browser.newContext();
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
    const ctx = await browser.newContext();
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
    const ctx = await browser.newContext();
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
    const ctx = await browser.newContext();
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
    const ctx = await browser.newContext();
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

    await page.evaluate(() => goTo("account"));
    await page.waitForTimeout(1200);

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

  // ── 8. Saved-search throttling ──
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

  // ── A bid whose status isn't the literal word "Open" ──
  // The server found seven of these, kept them, and reported zero; app.html
  // had the same exact-match bug in its own copy of the rule, so even once the
  // server counted them the feed would still have hidden them.
  console.log("\nBids whose status is phrased differently still show up");
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

    const label = (payload) =>
      page.evaluate((d) => placeLabel(d, 37.1043, -93.4021), payload);

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
    check("it falls back to the county, which does let curb work",
      (await label(rural)) === "Greene County, MO", await label(rural));

    const smallTown = {
      principalSubdivisionCode: "US-MO", city: "Fair Grove",
      localityInfo: { administrative: [{ name: "Township of Franklin", adminLevel: 7 }] },
    };
    check("a small town still wins over the township it sits in",
      (await label(smallTown)) === "Fair Grove, MO", await label(smallTown));

    check("a ZIP is still better than coordinates when there is no place name",
      (await label({ principalSubdivisionCode: "US-MO", postcode: "65714" })) === "65714");
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

    for (const bad of [null, { localityInfo: null }, { localityInfo: { administrative: [null, 7, "x"] } }]) {
      check(`malformed payload does not throw: ${JSON.stringify(bad)}`,
        typeof (await label(bad)) === "string");
    }

    check("no uncaught page errors", pageErrors.length === 0, pageErrors.join(" | "));
    await ctx.close();
  }

  await browser.close();
  server.close();
  console.log(failures ? `\n${failures} check(s) FAILED` : "\nAll checks passed");
  process.exit(failures ? 1 : 0);
})();
