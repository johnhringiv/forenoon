// A Cloudflare Worker on forenoon.dev and its subdomains. Every request goes to the origin
// behind the tunnel as usual. When the origin cannot answer, the landing page is served from
// a copy held at the edge (the static assets in wrangler.toml: web/site, bundled at deploy),
// with a line saying the demo behind its links is away; a persona host gets a short notice.
// The origin is asked for the encodings the visitor accepts (toOrigin).
// Everything else, the pages' own errors included, passes through untouched. Deployed with
// wrangler.toml beside this file; see docs/demo.md.

// The landing page's icons and share card, from web/icons (the rules in wrangler.toml bundle
// them): the assets folder is web/site alone.
import favicon from "../../web/icons/favicon.ico";
import iconSvg from "../../web/icons/icon.svg";
import appleTouchIcon from "../../web/icons/apple-touch-icon.png";
import og from "../../web/icons/og.png";

const ICONS = {
  "/favicon.ico": [favicon, "image/x-icon"],
  "/icon.svg": [iconSvg, "image/svg+xml"],
  "/apple-touch-icon.png": [appleTouchIcon, "image/png"],
  "/og.png": [og, "image/png"],
};

// What counts as the origin being down, not the application failing: the tunnel or the
// container gone (Cloudflare answers the fetch with one of its 52x or 530 statuses rather than
// throwing), or a gateway in front of the pages with no live backend (502 to 504). A 500 is
// the application's own and is shown as it is.
const OUTAGE = new Set([502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530]);
const LANDING_HOST = "forenoon.dev";

export default {
  async fetch(request, env) {
    let response;
    try {
      response = await fetch(toOrigin(request));
    } catch {
      return outage(request, env);
    }
    return OUTAGE.has(response.status) ? outage(request, env) : response;
  },
};

// Cloudflare asks the origin for "br, gzip" whatever the visitor accepts, and the origin's
// pages say no-transform, so Cloudflare cannot re-encode them: a visitor without Brotli was
// sent the page uncompressed. The origin is asked instead for what the visitor accepts, of
// the two encodings Cloudflare takes from an origin, and its answer passes through as it is.
// Cloudflare overrides this header on its own unless the zone's Cache Rule sets Respect Strong
// ETags (docs/demo.md), which forwards it.
function toOrigin(request) {
  const accepted = request.cf?.clientAcceptEncoding;
  if (accepted === undefined) {
    return request;
  }
  const ours = accepted
    .split(",")
    .map((part) => part.trim())
    .filter((part) => /^(br|gzip)\s*(;|$)/i.test(part));
  const headers = new Headers(request.headers);
  headers.set("accept-encoding", ours.join(", "));
  return new Request(request, { headers });
}

async function outage(request, env) {
  const url = new URL(request.url);
  if (url.hostname === LANDING_HOST) {
    const icon = ICONS[url.pathname];
    if (icon) {
      return new Response(icon[0], { headers: { "content-type": icon[1], "cache-control": "no-store" } });
    }
    // The page and its pictures, from the edge's copy; the page with its notice.
    const asset = await env.ASSETS.fetch(request);
    if (!asset.ok) {
      return asset;
    }
    const headers = new Headers(asset.headers);
    headers.set("cache-control", "no-store");
    if (url.pathname !== "/") {
      // A picture keeps its 200: a browser drops an image that comes with any other status.
      return new Response(asset.body, { status: asset.status, headers });
    }
    // The page itself goes out as a 503, so a monitor sees the outage while a reader sees
    // the page; browsers render a 503's body as they would a 200's.
    headers.set("retry-after", "120");
    const notice = `<p class="demo" style="max-width:1120px;margin:.8rem auto 0;padding:0 20px;font:.95rem/1.5 system-ui,sans-serif;color:#8a3b12">The demo behind the links below is away for a few minutes: the machine that runs it is not answering. This page is served from Cloudflare's copy.</p>`;
    return new HTMLRewriter()
      .on("header.mast", { element(header) { header.after(notice, { html: true }); } })
      .transform(new Response(asset.body, { status: 503, headers }));
  }
  // A persona host, or the admin's: a page for a browser going somewhere, a bare status for
  // a picture or a script it asked for.
  const mode = request.headers.get("sec-fetch-mode");
  const navigating = mode ? mode === "navigate" : (request.headers.get("accept") || "").includes("text/html");
  const headers = { "retry-after": "120", "cache-control": "no-store" };
  if (!navigating) {
    return new Response("Service Unavailable", { status: 503, headers });
  }
  return new Response(NOTICE, { status: 503, headers: { ...headers, "content-type": "text/html; charset=utf-8" } });
}

const NOTICE = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>The demo is not answering</title>
<style>
:root { color-scheme:light dark; --bg:#f6f4ef; --ink:#1c1b19; --soft:#6b675f; --accent:#8a3b12 }
@media (prefers-color-scheme:dark) { :root { --bg:#161513; --ink:#ece8df; --soft:#a39e93; --accent:#e0a074 } }
body { margin:0; min-height:100vh; display:grid; place-items:center; padding:1.5rem; background:var(--bg); color:var(--ink);
  font:18px/1.6 Georgia,"Times New Roman",serif }
main { max-width:30rem }
.plate { display:inline-block; font-size:1.6rem; font-weight:700; text-transform:uppercase; letter-spacing:.16em;
  border-top:1.5px solid; border-bottom:4px double; padding:.12em 0 .1em .16em; margin-bottom:1.4rem }
h1 { font-size:1.6rem; line-height:1.2; margin:0 0 .6em }
p { margin:0 0 .8em }
.small { font:.9rem/1.5 system-ui,sans-serif; color:var(--soft) }
a { color:var(--accent) }
</style>
</head>
<body>
<main>
<div class="plate">Forenoon</div>
<h1>The demo is away for a moment.</h1>
<p>The machine that serves these pages is not answering. It is usually back within a few minutes; this page goes away by itself when it is.</p>
<p class="small">Meanwhile: <a href="https://forenoon.dev/">what Forenoon is</a>, and <a href="https://github.com/johnhringiv/forenoon">the source</a>.</p>
</main>
</body>
</html>`;
