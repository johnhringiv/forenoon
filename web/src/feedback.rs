//! The rating links in delivered books: `/f/<token>`, opened in the Kindle's browser.
//!
//! These are the only pages without a sign-in. A link carries a signed, expiring claim made
//! by the pipeline (`app/tokens.py`): this reader rated this article so. Opening it records
//! the tap, and the page it answers with offers the other choices, signed the same way, so a
//! mis-tap is fixed without going back to the book.

use askama::Template;
use axum::{
    extract::{Path, State},
    http::StatusCode,
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use sha2::Sha256;

use crate::{
    db::{self, Rating},
    pages::{AppState, Client, Page, html},
    ratings::{self, Signal},
};

const VERSION: &str = "1";
const SIGNATURE_BYTES: usize = 16;
/// Taps allowed from one client address in the limiter's minute.
pub(crate) const TAPS_PER_CLIENT: usize = 30;
const SOURCE: &str = "kindle-footer";
const PROBLEM: &str = "problem";

/// What a link asks for.
#[derive(Debug, PartialEq, Eq)]
pub enum Tap {
    /// A signal in the words `Signal` writes; `done`, from a book delivered before ratings,
    /// still counts.
    Rate(Signal),
    /// "This article came out broken or incomplete": a report about the delivery, not a rating.
    Problem,
}

impl Tap {
    fn parse(text: &str) -> Option<Tap> {
        if text == PROBLEM { Some(Tap::Problem) } else { text.parse().ok().map(Tap::Rate) }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct Claim {
    pub user: String,
    pub article: String,
    pub tap: Tap,
    /// Unix seconds.
    pub expires: i64,
}

#[derive(Debug, PartialEq, Eq)]
pub enum TokenError {
    Invalid,
    Expired,
}

fn mac(secret: &[u8], payload: &[u8]) -> Hmac<Sha256> {
    let mut mac = Hmac::<Sha256>::new_from_slice(secret).expect("HMAC takes a key of any length");
    mac.update(payload);
    mac
}

/// A link's token, as `app/tokens.py` makes them.
pub fn make(secret: &[u8], user: &str, article: &str, signal: &str, expires: i64) -> String {
    let payload = [VERSION, user, article, signal, &expires.to_string()].join("|");
    let signature = mac(secret, payload.as_bytes()).finalize().into_bytes();
    format!("{}.{}", URL_SAFE_NO_PAD.encode(&payload), URL_SAFE_NO_PAD.encode(&signature[..SIGNATURE_BYTES]))
}

pub fn verify(secret: &[u8], token: &str, now: i64) -> Result<Claim, TokenError> {
    let (payload, signature) = token.split_once('.').ok_or(TokenError::Invalid)?;
    let payload = URL_SAFE_NO_PAD.decode(payload).map_err(|_| TokenError::Invalid)?;
    let signature = URL_SAFE_NO_PAD.decode(signature).map_err(|_| TokenError::Invalid)?;
    // The length first: a truncated check would accept a shorter signature as a prefix.
    if signature.len() != SIGNATURE_BYTES {
        return Err(TokenError::Invalid);
    }
    mac(secret, &payload).verify_truncated_left(&signature).map_err(|_| TokenError::Invalid)?;
    let payload = String::from_utf8(payload).map_err(|_| TokenError::Invalid)?;
    let [version, user, article, signal, expires] =
        payload.split('|').collect::<Vec<_>>().try_into().map_err(|_| TokenError::Invalid)?;
    let tap = Tap::parse(signal).ok_or(TokenError::Invalid)?;
    let expires = expires.parse::<i64>().map_err(|_| TokenError::Invalid)?;
    if version != VERSION {
        return Err(TokenError::Invalid);
    }
    if expires < now {
        return Err(TokenError::Expired);
    }
    Ok(Claim { user: user.to_owned(), article: article.to_owned(), tap, expires })
}

#[derive(Template)]
#[template(path = "feedback.html")]
struct FeedbackPage {
    message: String,
    detail: String,
    /// (token, label) for each reason that fits the rating; empty when there is no rating.
    reasons: Vec<(String, &'static str)>,
    several: bool,
    /// ("Finished:", [(token, 1), ...]) and the same for not finished; empty on an error page.
    change: Vec<(&'static str, Vec<(String, i64)>)>,
    report: String,
}

fn plain(status: StatusCode, message: &str, detail: &str) -> Page {
    let page = FeedbackPage {
        message: message.to_owned(),
        detail: detail.to_owned(),
        reasons: Vec::new(),
        several: false,
        change: Vec::new(),
        report: String::new(),
    };
    html(status, page)
}

pub async fn tap(State(state): State<AppState>, Client(client): Client, Path(token): Path<String>) -> Page {
    if !state.taps.allow(&client, TAPS_PER_CLIENT) {
        return plain(StatusCode::TOO_MANY_REQUESTS, "Too many requests.", "Try again in a minute.");
    }
    let Some(secret) = state.feedback_secret.as_deref() else {
        return plain(StatusCode::NOT_FOUND, "Link not valid.", "");
    };
    let claim = match verify(secret, &token, chrono::Utc::now().timestamp()) {
        Ok(claim) => claim,
        Err(TokenError::Expired) => return plain(StatusCode::BAD_REQUEST, "Link not valid.", "This link has expired."),
        Err(TokenError::Invalid) => return plain(StatusCode::BAD_REQUEST, "Link not valid.", ""),
    };
    let pool = &state.pool;
    if !db::pipeline_ready(pool).await? {
        return plain(StatusCode::NOT_FOUND, "Link not valid.", "");
    }
    let title = db::article_title(pool, &claim.article).await?.unwrap_or_default();
    let signal = match &claim.tap {
        Tap::Problem => {
            // Once per article, as on the web pages: the Kindle's browser may ask again on
            // its own, and a second tap on a slow screen is the same report.
            if !db::reported(pool, &claim.user).await?.contains(&claim.article) {
                db::report_problem(pool, &claim.user, &claim.article, SOURCE).await?;
            }
            return plain(StatusCode::OK, "Reported. Thank you.", &title);
        }
        Tap::Rate(signal) => signal,
    };
    let Rating { rating, finished, reason } =
        db::record_feedback(pool, &claim.user, &claim.article, signal, SOURCE).await?;
    let mut message = match rating {
        Some(n) => format!("Rated {n} of 5, "),
        None => "Not rated, ".to_owned(),
    };
    message += if finished { "finished." } else { "not finished." };
    for label in ratings::chosen(&reason).filter_map(ratings::label) {
        message += &format!(" {label}.");
    }
    // Same reader, article and expiry as the link that was tapped.
    let sign = |signal: &str| make(secret, &claim.user, &claim.article, signal, claim.expires);
    // One more tap says why. Always offered, never required: the rating already counts.
    let reasons = match rating {
        Some(_) => {
            ratings::reasons_for(rating).iter().map(|(key, label)| (sign(&format!("why-{key}")), *label)).collect()
        }
        None => Vec::new(),
    };
    let row = |kind: char| (1..=5).map(|n| (sign(&format!("{kind}{n}")), n)).collect();
    let page = FeedbackPage {
        message,
        detail: title,
        reasons,
        several: rating.is_some_and(|n| n >= 4),
        change: vec![("Finished:", row('f')), ("Not finished:", row('r'))],
        // A delivery fault goes to whoever runs the install; it is not part of the rating.
        report: sign(PROBLEM),
    };
    html(StatusCode::OK, page)
}

#[cfg(test)]
mod tests {
    use super::*;

    const SECRET: &[u8] = b"kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk";

    #[test]
    fn verifies_tokens_made_by_the_pipeline() {
        // Made by `app/tokens.py`; `tests/test_core.py` checks it makes the same strings.
        let claim = verify(SECRET, "MXxqb2hufGExfGY0fDIwMDAwMDAwMDA.1VbsTqAK0ZRimXeA3yOaUg", 0).unwrap();
        let rated = Tap::Rate(Signal::Rated { rating: 4, finished: true });
        assert_eq!(claim, Claim { user: "john".into(), article: "a1".into(), tap: rated, expires: 2_000_000_000 });
        let why = verify(SECRET, "MXxqb2hufGExfHdoeS1kZXB0aHwyMDAwMDAwMDAw.wm1E7cJSqcEcRvNc0OMZ6w", 0).unwrap();
        assert_eq!(why.tap, Tap::Rate(Signal::Why("depth".into())));
        let problem = verify(SECRET, "MXxqb2hufGExfHByb2JsZW18MjAwMDAwMDAwMA.21UPHi4dtcYgvhV0QRpvjA", 0).unwrap();
        assert_eq!(problem.tap, Tap::Problem);
        assert_eq!(
            make(SECRET, "john", "a1", "f4", 2_000_000_000),
            "MXxqb2hufGExfGY0fDIwMDAwMDAwMDA.1VbsTqAK0ZRimXeA3yOaUg"
        );
    }

    #[test]
    fn refuses_what_it_did_not_sign_and_what_has_expired() {
        let token = make(SECRET, "john", "a1", "r2", 1000);
        assert!(verify(SECRET, &token, 1000).is_ok());
        assert_eq!(verify(SECRET, &token, 1001), Err(TokenError::Expired));
        assert_eq!(verify(b"another secret, also long enough", &token, 0), Err(TokenError::Invalid));
        let (payload, signature) = token.split_once('.').unwrap();
        // A signature cut short is refused, even though it is a prefix of the right one.
        assert_eq!(verify(SECRET, &format!("{payload}.{}", &signature[..8]), 0), Err(TokenError::Invalid));
        let forged = URL_SAFE_NO_PAD.encode("1|john|a1|f5|1000");
        assert_eq!(verify(SECRET, &format!("{forged}.{signature}"), 0), Err(TokenError::Invalid));
        for signal in ["up", "f6", "x3", "why-broken", "r", ""] {
            assert_eq!(
                verify(SECRET, &make(SECRET, "john", "a1", signal, 1000), 0),
                Err(TokenError::Invalid),
                "{signal}"
            );
        }
        assert_eq!(verify(SECRET, &make(SECRET, "john|x", "a1", "f1", 1000), 0), Err(TokenError::Invalid));
        assert_eq!(verify(SECRET, "garbage", 0), Err(TokenError::Invalid));
    }
}
