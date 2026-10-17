//! The landing page: forenoon.dev, served by this program at one hostname. It is built into
//! the binary from `web/site`, one page with its styles inline and its pictures beside it, so
//! the public site is the same deploy as the demo behind it and needs nothing else running.
//! Nothing but the page, its pictures, the icons and the health check answers on that host.
//! When the deploy is down, a Cloudflare Worker (`deploy/cloudflare`) shows a page saying so.

use std::sync::Arc;

use axum::{
    extract::{Request, State},
    http::{HeaderValue, Method, header},
    middleware::Next,
    response::{IntoResponse, Response},
};

use crate::{demo::hosts, icons, pages};

const PAGE: &str = include_str!("../site/index.html");
/// (path, contents): the pictures the page names, all WebP.
const IMAGES: &[(&str, &[u8])] = &[
    ("/images/kindle-cover.webp", include_bytes!("../site/images/kindle-cover.webp")),
    ("/images/kindle-contents.webp", include_bytes!("../site/images/kindle-contents.webp")),
    ("/images/kindle-article.webp", include_bytes!("../site/images/kindle-article.webp")),
    ("/images/kindle-code.webp", include_bytes!("../site/images/kindle-code.webp")),
    ("/images/newsstand-light.webp", include_bytes!("../site/images/newsstand-light.webp")),
    ("/images/newsstand-dark.webp", include_bytes!("../site/images/newsstand-dark.webp")),
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

/// Served from the binary, dressed as every response is, and kept by a cache in front for a
/// while: the page briefly, since it changes with a release; a picture for a day.
fn file(media_type: &'static str, body: &'static [u8], cache: &'static str) -> Response {
    let response = ([(header::CONTENT_TYPE, media_type), (header::CACHE_CONTROL, cache)], body).into_response();
    let mut response = pages::secured(response);
    // Unlike every other page, this one is meant to be found.
    response.headers_mut().remove("x-robots-tag");
    response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static(cache));
    response
}

/// Outermost: on the landing host, the page and its pictures are answered here, the icons and
/// the health check by the router as usual, and anything else does not exist. Every other host
/// passes through untouched.
pub async fn gate(State(landing): State<Arc<str>>, request: Request, next: Next) -> Response {
    let ours = hosts(&request).is_some_and(|(host, bare)| *host == *landing || *bare == *landing);
    if !ours {
        return next.run(request).await;
    }
    let path = request.uri().path();
    let looking = request.method() == Method::GET || request.method() == Method::HEAD;
    if looking && path == "/" {
        return file("text/html; charset=utf-8", PAGE.as_bytes(), "public, max-age=300");
    }
    if looking && let Some((_, body)) = IMAGES.iter().find(|(name, _)| *name == path) {
        return file("image/webp", body, "public, max-age=86400");
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
    fn the_page_names_only_pictures_that_are_built_in() {
        for name in PAGE.split("images/").skip(1).map(|rest| rest.split('"').next().unwrap()) {
            assert!(IMAGES.iter().any(|(path, _)| *path == format!("/images/{name}")), "{name}");
        }
    }
}
