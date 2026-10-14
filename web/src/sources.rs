//! The sources page: what the pipeline reads, and the subscriber sessions it reads with.
//! Built-in sources are front pages the pipeline knows how to read and are only switched on or
//! off; feeds are added by address; a session is a site's cookie, used for every page on it.
//! Admins only, because every reader on the install shares them.

use std::{
    io::Write,
    os::unix::fs::OpenOptionsExt,
    path::{Path as FilePath, PathBuf},
};

use askama::Template;
use axum::{
    Form,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Redirect},
};
use serde::{Deserialize, de::DeserializeOwned};
use sqlx::SqlitePool;
use tower_sessions::Session;

use crate::{
    auth::AuthSession,
    db::{self, now_iso},
    pages::{AppError, AppState, NOT_FROM_HERE, Page, allowed, csrf_token, html, reader, refused, web_url},
};

const MAX_NAME: usize = 80;
const MAX_URL: usize = 2000;
const MAX_COOKIE: usize = 16 * 1024;
const MAX_FILE: u64 = 4 * 1024 * 1024;
const EXTRACT: [(&str, &str); 4] = [
    ("auto", "Best of page and feed"),
    ("page", "Web page only"),
    ("feed", "Feed only"),
    ("link", "Nowhere: show picks as links"),
];

pub struct Source {
    pub id: i64,
    pub name: String,
    pub kind: String,
    pub url: String,
    /// For a built-in source, the pipeline's key for it.
    pub builtin: String,
    pub extract: String,
    pub pinned: bool,
    pub enabled: bool,
    /// "" (never checked), "pending", "ok" or "failed": the pipeline's check of the address.
    pub check_state: String,
    pub check_note: String,
}

