//! The landing page: forenoon.dev, served by this program at one hostname. It is built into
//! the binary from `web/site`, one page with its styles inline and its pictures beside it, so
//! the public site is the same deploy as the demo behind it and needs nothing else running.
//! Nothing but the page, its pictures, the icons, the crawl files and the health check answers
//! on that host. The sitemap lists the page and the demo hosts' pages, and every one of those
//! hosts serves the robots.txt that names it.
//! When the deploy is down, a Cloudflare Worker (`deploy/cloudflare`) shows a page saying so.

use std::sync::{Arc, LazyLock};

use axum::{
    extract::{Request, State},
    http::{HeaderValue, Method, header},
    middleware::Next,
    response::{IntoResponse, Response},
};

use crate::{
    demo::{Persona, hosts},
    icons, pages,
};

/// The page, naming the share card by its versioned address (`icons::SHARE_CARD`). The outage
/// Worker's copy keeps the plain `/og.png`, which is served too.
static PAGE: LazyLock<String> =
    LazyLock::new(|| include_str!("../site/index.html").replace("/og.png\"", &format!("{}\"", *icons::SHARE_CARD)));
/// (path, contents): the pictures the page names, all WebP.
const IMAGES: &[(&str, &[u8])] = &[
    ("/images/kindle-cover.webp", include_bytes!("../site/images/kindle-cover.webp")),
    ("/images/kindle-contents.webp", include_bytes!("../site/images/kindle-contents.webp")),
    ("/images/kindle-article.webp", include_bytes!("../site/images/kindle-article.webp")),
    ("/images/kindle-code.webp", include_bytes!("../site/images/kindle-code.webp")),
    ("/images/newsstand-light.webp", include_bytes!("../site/images/newsstand-light.webp")),
    ("/images/newsstand-dark.webp", include_bytes!("../site/images/newsstand-dark.webp")),
    ("/images/phone-light.webp", include_bytes!("../site/images/phone-light.webp")),
    ("/images/phone-dark.webp", include_bytes!("../site/images/phone-dark.webp")),
];

/// The hostname the page answers at, lowercase and without a default port, as browsers send
/// it (`FORENOON_WEB_LANDING`). `None` when the option is empty: an ordinary install.
pub fn host(text: &str) -> Result<Option<String>, String> {
    let host = text.trim().to_ascii_lowercase();
    if host.is_empty() {
        return Ok(None);
    }
    let host = host.strip_suffix(":443").or_else(|| host.strip_suffix(":80")).map_or(host.clone(), str::to_owned);
    let plain = host.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | ':' | '[' | ']'));
    if !plain {
        return Err(format!("{host}: not a hostname; give it without a scheme or a path"));
    }
    Ok(Some(host))
}

/// What the landing host serves besides the page: its address, and the crawl files, which
/// cover the demo hosts too (`FORENOON_WEB_DEMO`).
pub struct Landing {
    host: String,
    /// The demo hosts, where robots.txt is answered here as well.
    personas: Vec<String>,
    sitemap: String,
    robots: String,
}

impl Landing {
    /// `host` as `host` returns it. The addresses are https: the public site is served no other way.
    pub fn new(host: String, personas: &[Persona]) -> Landing {
        let mut pages = vec![format!("https://{host}/")];
        for persona in personas {
            pages.extend(["/", "/archive", "/sources"].map(|path| format!("https://{}{path}", persona.host)));
        }
        // A hostname has nothing XML would need escaped (`host` and `demo::parse` check).
        let urls: String = pages.iter().map(|page| format!("  <url><loc>{page}</loc></url>\n")).collect();
        let sitemap = format!(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n\
             <urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\">\n{urls}</urlset>\n"
        );
        // Named from each demo host's robots.txt, a sitemap may list that host's pages too.
        // Thumbnails are other sites' pictures, not ours to have indexed.
        let robots = format!("User-agent: *\nDisallow: /thumb/\n\nSitemap: https://{host}/sitemap.xml\n");
        Landing { personas: personas.iter().map(|persona| persona.host.clone()).collect(), host, sitemap, robots }
    }
}

