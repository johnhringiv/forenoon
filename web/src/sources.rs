//! The sources page: the feeds and recipes the pipeline reads. Admins only, because every
//! reader on the install shares them.

use askama::Template;
use axum::{
    Form,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Redirect},
};
use serde::Deserialize;
use sqlx::SqlitePool;
use tower_sessions::Session;

use crate::{
    auth::AuthSession,
    db::{self, now_iso},
    pages::{AppError, AppState, NOT_FROM_HERE, Page, allowed, csrf_token, html, reader, refused, web_url},
};

const MAX_NAME: usize = 80;
const MAX_URL: usize = 2000;
const EXTRACT: [(&str, &str); 3] =
    [("auto", "Page, else feed"), ("page", "Always the page"), ("feed", "Only the feed")];

pub struct Source {
    pub id: i64,
    pub name: String,
    pub kind: String,
    pub url: String,
    pub recipe: String,
    pub extract: String,
    pub pinned: bool,
    pub enabled: bool,
}

pub async fn list(pool: &SqlitePool) -> Result<Vec<Source>, sqlx::Error> {
    sqlx::query_as!(
        Source,
        r#"SELECT id AS "id!", name, kind, url, recipe, extract, pinned AS "pinned: bool", enabled AS "enabled: bool"
           FROM sources ORDER BY pinned DESC, name COLLATE NOCASE"#
    )
    .fetch_all(pool)
    .await
}

struct Health {
    source: String,
    failures: i64,
    last_error: String,
    updated_at: String,
}