pub async fn list(pool: &SqlitePool) -> Result<Vec<Source>, sqlx::Error> {
    sqlx::query_as!(
        Source,
        r#"SELECT id AS "id!", name, kind, url, builtin, extract, pinned AS "pinned: bool",
           enabled AS "enabled: bool", check_state, check_note
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

/// How a session's last page went, as the browser container recorded it.
#[derive(Deserialize)]
struct Run {
    at: String,
    ok: bool,
    #[serde(default)]
    note: String,
    /// The day the site's sign-in ends, when the site has said.
    #[serde(default)]
    expires: String,
}

/// A small JSON file another container wrote; `None` when it is absent, too large or not valid.
fn read_json<T: DeserializeOwned>(path: &FilePath) -> Option<T> {
    if std::fs::metadata(path).ok()?.len() > MAX_FILE {
        return None;
    }
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}

/// Whole or not at all: the browser container may read the file at any moment.
fn write_whole(path: &FilePath, content: &[u8], mode: u32) -> std::io::Result<()> {
    let name = path.file_name().and_then(|name| name.to_str()).unwrap_or("file");
    let partial = path.with_file_name(format!(".{name}.tmp"));
    let mut file = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(mode).open(&partial)?;
    file.write_all(content)?;
    file.sync_all()?;
    std::fs::rename(partial, path)
}

/// Where a site's session cookie is kept: `None` when this install has no logins folder.
fn login_file(state: &AppState, site: &str) -> Option<PathBuf> {
    state.logins_dir.as_ref().map(|dir| dir.join(format!("site-{site}.json")))
}

fn plain(text: &str, max: usize) -> bool {
    !text.is_empty() && text.chars().count() <= max && !text.chars().any(char::is_control)
}

fn check_name(name: &str) -> Result<String, String> {
    let name = name.trim();
    if plain(name, MAX_NAME) {
        Ok(name.to_owned())
    } else {
        Err(format!("A source needs a name of 1 to {MAX_NAME} characters."))
    }
}

struct Choice {
    key: &'static str,
    label: &'static str,
    selected: bool,
}

struct BuiltinRow {
    id: i64,
    name: String,
    about: &'static str,
    enabled: bool,
    status: Option<String>,
    failing: bool,
}

struct FeedRow {
    id: i64,
    name: String,
    address: String,
    extract: Vec<Choice>,
    pinned: bool,
    enabled: bool,
    /// How its last run went, in words; `None` before it has been read.
    status: Option<String>,
    failing: bool,
    /// What the check of its address found; `None` when it was never checked.
    check: Option<String>,
    check_failed: bool,
    /// The site whose subscriber session its pages are fetched with, when one covers it.
    session: Option<String>,
    /// Just added: shown first until the page is loaded afresh.
    new: bool,
}

struct SessionRow {
    site: String,
    /// How the last page fetched with it went, and when the site says its sign-in ends.
    note: Option<String>,
    failed: bool,
}

#[derive(Template)]
#[template(path = "sources.html")]
struct SourcesPage {
    name: String,
    csrf: String,
    builtins: Vec<BuiltinRow>,
    sessions: Vec<SessionRow>,
    feeds: Vec<FeedRow>,
    logins: bool,
    summary: String,
    error: Option<String>,
    notice: Option<&'static str>,
    /// An address is still being checked: the page reloads itself until it is done.
    checking: bool,
}

fn choices(current: &str) -> Vec<Choice> {
    EXTRACT.iter().map(|(key, label)| Choice { key, label, selected: *key == current }).collect()
}

fn about(builtin: &str) -> &'static str {
    match builtin {
        "hackernews" => "Stories from the front page and others that drew votes in the last few days.",
        "lobsters" => "The hottest stories, mostly programming and systems.",
        _ => "",
    }
}

fn when(stamp: &str) -> String {
    stamp.get(..16).unwrap_or(stamp).replace('T', " ")
}

async fn render(
    state: &AppState,
    auth: &AuthSession,
    session: &Session,
    status: StatusCode,
    error: Option<String>,
    notice: Option<&'static str>,
    new: Option<i64>,
) -> Page {
    let user = reader(auth)?;
    let health = health(&state.pool).await?;
    let sources = list(&state.pool).await?;
    let sites = session_sites(&state.pool).await?;
    let runs: std::collections::HashMap<String, Run> =
        read_json(&state.sessions_dir.join("status.json")).unwrap_or_default();
    let (total, paused) = (sources.len(), sources.iter().filter(|source| !source.enabled).count());
    let (mut builtins, mut feeds) = (Vec::new(), Vec::new());
    let mut failing_count = 0;
    for source in sources {
        let seen = health.iter().find(|entry| entry.source == source.name);
        let status = seen.map(|entry| match entry.failures {
            0 => format!("Read without trouble, {}", entry.updated_at.get(..10).unwrap_or("")),
            n => format!("Failed {n} run{} in a row: {}", if n == 1 { "" } else { "s" }, entry.last_error),
        });
        let failing = seen.is_some_and(|entry| entry.failures > 0);
        failing_count += usize::from(failing && source.enabled);
        match source.kind.as_str() {
            "builtin" => builtins.push(BuiltinRow {
                id: source.id,
                about: about(&source.builtin),
                name: source.name,
                enabled: source.enabled,
                status,
                failing,
            }),
            _ => feeds.push(FeedRow {
                id: source.id,
                session: covering(&sites, &source.url),
                new: new == Some(source.id),
                address: source.url,
                extract: choices(&source.extract),
                pinned: source.pinned,
                enabled: source.enabled,
                failing,
                status,
                check: match source.check_state.as_str() {
                    "pending" => Some("Checking this address; it takes up to a minute.".to_owned()),
                    "ok" | "failed" => Some(source.check_note),
                    _ => None,
                },
                check_failed: source.check_state == "failed",
                name: source.name,
            }),
        }
    }
    // What was just added stays first until the page is loaded afresh, beside its check.
    feeds.sort_by_key(|row| !row.new);
    let sessions = sites
        .into_iter()
        .map(|site| {
            let run = runs.get(&format!("site-{site}"));
            SessionRow {
                note: run.map(|run| match run.ok {
                    true if run.expires.is_empty() => format!("Last page fetched with it: {}.", when(&run.at)),
                    true => format!(
                        "Last page fetched with it: {}. The site's sign-in lasts until {}.",
                        when(&run.at),
                        run.expires.get(..10).unwrap_or(&run.expires)
                    ),
                    false => format!("Last page with it failed {}: {}", when(&run.at), run.note),
                }),
                failed: run.is_some_and(|run| !run.ok),
                site,
            }
        })
        .collect();
    let summary =
        format!("{total} source{}, {paused} paused, {failing_count} failing.", if total == 1 { "" } else { "s" });
    let checking = feeds.iter().any(|row| row.check.as_deref().is_some_and(|note| note.starts_with("Checking")));
    let page = SourcesPage {
        name: user.name,
        csrf: csrf_token(session).await?,
        builtins,
        sessions,
        feeds,
        logins: state.logins_dir.is_some(),
        summary,
        error,
        notice,
        checking,
    };
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
    /// The source just added, shown first.
    new: Option<i64>,
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
        Some("added") => Some("Source added. Its address is being checked; it is read from the next run."),
        Some("session") => Some("Session saved. Pages from that site are fetched with it from the next run."),
        Some("nosession") => Some("Session removed."),
        Some("saved") => Some("Saved."),
        Some("removed") => Some("Source removed. Articles already delivered from it are kept."),
        _ => None,
    };
    render(&state, &auth, &session, StatusCode::OK, None, notice, query.new).await
}

