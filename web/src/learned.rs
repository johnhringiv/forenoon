//! What the pipeline has learned about the reader's taste: a short note it rewrites from their
//! ratings about once a week and gives to the model beside their interests.
//!
//! The note is the pipeline's, not the reader's, but it is theirs to see, correct and roll
//! back. An edit is not a lock: the next rewrite starts from it and may change it. What a
//! reader wants to hold for good belongs in their interests, on the account page.

use std::str::FromStr;

use askama::Template;
use axum::{
    Form,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Redirect},
};
use chrono_tz::Tz;
use serde::Deserialize;
use tower_sessions::Session;

use crate::{
    auth::AuthSession,
    db,
    pages::{AppState, NOT_FROM_HERE, Page, allowed, csrf_token, html, local, reader, refused},
};

const MAX_NOTES: usize = 2000;
const VERSIONS_SHOWN: i64 = 10;

struct Version {
    id: i64,
    when: String,
    who: &'static str,
    notes: String,
    current: bool,
}

#[derive(Template)]
#[template(path = "learned.html")]
struct LearnedPage {
    name: String,
    csrf: String,
    notes: String,
    /// When the pipeline last rewrote it, in words.
    rewritten: Option<String>,
    notice: Option<&'static str>,
    error: Option<&'static str>,
    versions: Vec<Version>,
}

#[derive(Deserialize)]
pub struct LearnedQuery {
    saved: Option<String>,
}

pub async fn page(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    Query(query): Query<LearnedQuery>,
) -> Page {
    let user = reader(&auth)?;
    let zone = Tz::from_str(&user.timezone).unwrap_or(Tz::UTC);
    let day = |stamp: &str| local(stamp, zone).map_or(stamp.to_owned(), |at| at.format("%-d %B %Y").to_string());
    let now = db::learned(&state.pool, &user.id).await?;
    let versions = db::profile_history(&state.pool, &user.id, VERSIONS_SHOWN)
        .await?
        .into_iter()
        .map(|version| Version {
            id: version.id,
            when: day(&version.created_at),
            who: if version.author == "agent" { "Rewritten from your ratings" } else { "Edited by you" },
            current: version.notes == now.notes,
            notes: version.notes,
        })
        .collect();
    let page = LearnedPage {
        name: user.name,
        csrf: csrf_token(&session).await?,
        rewritten: now.learned_at.as_deref().map(day),
        notes: now.notes,
        notice: match query.saved.as_deref() {
            Some("notes") => Some("Saved. It applies from the next run."),
            Some("restored") => Some("Restored. It applies from the next run."),
            _ => None,
        },
        error: (query.saved.as_deref() == Some("long")).then_some("The notes can be at most 2,000 characters."),
        versions,
    };
    html(StatusCode::OK, page)
}

#[derive(Deserialize)]
pub struct NotesForm {
    csrf: String,
    notes: String,
}

/// The reader's own correction of the notes. Kept in the history like every version.
pub async fn save(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<NotesForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let notes: String = form.notes.replace("\r\n", "\n").chars().filter(|c| *c == '\n' || !c.is_control()).collect();
    let notes = notes.trim();
    if notes.chars().count() > MAX_NOTES {
        return Ok(Redirect::to("/learned?saved=long").into_response());
    }
    db::set_learned(&state.pool, &user.id, notes).await?;
    Ok(Redirect::to("/learned?saved=notes").into_response())
}

#[derive(Deserialize)]
pub struct RestoreForm {
    csrf: String,
    version: String,
}

/// Put an earlier version of the reader's own notes back.
pub async fn restore(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<RestoreForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let id = form.version.parse::<i64>().unwrap_or(-1);
    let Some(notes) = db::profile_version(&state.pool, &user.id, id).await? else {
        return refused(StatusCode::NOT_FOUND, "No such version of your notes.");
    };
    db::set_learned(&state.pool, &user.id, &notes).await?;
    Ok(Redirect::to("/learned?saved=restored").into_response())
}
