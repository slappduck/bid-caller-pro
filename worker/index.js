// CurbCall Pro's Worker. The site itself is static files (curbcall_netlify_v4,
// see wrangler.toml); this script adds two things to them.
//
// fetch: every web request goes straight to the static files, exactly as
//   before this script existed (404 page, _redirects and all).
// email: Cloudflare Email Routing hands this Worker the mail sent to
//   docs+<code>@curbcallpro.com, the address contractors forward plan-room
//   emails to. The raw message goes to the backend (license_server/inbox.py)
//   with a shared secret; the backend keeps the PDFs for that contractor.
//   An address nobody has is bounced.
// fetch also fills the open-bids list on each /concrete-bids/<state>.html
//   page (built by tools/build_bid_pages.py) from the backend's
//   /bids-in-state, cached at Cloudflare's edge for half an hour. A slow or
//   sleeping backend never holds a page up: after BIDS_WAIT_MS the page goes
//   out with its placeholder and a link to the app.
const STATE_PAGE = /^\/concrete-bids\/[a-z-]+\.html$/;
const BIDS_WAIT_MS = 2500;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}
function due(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || "");
  return m ? `due ${MONTHS[Number(m[2]) - 1]} ${Number(m[3])}` : "due date on the posting";
}

// The list a state page shows, from /bids-in-state's answer. null leaves
// the page's own placeholder in place.
export function bidsHTML(d) {
  if (!d || !d.ok || !Array.isArray(d.bids)) return null;
  if (!d.count) {
    return '<p class="note">No open bids found here in the last few weeks. ' +
      '<a href="/app.html">Run a fresh scan for your area, free</a>.</p>';
  }
  const items = d.bids.map((b) =>
    // Titles are stored cut at 140 characters; say so rather than stop mid-word.
    `<li><b>${esc(String(b.title || "").length >= 139 ? String(b.title).replace(/\s+\S*$/, "") + "\u2026" : b.title)}</b><span>${esc(b.town)} &middot; ${esc(due(b.due))}</span></li>`).join("");
  const more = d.count > d.bids.length ? ` and ${d.count - d.bids.length} more` : "";
  return `<p class="note">${d.count} open bid${d.count === 1 ? "" : "s"} our scans found here that may include ` +
    `concrete work${more ? ", soonest due first" : ""}. Read each agency's posting for the full scope.</p>` +
    `<ul class="nb-list">${items}</ul>` +
    (more ? `<p class="note">${esc(more.trim())}.</p>` : "") +
    `<a class="cta" href="/app.html">Open them, get alerts and price them: free to try</a>`;
}

async function bidsInState(env, st) {
  if (!/^[A-Z]{2}$/.test(st)) return null;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), BIDS_WAIT_MS);
  try {
    const r = await fetch(`${env.BACKEND_URL}/bids-in-state?state=${st}`,
      { signal: ctl.signal, cf: { cacheTtl: 1800, cacheEverything: true } });
    return r.ok ? await r.json() : null;
  } catch (e) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

async function statePage(request, env) {
  // Asked fresh every time: a 304 from the static file would leave a
  // browser showing yesterday's list.
  const headers = new Headers(request.headers);
  headers.delete("If-None-Match");
  headers.delete("If-Modified-Since");
  const page = await env.ASSETS.fetch(new Request(request, { headers }));
  if (!page.ok || !(page.headers.get("content-type") || "").includes("text/html")) return page;
  const out = new HTMLRewriter().on("#open-bids", {
    async element(el) {
      const html = bidsHTML(await bidsInState(env, el.getAttribute("data-state") || ""));
      if (html) el.setInnerContent(html, { html: true });
    },
  }).transform(page);
  const res = new Response(out.body, out);
  res.headers.delete("ETag");
  res.headers.delete("Last-Modified");
  res.headers.set("Cache-Control", "public, max-age=600");
  return res;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "GET" && STATE_PAGE.test(url.pathname) && !url.pathname.endsWith("/index.html")) {
      return statePage(request, env);
    }
    return env.ASSETS.fetch(request);
  },

  async email(message, env) {
    if (!env.INBOUND_EMAIL_SECRET) {
      message.setReject("This address isn't accepting mail yet.");
      return;
    }
    if (message.rawSize > 40 * 1024 * 1024) {
      message.setReject("That email is too large. Forward the bid form and plans in separate emails.");
      return;
    }
    const raw = await new Response(message.raw).arrayBuffer();
    const r = await fetch(`${env.BACKEND_URL}/inbound/email`, {
      method: "POST",
      headers: {
        "Content-Type": "message/rfc822",
        "X-Inbound-Secret": env.INBOUND_EMAIL_SECRET,
        "X-Envelope-To": message.to,
        "X-Envelope-From": message.from,
      },
      body: raw,
    });
    if (r.status === 404) {
      message.setReject("There's no CurbCall Pro account with this address.");
      return;
    }
    // Anything else that failed (the backend waking up, a deploy) is thrown
    // so the sending server retries later instead of the mail being lost.
    if (!r.ok) throw new Error(`backend answered ${r.status}`);
  },
};