/// `None` when the form may be acted on; otherwise the response that refuses it.
async fn guard(
    auth: &AuthSession,
    session: &Session,
    headers: &HeaderMap,
    csrf: &str,
) -> Result<Option<axum::response::Response>, AppError> {
    if !admin(auth)? {
        return Ok(Some(refused(StatusCode::FORBIDDEN, ADMINS_ONLY)?));
    }
    if !allowed(headers, session, csrf).await? {
        return Ok(Some(refused(StatusCode::FORBIDDEN, NOT_FROM_HERE)?));
    }
    Ok(None)
}

fn name_taken(error: &sqlx::Error) -> bool {
    error.as_database_error().is_some_and(|e| e.is_unique_violation())
}

fn saved(what: &str) -> Page {
    Ok(Redirect::to(&format!("/sources?saved={what}")).into_response())
}

#[derive(Deserialize)]
pub struct SourceForm {
    csrf: String,
    name: String,
    /// A feed's address, or a site's for the feed to be found on.
    address: Option<String>,
    extract: Option<String>,
    pinned: Option<String>,
    enabled: Option<String>,
}

impl SourceForm {
    fn address(&self) -> Result<String, String> {
        let address = self.address.as_deref().unwrap_or("").trim();
        if address.len() > MAX_URL || web_url(address).is_none() {
            return Err("A feed needs an http(s) address.".to_owned());
        }
        Ok(address.to_owned())
    }

    fn extract(&self) -> &'static str {
        let wanted = self.extract.as_deref().unwrap_or("auto");
        EXTRACT.iter().map(|(key, _)| *key).find(|key| *key == wanted).unwrap_or("auto")
    }
}

/// Shows the page again with what was wrong, or the saved notice.
async fn finish(
    state: &AppState,
    auth: &AuthSession,
    session: &Session,
    done: Result<Result<&str, String>, sqlx::Error>,
    name: &str,
) -> Page {
    let problem = match done {
        Ok(Ok(what)) => return saved(what),
        Ok(Err(problem)) => problem,
        Err(error) if name_taken(&error) => format!("There is already a source named {name}."),
        Err(error) => return Err(error.into()),
    };
    render(state, auth, session, StatusCode::BAD_REQUEST, Some(problem), None, None).await
}

pub async fn add(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<SourceForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let (name, url) = match (check_name(&form.name), form.address()) {
        (Ok(name), Ok(url)) => (name, url),
        (Err(problem), _) | (_, Err(problem)) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
    };
    let (extract, pinned, now) = (form.extract(), form.pinned.is_some(), now_iso());
    let done = sqlx::query!(
        "INSERT INTO sources (name, kind, url, extract, pinned, enabled, created_at, check_state)
         VALUES (?, 'rss', ?, ?, ?, 1, ?, 'pending')",
        name,
        url,
        extract,
        pinned,
        now
    )
    .execute(&state.pool)
    .await;
    let what = done.as_ref().map(|row| format!("added&new={0}#s-{0}", row.last_insert_rowid())).unwrap_or_default();
    finish(&state, &auth, &session, done.map(|_| Ok(what.as_str())), &name).await
}

pub async fn update(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(form): Form<SourceForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let (name, url) = match (check_name(&form.name), form.address()) {
        (Ok(name), Ok(url)) => (name, url),
        (Err(problem), _) | (_, Err(problem)) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
    };
    let (extract, pinned, enabled) = (form.extract(), form.pinned.is_some(), form.enabled.is_some());
    // A built-in source is only switched on or off: this changes feeds alone.
    let done = sqlx::query!(
        "UPDATE sources SET name = ?, url = ?, extract = ?, pinned = ?, enabled = ?,
         check_state = 'pending', check_note = '' WHERE id = ? AND kind = 'rss'",
        name,
        url,
        extract,
        pinned,
        enabled,
        id
    )
    .execute(&state.pool)
    .await;
    finish(&state, &auth, &session, done.map(|_| Ok("saved")), &name).await
}

#[derive(Deserialize)]
pub struct ToggleForm {
    csrf: String,
    /// "1" to read it, anything else to stop.
    enabled: String,
}

/// Switch a built-in source on or off.
pub async fn toggle(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(form): Form<ToggleForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let enabled = form.enabled == "1";
    sqlx::query!("UPDATE sources SET enabled = ? WHERE id = ? AND kind = 'builtin'", enabled, id)
        .execute(&state.pool)
        .await?;
    saved("saved")
}

