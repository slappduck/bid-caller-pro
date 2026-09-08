// The accessibility audit, runnable on demand.
//
// Serves curbcall_netlify_v4 locally and runs axe-core over every page at a
// phone viewport, reporting WCAG 2.1 A and AA violations. It found four real
// ones the first time: pinch-zoom disabled across the whole app (critical),
// an unlabelled radius select on the marketing page (critical), body text at
// 2.71:1 against its own panel, and a link distinguishable only by colour.
//
// Not part of the test suite -- it needs a browser and a downloaded copy of
// axe-core. tests/test_accessibility.py pins the specific regressions instead,
// cheaply and deterministically. Run this when changing layout or colour.
//
//   curl -sS -o /tmp/axe.min.js \
//     https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js
//   AXE=/tmp/axe.min.js NODE_PATH=$(npm root -g) node tools/a11y_audit.js
//
const http = require("http");
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const ROOT = path.join(__dirname, "..", "curbcall_netlify_v4");
const AXE = fs.readFileSync(process.env.AXE || "/tmp/axe.min.js", "utf8");
const PAGES = ["index.html", "app.html", "terms.html", "privacy.html",
               "legal/index.html", "404.html"];
const TYPES = {".html":"text/html",".css":"text/css",".js":"text/javascript",
               ".json":"application/json",".png":"image/png",".svg":"image/svg+xml",
               ".ico":"image/x-icon",".webmanifest":"application/manifest+json"};

const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split("?")[0]).replace(/^\//, "") || "index.html";
  const file = path.join(ROOT, rel);
  if (!file.startsWith(ROOT) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404); return res.end("nope");
  }
  res.writeHead(200, { "Content-Type": TYPES[path.extname(file)] || "text/plain" });
  res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(r => server.listen(0, r));
  const base = `http://127.0.0.1:${server.address().port}/`;
  const browser = await chromium.launch({
    executablePath: process.env.PW_CHROME || undefined });
  const out = {};
  for (const page of PAGES) {
    const p = await browser.newPage({ viewport: { width: 390, height: 844 } });
    p.on("pageerror", () => {});
    try {
      await p.goto(base + page, { waitUntil: "domcontentloaded", timeout: 20000 });
      await p.waitForTimeout(1200);
      await p.addScriptTag({ content: AXE });
      const r = await p.evaluate(async () => await window.axe.run(document, {
        runOnly: { type: "tag", values: ["wcag2a","wcag2aa","wcag21a","wcag21aa"] }
      }));
      out[page] = r.violations.map(v => ({
        id: v.id, impact: v.impact, help: v.help, n: v.nodes.length,
        sample: v.nodes.slice(0, 6).map(n => ({
          target: (n.target || []).join(" "),
          html: (n.html || "").slice(0, 90),
          data: (n.any || []).map(a => a.data)
        }))
      }));
    } catch (e) { out[page] = [{ id: "LOAD_ERROR", impact: "n/a", help: String(e).slice(0,200), n: 0, sample: [] }]; }
    await p.close();
  }
  await browser.close();
  server.close();
  console.log(JSON.stringify(out, null, 1));
})();
