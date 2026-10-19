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

const SKIP_REST: &str = "skip-rest";

/// What a link asks for.
#[derive(Debug, PartialEq, Eq)]
pub enum Tap {
    Rate(Signal),
    /// "This article came out broken or incomplete": a report about the delivery, not a rating.
    Problem,
    /// "Mark unrated as skipped": the claim's article is the book's articles, comma-separated.
    SkipRest,
}

impl Tap {
    fn parse(text: &str) -> Option<Tap> {
        match text {
            PROBLEM => Some(Tap::Problem),
            SKIP_REST => Some(Tap::SkipRest),
            _ => text.parse().ok().map(Tap::Rate),
        }
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

/// A link this install signed, for a rating from 1 to 5 (`f4`, `r2`) or "finished" (`done`):
/// books sent before verdicts carry them. Only to say so; nothing is recorded.
fn from_before_verdicts(secret: &[u8], token: &str) -> bool {
    let Some((payload, signature)) = token.split_once('.') else { return false };
    let (Ok(payload), Ok(signature)) = (URL_SAFE_NO_PAD.decode(payload), URL_SAFE_NO_PAD.decode(signature)) else {
        return false;
    };
    if signature.len() != SIGNATURE_BYTES || mac(secret, &payload).verify_truncated_left(&signature).is_err() {
        return false;
    }
    let payload = String::from_utf8_lossy(&payload);
    let signal = payload.split('|').nth(3).unwrap_or_default();
    let old = |kind: char| {
        signal.len() == 2 && signal.starts_with(kind) && ('1'..='5').contains(&signal.chars().nth(1).unwrap())
    };
    old('f') || old('r') || signal == "done"
}

#[derive(Template)]
#[template(path = "feedback.html")]
struct FeedbackPage {
    message: String,
    detail: String,
    /// What the reason links answer, and (token, label) for each reason that fits; none when
    /// nothing fits.
    question: &'static str,
    reasons: Vec<Pick>,
    several: bool,
    /// Every verdict and every progress, the reader's own marked; empty on an error page.
    verdicts: Vec<Pick>,
    progress: Vec<Pick>,
    report: String,
}

/// One link on the tap page. A chosen verdict or progress is shown without a link (tapping it
/// again would change nothing); a chosen reason keeps its link, to take it back.
struct Pick {
    token: String,
    label: &'static str,
    on: bool,
}

fn plain(status: StatusCode, message: &str, detail: &str) -> Page {
    let page = FeedbackPage {
        message: message.to_owned(),
        detail: detail.to_owned(),
        question: "",
        reasons: Vec::new(),
        several: false,
        verdicts: Vec::new(),
        progress: Vec::new(),
        report: String::new(),
    };
    html(status, page)
}

/// "Worth it, finished." "Skipped."
fn stated(rating: &Rating) -> String {
    let (verdict, progress) = (rating.verdict.as_deref(), rating.progress.as_deref());
    let mut message = match (verdict.and_then(ratings::verdict_label), progress) {
        (Some(verdict), Some("skipped")) | (Some(verdict), None) => format!("{verdict}."),
        (Some(verdict), Some(progress)) => {
            format!("{verdict}, {}.", ratings::progress_label(progress).unwrap_or_default().to_lowercase())
        }
        (None, Some(progress)) => format!("{}.", ratings::progress_label(progress).unwrap_or_default()),
        (None, None) => "Nothing recorded.".to_owned(),
    };
    for label in ratings::chosen(&rating.reason).filter_map(ratings::label) {
        message += &format!(" {label}.");
    }
    message
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
        Err(TokenError::Invalid) if from_before_verdicts(secret, &token) => {
            return plain(
                StatusCode::BAD_REQUEST,
                "This book is from before ratings changed.",
                "Its links no longer work: rate it on the web pages instead.",
            );
        }
        Err(TokenError::Invalid) => return plain(StatusCode::BAD_REQUEST, "Link not valid.", ""),
    };
    let pool = &state.pool;
    if !db::pipeline_ready(pool).await? {
        return plain(StatusCode::NOT_FOUND, "Link not valid.", "");
    }
    let signal = match &claim.tap {
        Tap::SkipRest => {
            let articles: Vec<&str> = claim.article.split(',').filter(|id| !id.is_empty()).collect();
            let skipped = db::skip_rest(pool, &claim.user, &articles, SOURCE).await?;
            let message = match skipped {
                0 => "Nothing left to mark: you rated every article.".to_owned(),
                1 => "Marked 1 article skipped.".to_owned(),
                n => format!("Marked {n} articles skipped."),
            };
            return plain(StatusCode::OK, &message, "Rate any of them in the book or on the web pages to change it.");
        }
        Tap::Problem => {
            let title = db::article_title(pool, &claim.article).await?.unwrap_or_default();
            // Once per article, as on the web pages: the Kindle's browser may ask again on
            // its own, and a second tap on a slow screen is the same report.
            if !db::reported(pool, &claim.user).await?.contains(&claim.article) {
                db::report_problem(pool, &claim.user, &claim.article, SOURCE).await?;
            }
            return plain(StatusCode::OK, "Reported. Thank you.", &title);
        }
        Tap::Rate(signal) => signal,
    };
    let title = db::article_title(pool, &claim.article).await?.unwrap_or_default();
    let rating = db::record_feedback(pool, &claim.user, &claim.article, signal, SOURCE).await?;
    let (verdict, progress) = (rating.verdict.as_deref(), rating.progress.as_deref());
    // Same reader, article and expiry as the link that was tapped.
    let sign = |signal: &str| make(secret, &claim.user, &claim.article, signal, claim.expires);
    // One more tap says why. Always offered, never required: the rating already counts.
    let reasons = ratings::reasons_for(verdict)
        .iter()
        .map(|(key, label)| Pick {
            token: sign(&format!("why-{key}")),
            label,
            on: ratings::chosen(&rating.reason).any(|chosen| chosen == *key),
        })
        .collect();
    // Both pick-one groups in full, so the reader sees what they chose among what they could
    // have. A verdict from here keeps progress as it is.
    let verdicts = ratings::VERDICTS
        .iter()
        .map(|(key, label)| Pick { token: sign(key), label, on: verdict == Some(*key) })
        .collect();
    let progress_picks = [
        ("finished", "finished", "Finished"),
        ("partway", "partway", "Stopped partway"),
        ("skipped", "skip", "Skipped: did not read it"),
    ]
    .into_iter()
    .map(|(key, signal, label)| Pick { token: sign(signal), label, on: progress == Some(key) })
    .collect();
    let page = FeedbackPage {
        message: stated(&rating),
        detail: title,
        question: ratings::question(verdict),
        reasons,
        several: ratings::several(verdict),
        verdicts,
        progress: progress_picks,
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
        let claim = verify(SECRET, "MXxqb2hufGExfGVuZC13b3J0aC1pdHwyMDAwMDAwMDAw.r04sH-kavDSO49ow21hpzQ", 0).unwrap();
        let rated = Tap::Rate(Signal::End("worth-it"));
        assert_eq!(claim, Claim { user: "john".into(), article: "a1".into(), tap: rated, expires: 2_000_000_000 });
        let why = verify(SECRET, "MXxqb2hufGExfHdoeS1kZXB0aHwyMDAwMDAwMDAw.wm1E7cJSqcEcRvNc0OMZ6w", 0).unwrap();
        assert_eq!(why.tap, Tap::Rate(Signal::Why("depth".into())));
        let problem = verify(SECRET, "MXxqb2hufGExfHByb2JsZW18MjAwMDAwMDAwMA.21UPHi4dtcYgvhV0QRpvjA", 0).unwrap();
        assert_eq!(problem.tap, Tap::Problem);
        let done = verify(SECRET, "MXxqb2hufGExLGEyfHNraXAtcmVzdHwyMDAwMDAwMDAw.kMn5VzfdrB7rvXbL6_rIqg", 0).unwrap();
        assert_eq!((done.article.as_str(), done.tap), ("a1,a2", Tap::SkipRest));
        assert_eq!(
            make(SECRET, "john", "a1", "end-worth-it", 2_000_000_000),
            "MXxqb2hufGExfGVuZC13b3J0aC1pdHwyMDAwMDAwMDAw.r04sH-kavDSO49ow21hpzQ"
        );
    }

    #[test]
    fn refuses_what_it_did_not_sign_and_what_has_expired() {
        let token = make(SECRET, "john", "a1", "partway", 1000);
        assert!(verify(SECRET, &token, 1000).is_ok());
        assert_eq!(verify(SECRET, &token, 1001), Err(TokenError::Expired));
        assert_eq!(verify(b"another secret, also long enough", &token, 0), Err(TokenError::Invalid));
        let (payload, signature) = token.split_once('.').unwrap();
        // A signature cut short is refused, even though it is a prefix of the right one.
        assert_eq!(verify(SECRET, &format!("{payload}.{}", &signature[..8]), 0), Err(TokenError::Invalid));
        let forged = URL_SAFE_NO_PAD.encode("1|john|a1|end-more|1000");
        assert_eq!(verify(SECRET, &format!("{forged}.{signature}"), 0), Err(TokenError::Invalid));
        for signal in ["up", "done", "f4", "r2", "end-great", "why-broken", "why-topic", ""] {
            assert_eq!(
                verify(SECRET, &make(SECRET, "john", "a1", signal, 1000), 0),
                Err(TokenError::Invalid),
                "{signal}"
            );
        }
        assert_eq!(verify(SECRET, &make(SECRET, "john|x", "a1", "skip", 1000), 0), Err(TokenError::Invalid));
        assert_eq!(verify(SECRET, "garbage", 0), Err(TokenError::Invalid));
        // Signed before verdicts: recognised only to say so.
        assert!(from_before_verdicts(SECRET, &make(SECRET, "john", "a1", "f4", 1000)));
        assert!(!from_before_verdicts(SECRET, &make(SECRET, "john", "a1", "end-more", 1000)));
        assert!(!from_before_verdicts(b"another secret, also long enough", &make(SECRET, "john", "a1", "r2", 1000)));
    }
}