#[derive(Deserialize)]
pub struct CsrfForm {
    csrf: String,
}

pub async fn delete(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(form): Form<CsrfForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    sqlx::query!("DELETE FROM sources WHERE id = ? AND kind != 'builtin'", id).execute(&state.pool).await?;
    saved("removed")
}

/// A site's name as the session fetcher will match it: a plain host name, not an address.
fn check_site(site: &str) -> Option<String> {
    let site = site.trim().trim_start_matches("https://").trim_end_matches('/').to_ascii_lowercase();
    // Saved for the whole site: a session for www.ft.com is one for ft.com.
    let site = site.strip_prefix("www.").map(str::to_owned).unwrap_or(site);
    let labels: Vec<&str> = site.split('.').collect();
    let named = labels.len() >= 2
        && site.len() <= 253
        && labels.iter().all(|label| {
            !label.is_empty()
                && !label.starts_with('-')
                && !label.ends_with('-')
                && label.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        })
        && labels.last().is_some_and(|last| last.chars().any(|c| c.is_ascii_alphabetic()));
    named.then_some(site)
}

async fn session_sites(pool: &SqlitePool) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar!(r#"SELECT domain AS "domain!" FROM sessions ORDER BY domain"#).fetch_all(pool).await
}

/// The site whose session a page at `url` is fetched with, as the pipeline matches it: the
/// most particular one the page's host is, or is under.
fn covering(sites: &[String], url: &str) -> Option<String> {
    let host = url.split("://").nth(1)?.split(['/', ':', '?', '#']).next()?.to_ascii_lowercase();
    sites
        .iter()
        .filter(|site| host == **site || host.ends_with(&format!(".{site}")))
        .max_by_key(|site| site.len())
        .cloned()
}

#[derive(Deserialize)]
pub struct SessionForm {
    csrf: String,
    site: String,
    cookie: String,
}

/// Save a subscriber session for a site, where only the browser container reads it. Every page
/// on that site, from any feed or added by hand, is fetched with it. The cookie is never shown
/// again, never stored in the database, and sent to that one site only.
pub async fn set_session(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<SessionForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let Some(site) = check_site(&form.site) else {
        let problem = "Give the site the session is for, such as ft.com.".to_owned();
        return finish(&state, &auth, &session, Ok(Err(problem)), "").await;
    };
    let Some(path) = login_file(&state, &site) else {
        let problem = "Sessions cannot be saved: this install has no logins folder (FORENOON_LOGINS_DIR).".to_owned();
        return finish(&state, &auth, &session, Ok(Err(problem)), "").await;
    };
    // Pasted with or without the header's name. A line break would let it carry other headers.
    let cookie = form.cookie.trim();
    let cookie = cookie.strip_prefix("Cookie:").or_else(|| cookie.strip_prefix("cookie:")).unwrap_or(cookie).trim();
    if !plain(cookie, MAX_COOKIE) || !cookie.contains('=') || !cookie.is_ascii() {
        let problem =
            "Paste the Cookie header your browser sends to the site: one line of name=value pairs.".to_owned();
        return finish(&state, &auth, &session, Ok(Err(problem)), "").await;
    }
    let content = serde_json::json!({ "cookie": cookie, "domain": site }).to_string();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    write_whole(&path, content.as_bytes(), 0o600)?;
    let now = now_iso();
    sqlx::query!("INSERT OR IGNORE INTO sessions (domain, created_at) VALUES (?, ?)", site, now)
        .execute(&state.pool)
        .await?;
    saved("session")
}

#[derive(Deserialize)]
pub struct RemoveSessionForm {
    csrf: String,
    site: String,
}

pub async fn remove_session(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<RemoveSessionForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let Some(site) = check_site(&form.site) else { return saved("nosession") };
    sqlx::query!("DELETE FROM sessions WHERE domain = ?", site).execute(&state.pool).await?;
    if let Some(path) = login_file(&state, &site) {
        match std::fs::remove_file(path) {
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => return Err(error.into()),
            _ => {}
        }
    }
    saved("nosession")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_sessions_site_is_a_plain_host_name() {
        assert_eq!(check_site(" https://FT.com/ ").as_deref(), Some("ft.com"));
        assert_eq!(check_site("www.nytimes.com").as_deref(), Some("nytimes.com"));
        for bad in ["", "com", "localhost", "192.168.1.1", "ft.com/path", "ft.com:443", "a b.com", "-x.com", "x..com"] {
            assert_eq!(check_site(bad), None, "{bad}");
        }
    }
}
