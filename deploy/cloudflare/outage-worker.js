// A Cloudflare Worker on forenoon.dev: when the origin behind the tunnel cannot answer, a
// visitor gets a page in Forenoon's own voice instead of Cloudflare's error screen. Every
// other answer, the pages' own errors included, passes through untouched. Deployed with
// wrangler.toml beside this file; see docs/demo.md.

// What counts as the origin being down, not the application failing: the tunnel or the
// container gone (Cloudflare answers the fetch with one of its 52x or 530 statuses rather than
// throwing), or a gateway in front of the pages with no live backend (502 to 504). A 500 is
// the application's own and is shown as it is.
const OUTAGE = new Set([502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 530]);

export default {
  async fetch(request) {
    let response;
    try {
      response = await fetch(request);
    } catch {
      return outage(request);
    }
    return OUTAGE.has(response.status) ? outage(request) : response;
  },
};

function outage(request) {
  // A page for a browser going somewhere; a bare status for a picture or a script it asked for.
  const mode = request.headers.get("sec-fetch-mode");
  const navigating = mode ? mode === "navigate" : (request.headers.get("accept") || "").includes("text/html");
  const headers = { "retry-after": "120", "cache-control": "no-store" };
  if (!navigating) {
    return new Response("Service Unavailable", { status: 503, headers });
  }
  return new Response(PAGE, { status: 503, headers: { ...headers, "content-type": "text/html; charset=utf-8" } });
}

const PAGE = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forenoon is not answering</title>
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
<h1>The press is stopped for a moment.</h1>
<p>The machine that serves these pages is not answering. It is usually back within a few minutes; this page will go away by itself when it is.</p>
<p class="small">The project itself is unaffected: <a href="https://github.com/johnhringiv/forenoon">github.com/johnhringiv/forenoon</a>.</p>
</main>
</body>
</html>`;
