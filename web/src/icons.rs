//! The favicon, the icons a phone puts on its home screen, and the web app manifest that names
//! them. Built from the mark by `web/icons/make.py`, and built into the server. Public: a browser
//! asks for them before anyone signs in, and the sign-in page shows them too.

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
    ("/manifest.webmanifest", "application/manifest+json", include_bytes!("../icons/manifest.webmanifest")),
];

/// Kept for a day: the addresses never change, so a new icon shows within a day of a release.
pub fn serve(media_type: &'static str, body: &'static [u8]) -> Response {
    ([(header::CONTENT_TYPE, media_type), (header::CACHE_CONTROL, "public, max-age=86400")], body).into_response()
}
