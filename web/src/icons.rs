//! The favicon, the icons a phone puts on its home screen, and the web app manifest that names
//! them, and the picture a shared link shows. Built from the mark by `tools/make_icons.py`, and
//! built into the server. Public: a browser asks for them before anyone signs in, the sign-in page
//! shows them too, and a chat app fetches the picture without a session.

use std::sync::LazyLock;

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
    ("/og.png", "image/png", SHARE_CARD_PNG),
    ("/manifest.webmanifest", "application/manifest+json", include_bytes!("../icons/manifest.webmanifest")),
];

const SHARE_CARD_PNG: &[u8] = include_bytes!("../icons/og.png");

/// The share card's address as the pages name it, versioned by a hash of the picture: a new
/// card is a new address, so Cloudflare's cache and a chat app's, which keep a card by its
/// address, fetch it again. The plain `/og.png` is served too.
pub static SHARE_CARD: LazyLock<String> = LazyLock::new(|| format!("/og.png?v={:016x}", fnv1a(SHARE_CARD_PNG)));

/// FNV-1a, 64 bits: stable across builds and toolchains, unlike `std`'s hasher.
fn fnv1a(bytes: &[u8]) -> u64 {
    bytes.iter().fold(0xcbf2_9ce4_8422_2325, |hash, &byte| (hash ^ u64::from(byte)).wrapping_mul(0x0100_0000_01b3))
}

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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_share_cards_address_is_versioned_by_its_picture() {
        assert_eq!(fnv1a(b""), 0xcbf2_9ce4_8422_2325);
        assert_eq!(fnv1a(b"a"), 0xaf63_dc4c_8601_ec8c);
        assert_eq!(*SHARE_CARD, format!("/og.png?v={:016x}", fnv1a(SHARE_CARD_PNG)));
    }
}
