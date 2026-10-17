//! The favicon, the icons a phone puts on its home screen, and the web app manifest that names
//! them, and the picture a shared link shows. Built from the mark by `tools/make_icons.py`, and
//! built into the server. Public: a browser asks for them before anyone signs in, the sign-in page
//! shows them too, and a chat app fetches the picture without a session.

use axum::{
    http::header,
    response::{IntoResponse, Response},
};

/// (path, media type, contents).
pub const FILES: &[(&str, &str, &[u8])] = &[
    ("/favicon.ico", "image/x-icon", include_bytes!("../icons/favicon.ico")),
    ("/icon.svg", "image/svg+xml", include_bytes!("../icons/icon.svg")),
    ("/apple-touch-icon.png", "image/png", include_bytes!("../icons/apple-touch-icon.png")),
    ("/icon-192.png", "image/png", include_bytes!("../icons/icon-192.png")),
    ("/icon-512.png", "image/png", include_bytes!("../icons/icon-512.png")),
    ("/icon-maskable-512.png", "image/png", include_bytes!("../icons/icon-maskable-512.png")),
    ("/og.png", "image/png", include_bytes!("../icons/og.png")),
    ("/manifest.webmanifest", "application/manifest+json", include_bytes!("../icons/manifest.webmanifest")),
];

/// The share card is shown on other sites, by a chat's or a preview tool's page: its response
/// says so, where every other one of ours refuses (`pages::secured`).
const SHARED: &[&str] = &["/og.png"];

/// Kept for a day: the addresses never change, so a new icon shows within a day of a release.
pub fn serve(path: &'static str, media_type: &'static str, body: &'static [u8]) -> Response {
    let policy = if SHARED.contains(&path) { "cross-origin" } else { "same-origin" };
    (
        [
            (header::CONTENT_TYPE, media_type),
            (header::CACHE_CONTROL, "public, max-age=86400"),
            (header::HeaderName::from_static("cross-origin-resource-policy"), policy),
        ],
        body,
    )
        .into_response()
}
