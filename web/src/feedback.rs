//! The rating links in delivered books: `/f/<token>`, opened in the Kindle's browser.
//!
//! These are the only pages without a sign-in. A link carries a signed, expiring claim made
//! by the pipeline (`app/tokens.py`): this reader rated this article so. Opening it records
//! the tap, and the page it answers with offers the other choices, signed the same way, so a
//! mis-tap is fixed without going back to the book, and a box for a short note, which posts back
//! to a link of its own.

use askama::Template;
use axum::{
    Form,
    extract::{Path, State},
    http::StatusCode,
    response::{IntoResponse, Redirect},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use hmac::{Hmac, Mac};
use serde::Deserialize;
use sha2::Sha256;

use crate::{
    db::{self, Rating},
    pages::{AppState, Client, Page, html},
    ratings::{Signal, clean_note},
};

const VERSION: &str = "1";
const SIGNATURE_BYTES: usize = 16;
/// Taps allowed from one client address in the limiter's minute.
pub(crate) const TAPS_PER_CLIENT: usize = 30;
const SOURCE: &str = "kindle-footer";
const PROBLEM: &str = "problem";
const NOTE: &str = "note";

/// What a link asks for.
#[derive(Debug, PartialEq, Eq)]
pub enum Tap {
    Rate(Signal),
    /// "This article came out broken or incomplete": a report about the delivery, not a rating.
    Problem,
    /// Where the tap page's note box posts. Opened, it only shows the rating.
    Note,
}

impl Tap {
    fn parse(text: &str) -> Option<Tap> {
        match text {
            PROBLEM => Some(Tap::Problem),
            NOTE => Some(Tap::Note),
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

#[derive(Template)]
#[template(path = "feedback.html")]
struct FeedbackPage {
    message: String,
    detail: String,
    /// Where the note box posts, and the note as it stands; no box when empty (an error page,
    /// or nothing yet for a note to go with).
    note_to: String,
    note: String,
    /// 1 to 5, the reader's own marked; empty on an error page.
    stars: Vec<Star>,
    report: String,
}

/// One link on the tap page. The rating given is shown without a link: tapping it again would
/// change nothing.
struct Star {
    token: String,
    n: i64,
    on: bool,
}

fn plain(status: StatusCode, message: &str, detail: &str) -> Page {
    let page = FeedbackPage {
        message: message.to_owned(),
        detail: detail.to_owned(),
        note_to: String::new(),
        note: String::new(),
        stars: Vec::new(),
        report: String::new(),
    };
    html(status, page)
}

/// "Rated 4 of 5."
fn stated(rating: Option<&Rating>) -> String {
    match rating {
        Some(rating) => format!("Rated {} of 5.", rating.rating),
        None => "Not rated.".to_owned(),
    }
}

#[derive(Deserialize)]
pub struct NoteForm {
    note: String,
}

/// The note box on the tap page: keeps the note, and answers with the same page.
pub async fn note(
    State(state): State<AppState>,
    client: Client,
    Path(token): Path<String>,
    Form(form): Form<NoteForm>,
) -> Page {
    answer(state, client, token, Some(form.note)).await
}

pub async fn tap(State(state): State<AppState>, client: Client, Path(token): Path<String>) -> Page {
    answer(state, client, token, None).await
}

async fn answer(state: AppState, Client(client): Client, token: String, note: Option<String>) -> Page {
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
    let rating = match (&claim.tap, note) {
        (Tap::Note, Some(note)) => {
            // As on the web pages: an unchanged note is not logged again. Then a redirect to
            // the link's own page, so that a reload of it posts nothing a second time.
            let kept = db::get_rating(pool, &claim.user, &claim.article).await?;
            if kept.is_some_and(|kept| kept.note != clean_note(&note)) {
                db::record_note(pool, &claim.user, &claim.article, &note, SOURCE).await?;
            }
            return Ok(Redirect::to(&format!("/f/{token}")).into_response());
        }
        (Tap::Note, None) => db::get_rating(pool, &claim.user, &claim.article).await?,
        (_, Some(_)) => return plain(StatusCode::BAD_REQUEST, "Link not valid.", ""),
        (Tap::Problem, None) => {
            // Once per article, as on the web pages: the Kindle's browser may ask again on
            // its own, and a second tap on a slow screen is the same report.
            if !db::reported(pool, &claim.user).await?.contains(&claim.article) {
                db::report_problem(pool, &claim.user, &claim.article, SOURCE).await?;
            }
            return plain(StatusCode::OK, "Reported. Thank you.", &title);
        }
        (Tap::Rate(signal), None) => {
            Some(db::record_feedback(pool, &claim.user, &claim.article, signal, SOURCE).await?)
        }
    };
    // Same reader, article and expiry as the link that was tapped.
    let sign = |signal: &str| make(secret, &claim.user, &claim.article, signal, claim.expires);
    // The whole row, so the reader sees what they chose among what they could have.
    let stars = (1..=5)
        .map(|n| Star {
            token: sign(&Signal::Rated(n).to_string()),
            n,
            on: rating.as_ref().is_some_and(|r| r.rating == n),
        })
        .collect();
    let page = FeedbackPage {
        message: stated(rating.as_ref()),
        detail: title,
        // A few words say why. Always offered, never required: the rating already counts.
        note_to: if rating.is_some() { sign(NOTE) } else { String::new() },
        note: rating.map(|r| r.note).unwrap_or_default(),
        stars,
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
        let claim = verify(SECRET, "MXxqb2hufGExfHI0fDIwMDAwMDAwMDA.XnSG8SQXXgGPXPUYca5yyg", 0).unwrap();
        let rated = Tap::Rate(Signal::Rated(4));
        assert_eq!(claim, Claim { user: "john".into(), article: "a1".into(), tap: rated, expires: 2_000_000_000 });
        let note = verify(SECRET, "MXxqb2hufGExfG5vdGV8MjAwMDAwMDAwMA.se5olUsJ9d_opk-0gCeA_g", 0).unwrap();
        assert_eq!(note.tap, Tap::Note);
        let problem = verify(SECRET, "MXxqb2hufGExfHByb2JsZW18MjAwMDAwMDAwMA.21UPHi4dtcYgvhV0QRpvjA", 0).unwrap();
        assert_eq!(problem.tap, Tap::Problem);
        assert_eq!(
            make(SECRET, "john", "a1", "r4", 2_000_000_000),
            "MXxqb2hufGExfHI0fDIwMDAwMDAwMDA.XnSG8SQXXgGPXPUYca5yyg"
        );
        // A book sent before October 2026, when the links came in two rows.
        let older = verify(SECRET, "MXxqb2hufGExfGY0fDIwMDAwMDAwMDA.1VbsTqAK0ZRimXeA3yOaUg", 0).unwrap();
        assert_eq!(older.tap, Tap::Rate(Signal::Finished(4)));
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
        let forged = URL_SAFE_NO_PAD.encode("1|john|a1|r5|1000");
        assert_eq!(verify(SECRET, &format!("{forged}.{signature}"), 0), Err(TokenError::Invalid));
        for signal in ["up", "done", "f6", "r0", "x3", "end-more", "worth-it", "skip", "why-thin", "clear", ""] {
            assert_eq!(
                verify(SECRET, &make(SECRET, "john", "a1", signal, 1000), 0),
                Err(TokenError::Invalid),
                "{signal}"
            );
        }
        assert_eq!(verify(SECRET, &make(SECRET, "john|x", "a1", "r1", 1000), 0), Err(TokenError::Invalid));
        assert_eq!(verify(SECRET, "garbage", 0), Err(TokenError::Invalid));
    }
}