/// How each source's last runs went, from the pipeline's own record; empty before its first run.
async fn health(pool: &SqlitePool) -> Result<Vec<Health>, sqlx::Error> {
    if !db::pipeline_ready(pool).await? {
        return Ok(Vec::new());
    }
    sqlx::query_as!(Health, r#"SELECT source AS "source!", failures, last_error, updated_at FROM source_health"#)
        .fetch_all(pool)
        .await
}

/// A source as typed into a form, checked.
struct Entry {
    name: String,
    kind: &'static str,
    url: String,
    recipe: String,
    extract: &'static str,
    pinned: bool,
    enabled: bool,
}

#[derive(Deserialize)]
pub struct SourceForm {
    csrf: String,
    name: String,
    /// A feed address, or for a recipe the name of its folder.
    address: String,
    kind: Option<String>,
    extract: Option<String>,
    pinned: Option<String>,
    enabled: Option<String>,
}

fn check(form: &SourceForm) -> Result<Entry, String> {
    let name = form.name.trim();
    if name.is_empty() || name.chars().count() > MAX_NAME || name.chars().any(char::is_control) {
        return Err(format!("A source needs a name of 1 to {MAX_NAME} characters."));
    }
    let address = form.address.trim();
    let recipe = form.kind.as_deref() == Some("recipe");
    if recipe {
        let slug = !address.is_empty()
            && address.len() <= 64
            && address.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-');
        if !slug {
            return Err("A recipe is named by its folder: lowercase letters, digits and '-'.".to_owned());
        }
    } else if address.len() > MAX_URL || web_url(address).is_none() {
        return Err("A feed needs an http(s) address.".to_owned());
    }
    // A recipe's issue is the only copy there is, so it is never fetched again from the site.
    let wanted = if recipe { "feed" } else { form.extract.as_deref().unwrap_or("auto") };
    let extract = EXTRACT.iter().map(|(key, _)| *key).find(|key| *key == wanted).unwrap_or("auto");
    Ok(Entry {
        name: name.to_owned(),
        kind: if recipe { "recipe" } else { "rss" },
        url: if recipe { String::new() } else { address.to_owned() },
        recipe: if recipe { address.to_owned() } else { String::new() },
        extract,
        pinned: form.pinned.is_some(),
        enabled: form.enabled.is_some(),
    })
}

struct Choice {
    key: &'static str,
    label: &'static str,
    selected: bool,
}

struct Row {
    id: i64,
    name: String,
    recipe: bool,
    address: String,
    extract: Vec<Choice>,
    pinned: bool,
    enabled: bool,
    /// How its last run went, in words; `None` before it has been read.
    status: Option<String>,
    failing: bool,
}

#[derive(Template)]
#[template(path = "sources.html")]
struct SourcesPage {
    name: String,
    csrf: String,
    rows: Vec<Row>,
    summary: String,
    error: Option<String>,
    notice: Option<&'static str>,
}

fn choices(current: &str) -> Vec<Choice> {
    EXTRACT.iter().map(|(key, label)| Choice { key, label, selected: *key == current }).collect()
}

async fn render(
    state: &AppState,
    auth: &AuthSession,
    session: &Session,
    status: StatusCode,
    error: Option<String>,
    notice: Option<&'static str>,
) -> Page {
    let user = reader(auth)?;
    let health = health(&state.pool).await?;
    let sources = list(&state.pool).await?;
    let (total, paused) = (sources.len(), sources.iter().filter(|source| !source.enabled).count());
    let rows: Vec<Row> = sources
        .into_iter()
        .map(|source| {
            let seen = health.iter().find(|entry| entry.source == source.name);
            let status = seen.map(|entry| match entry.failures {
                0 => format!("Read without trouble, {}", entry.updated_at.get(..10).unwrap_or("")),
                n => format!("Failed {n} run{} in a row: {}", if n == 1 { "" } else { "s" }, entry.last_error),
            });
            Row {
                id: source.id,
                recipe: source.kind == "recipe",
                address: if source.kind == "recipe" { source.recipe } else { source.url },
                extract: choices(&source.extract),
                pinned: source.pinned,
                enabled: source.enabled,
                failing: seen.is_some_and(|entry| entry.failures > 0),
                status,
                name: source.name,
            }
        })
        .collect();
    let failing = rows.iter().filter(|row| row.failing && row.enabled).count();
    let summary = format!("{total} source{}, {paused} paused, {failing} failing.", if total == 1 { "" } else { "s" });
    let page = SourcesPage { name: user.name, csrf: csrf_token(session).await?, rows, summary, error, notice };
    html(status, page)
}

/// Everything here is for admins; anyone else is told so.
fn admin(auth: &AuthSession) -> Result<bool, AppError> {
    Ok(reader(auth)?.is_admin())
}

const ADMINS_ONLY: &str = "Sources are managed by an admin.";

#[derive(Deserialize)]
pub struct PageQuery {
    saved: Option<String>,
}

pub async fn page(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    Query(query): Query<PageQuery>,
) -> Page {
    if !admin(&auth)? {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    let notice = match query.saved.as_deref() {
        Some("added") => Some("Source added. It is read from the next run."),
        Some("saved") => Some("Saved."),
        Some("removed") => Some("Source removed. Articles already delivered from it are kept."),
        _ => None,
    };
    render(&state, &auth, &session, StatusCode::OK, None, notice).await
}

/// The checked form, or the response that refuses it.
async fn accept(
    state: &AppState,
    auth: &AuthSession,
    session: &Session,
    headers: &HeaderMap,
    form: &SourceForm,
) -> Result<Result<Entry, axum::response::Response>, AppError> {
    if !admin(auth)? {
        return Ok(Err(refused(StatusCode::FORBIDDEN, ADMINS_ONLY)?));
    }
    if !allowed(headers, session, &form.csrf).await? {
        return Ok(Err(refused(StatusCode::FORBIDDEN, NOT_FROM_HERE)?));
    }
    match check(form) {
        Ok(entry) => Ok(Ok(entry)),
        Err(problem) => Ok(Err(render(state, auth, session, StatusCode::BAD_REQUEST, Some(problem), None).await?)),
    }
}

fn name_taken(error: &sqlx::Error) -> bool {
    error.as_database_error().is_some_and(|e| e.is_unique_violation())
}

pub async fn add(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<SourceForm>,
) -> Page {
    let entry = match accept(&state, &auth, &session, &headers, &form).await? {
        Ok(entry) => entry,
        Err(response) => return Ok(response),
    };
    let now = now_iso();
    let done = sqlx::query!(
        "INSERT INTO sources (name, kind, url, recipe, extract, pinned, enabled, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        entry.name,
        entry.kind,
        entry.url,
        entry.recipe,
        entry.extract,
        entry.pinned,
        now
    )
    .execute(&state.pool)
    .await;
    match done {
        Ok(_) => Ok(Redirect::to("/sources?saved=added").into_response()),
        Err(error) if name_taken(&error) => {
            let problem = Some(format!("There is already a source named {}.", entry.name));
            render(&state, &auth, &session, StatusCode::BAD_REQUEST, problem, None).await
        }
        Err(error) => Err(error.into()),
    }
}

pub async fn update(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(form): Form<SourceForm>,
) -> Page {
    let entry = match accept(&state, &auth, &session, &headers, &form).await? {
        Ok(entry) => entry,
        Err(response) => return Ok(response),
    };
    let done = sqlx::query!(
        "UPDATE sources SET name = ?, kind = ?, url = ?, recipe = ?, extract = ?, pinned = ?, enabled = ? WHERE id = ?",
        entry.name,
        entry.kind,
        entry.url,
        entry.recipe,
        entry.extract,
        entry.pinned,
        entry.enabled,
        id
    )
    .execute(&state.pool)
    .await;
    match done {
        Ok(_) => Ok(Redirect::to("/sources?saved=saved").into_response()),
        Err(error) if name_taken(&error) => {
            let problem = Some(format!("There is already a source named {}.", entry.name));
            render(&state, &auth, &session, StatusCode::BAD_REQUEST, problem, None).await
        }
        Err(error) => Err(error.into()),
    }
}

#[derive(Deserialize)]
pub struct DeleteForm {
    csrf: String,
}

pub async fn delete(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(form): Form<DeleteForm>,
) -> Page {
    if !admin(&auth)? {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    sqlx::query!("DELETE FROM sources WHERE id = ?", id).execute(&state.pool).await?;
    Ok(Redirect::to("/sources?saved=removed").into_response())
}
