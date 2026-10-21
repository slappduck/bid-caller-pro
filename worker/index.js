// CurbCall Pro's Worker. The site itself is static files (curbcall_netlify_v4,
// see wrangler.toml); this script only exists for email.
//
// fetch: every web request goes straight to the static files, exactly as
//   before this script existed (404 page, _redirects and all).
// email: Cloudflare Email Routing hands this Worker the mail sent to
//   docs+<code>@curbcallpro.com, the address contractors forward plan-room
//   emails to. The raw message goes to the backend (license_server/inbox.py)
//   with a shared secret; the backend keeps the PDFs for that contractor.
//   An address nobody has is bounced.
export default {
  async fetch(request, env) {
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