/// Served from the binary, dressed as every response is, and kept by a cache in front for a
/// while: the page briefly, since it changes with a release; a picture for a day.
fn file(media_type: &'static str, body: impl IntoResponse, cache: &'static str) -> Response {
    let response = ([(header::CONTENT_TYPE, media_type), (header::CACHE_CONTROL, cache)], body).into_response();
    let mut response = pages::secured(response);
    // Unlike every other page, this one is meant to be found.
    response.headers_mut().remove("x-robots-tag");
    response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static(cache));
    response
}

/// Outermost: on the landing host, the page, its pictures and the sitemap are answered here,
/// the icons and the health check by the router as usual, and anything else does not exist.
/// robots.txt is answered here on the demo hosts too. Every other request passes through
/// untouched.
pub async fn gate(State(landing): State<Arc<Landing>>, request: Request, next: Next) -> Response {
    let Some((host, bare)) = hosts(&request) else { return next.run(request).await };
    let names = |name: &String| *name == host || *name == bare;
    let ours = names(&landing.host);
    let path = request.uri().path();
    let looking = request.method() == Method::GET || request.method() == Method::HEAD;
    if looking && path == "/robots.txt" && (ours || landing.personas.iter().any(names)) {
        return file("text/plain; charset=utf-8", landing.robots.clone(), "public, max-age=300");
    }
    if !ours {
        return next.run(request).await;
    }
    if looking && path == "/sitemap.xml" {
        return file("application/xml", landing.sitemap.clone(), "public, max-age=300");
    }
    if looking && path == "/" {
        return file("text/html; charset=utf-8", PAGE.as_bytes(), "public, max-age=300");
    }
    if looking && let Some((_, body)) = IMAGES.iter().find(|(name, _)| *name == path) {
        return file("image/webp", *body, "public, max-age=86400");
    }
    if looking && (path == "/healthz" || icons::FILES.iter().any(|(icon, _, _)| *icon == path)) {
        return next.run(request).await;
    }
    pages::secured(pages::not_found().await.unwrap_or_else(IntoResponse::into_response))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_landing_host_is_optional_and_loses_a_default_port() {
        assert_eq!(host(""), Ok(None));
        assert_eq!(host(" Forenoon.Example:443 "), Ok(Some("forenoon.example".to_owned())));
        assert_eq!(host("forenoon.example:8081"), Ok(Some("forenoon.example:8081".to_owned())));
        assert!(host("https://forenoon.example").is_err());
    }

    #[test]
    fn the_sitemap_lists_the_page_and_each_demo_hosts_pages() {
        let personas = crate::demo::parse("mara.forenoon.example=mara").unwrap();
        let landing = Landing::new("forenoon.example".to_owned(), &personas);
        let locs: Vec<&str> =
            landing.sitemap.split("<loc>").skip(1).map(|rest| rest.split('<').next().unwrap()).collect();
        assert_eq!(
            locs,
            [
                "https://forenoon.example/",
                "https://mara.forenoon.example/",
                "https://mara.forenoon.example/archive",
                "https://mara.forenoon.example/sources",
            ]
        );
        assert!(landing.robots.contains("Sitemap: https://forenoon.example/sitemap.xml\n"));
    }

    #[test]
    fn the_page_names_its_share_card_by_the_versioned_address() {
        assert!(PAGE.contains(&format!("content=\"https://forenoon.dev{}\"", *icons::SHARE_CARD)));
        assert!(!PAGE.contains("/og.png\""));
    }

    #[test]
    fn the_page_names_only_pictures_that_are_built_in() {
        for name in PAGE.split("images/").skip(1).map(|rest| rest.split('"').next().unwrap()) {
            assert!(IMAGES.iter().any(|(path, _)| *path == format!("/images/{name}")), "{name}");
        }
    }
}
