//! The sources page: what the pipeline reads, in three kinds. Built-in sources are front pages
//! the pipeline knows how to read and are only switched on or off; feeds are added by
//! address; Calibre recipes are chosen from the list the recipes container publishes.
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
use chrono::{DateTime, Utc};
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
const MAX_TITLE: usize = 200;
const MAX_LOGIN: usize = 500;
const MAX_COOKIE: usize = 16 * 1024;
const MAX_FILE: u64 = 4 * 1024 * 1024;
/// A run the recipes container started this long ago and never finished was interrupted.
const RUN_MINUTES: i64 = 45;
const EXTRACT: [(&str, &str); 3] =
    [("auto", "Best of page and feed"), ("page", "Web page only"), ("feed", "Feed only")];

pub struct Source {
    pub id: i64,
    pub name: String,
    pub kind: String,
    pub url: String,
    /// For a recipe, the folder its issues are written to.
    pub recipe: String,
    /// For a recipe, the Calibre recipe run into that folder.
    pub recipe_title: String,
    /// For a built-in source, the pipeline's key for it.
    pub builtin: String,
    pub extract: String,
    pub pinned: bool,
    pub enabled: bool,
    /// "" (never checked), "pending", "ok" or "failed": the pipeline's check of the address.
    pub check_state: String,
    pub check_note: String,
    /// For a feed with a subscriber's session: the only site its cookie is sent to. "" for none.
    pub session_domain: String,
}

pub async fn list(pool: &SqlitePool) -> Result<Vec<Source>, sqlx::Error> {
    sqlx::query_as!(
        Source,
        r#"SELECT id AS "id!", name, kind, url, recipe, recipe_title, builtin, extract, pinned AS "pinned: bool",
           enabled AS "enabled: bool", check_state, check_note, session_domain
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

// What the recipes container leaves in the recipes folder, and what is left there for it.

/// The recipes that can be chosen: Calibre's own, and any `.recipe` file in the folder.
#[derive(Deserialize, Default)]
struct Catalogue {
    #[serde(default)]
    recipes: Vec<Recipe>,
    #[serde(default)]
    custom: Vec<String>,
}

#[derive(Deserialize)]
struct Recipe {
    title: String,
    #[serde(default)]
    language: String,
    /// "yes", "optional" or "no": whether it wants a subscriber's login.
    #[serde(default)]
    login: String,
}

impl Catalogue {
    /// Whether this recipe wants a login; `None` when it is not in the list.
    fn login(&self, title: &str) -> Option<&str> {
        if self.custom.iter().any(|file| file == title) {
            return Some("optional"); // a recipe file of the admin's own: it may or may not
        }
        self.recipes.iter().find(|recipe| recipe.title == title).map(|recipe| recipe.login.as_str())
    }
}

/// How a recipe's last run went, as the recipes container recorded it.
#[derive(Deserialize)]
struct Run {
    at: String,
    ok: bool,
    #[serde(default)]
    running: bool,
    #[serde(default)]
    note: String,
}

#[derive(Deserialize)]
struct SavedLogin {
    username: String,
}

/// A small JSON file another container wrote; `None` when it is absent, too large or not valid.
fn read_json<T: DeserializeOwned>(path: &FilePath) -> Option<T> {
    if std::fs::metadata(path).ok()?.len() > MAX_FILE {
        return None;
    }
    serde_json::from_slice(&std::fs::read(path).ok()?).ok()
}

/// Whole or not at all: the recipes container may read the file at any moment.
fn write_whole(path: &FilePath, content: &[u8], mode: u32) -> std::io::Result<()> {
    let name = path.file_name().and_then(|name| name.to_str()).unwrap_or("file");
    let partial = path.with_file_name(format!(".{name}.tmp"));
    let mut file = std::fs::OpenOptions::new().write(true).create(true).truncate(true).mode(mode).open(&partial)?;
    file.write_all(content)?;
    file.sync_all()?;
    std::fs::rename(partial, path)
}

/// Write `recipes.conf`, the list the recipes container runs, from the sources that are read.
async fn write_conf(state: &AppState) -> Result<(), AppError> {
    let rows = sqlx::query!(
        "SELECT recipe, recipe_title FROM sources
         WHERE kind = 'recipe' AND enabled = 1 AND recipe_title != '' ORDER BY recipe"
    )
    .fetch_all(&state.pool)
    .await?;
    let mut conf = String::from("# Written by the web pages from the Sources page. Changes made here are lost.\n");
    for row in rows {
        conf.push_str(&format!("{} = {}\n", row.recipe, row.recipe_title));
    }
    std::fs::create_dir_all(&state.recipes_dir)?;
    write_whole(&state.recipes_dir.join("recipes.conf"), conf.as_bytes(), 0o644)?;
    Ok(())
}

/// Ask the recipes container to run this recipe now; it looks every few seconds.
fn request_run(state: &AppState, folder: &str) -> std::io::Result<()> {
    let requests = state.recipes_dir.join(".requests");
    std::fs::create_dir_all(&requests)?;
    std::fs::write(requests.join(folder), b"")
}

fn login_file(state: &AppState, folder: &str) -> Option<PathBuf> {
    state.logins_dir.as_ref().map(|dir| dir.join(format!("{folder}.json")))
}

fn plain(text: &str, max: usize) -> bool {
    !text.is_empty() && text.chars().count() <= max && !text.chars().any(char::is_control)
}

/// A folder name made from a recipe's title, not already used by another recipe.
fn folder_for(title: &str, taken: &[String]) -> String {
    let mut slug = String::new();
    for c in title.trim_end_matches(".recipe").chars() {
        if c.is_ascii_alphanumeric() {
            slug.push(c.to_ascii_lowercase());
        } else if !slug.is_empty() && !slug.ends_with('-') {
            slug.push('-');
        }
    }
    let slug: String = slug.trim_end_matches('-').chars().take(32).collect();
    let slug =
        if slug.trim_end_matches('-').is_empty() { "recipe".to_owned() } else { slug.trim_end_matches('-').to_owned() };
    let mut folder = slug.clone();
    let mut n = 2;
    while taken.contains(&folder) {
        folder = format!("{slug}-{n}");
        n += 1;
    }
    folder
}

/// The recipe named in a form, checked against the list when there is one.
fn check_title(title: &str, catalogue: Option<&Catalogue>) -> Result<String, String> {
    let title = title.trim();
    // '#' and '=' would not survive the conf file's one line per recipe.
    if !plain(title, MAX_TITLE) || title.contains(['#', '=']) {
        return Err("Choose a recipe from the list.".to_owned());
    }
    match catalogue {
        Some(catalogue) if catalogue.login(title).is_none() => {
            Err(format!("{title} is not a recipe this Calibre has. Choose one from the list."))
        }
        _ => Ok(title.to_owned()),
    }
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
    /// The site a saved session is for, or a guess at it for the form.
    session_site: String,
    has_session: bool,
    /// How the last page fetched with the session went.
    session_note: Option<String>,
    session_failed: bool,
}

struct RecipeRow {
    id: i64,
    name: String,
    title: String,
    folder: String,
    pinned: bool,
    enabled: bool,
    status: Option<String>,
    failing: bool,
    /// How the recipes container's last run of it went.
    run: Option<String>,
    run_failed: bool,
    /// It is being fetched now, or is about to be.
    fetching: bool,
    /// What to say above the login form.
    login_note: String,
    login_missing: bool,
    username: String,
}

struct Option_ {
    title: String,
    note: String,
}

#[derive(Template)]
#[template(path = "sources.html")]
struct SourcesPage {
    name: String,
    csrf: String,
    builtins: Vec<BuiltinRow>,
    feeds: Vec<FeedRow>,
    recipes: Vec<RecipeRow>,
    /// The recipes that can be chosen; empty until the recipes container has run.
    catalogue: Vec<Option_>,
    logins: bool,
    summary: String,
    error: Option<String>,
    notice: Option<&'static str>,
    /// Something is still being checked or fetched: the page reloads itself until it is done.
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
) -> Page {
    let user = reader(auth)?;
    let health = health(&state.pool).await?;
    let sources = list(&state.pool).await?;
    let catalogue: Option<Catalogue> = read_json(&state.recipes_dir.join("catalogue.json"));
    let runs: std::collections::HashMap<String, Run> =
        read_json(&state.recipes_dir.join("status.json")).unwrap_or_default();
    let (total, paused) = (sources.len(), sources.iter().filter(|source| !source.enabled).count());
    let (mut builtins, mut feeds, mut recipes) = (Vec::new(), Vec::new(), Vec::new());
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
            "recipe" => {
                let run = runs.get(&source.recipe);
                let asked = state.recipes_dir.join(".requests").join(&source.recipe).exists();
                let fresh = |run: &Run| {
                    DateTime::parse_from_rfc3339(&run.at)
                        .is_ok_and(|at| (Utc::now() - at.with_timezone(&Utc)).num_minutes() < RUN_MINUTES)
                };
                let fetching = asked || run.is_some_and(|run| run.running && fresh(run));
                let saved: Option<SavedLogin> = login_file(state, &source.recipe).and_then(|path| read_json(&path));
                let wants = catalogue.as_ref().and_then(|list| list.login(&source.recipe_title)).unwrap_or("optional");
                let (login_note, login_missing) = match (&saved, wants) {
                    (Some(login), _) => (format!("Login saved for {}.", login.username), false),
                    (None, "yes") => ("This recipe needs a subscriber's login.".to_owned(), true),
                    (None, "optional") => ("This recipe can take a subscriber's login.".to_owned(), false),
                    // Calibre says it needs none, but its list is not always right.
                    (None, _) => ("This recipe does not ask for a login.".to_owned(), false),
                };
                recipes.push(RecipeRow {
                    id: source.id,
                    name: source.name,
                    pinned: source.pinned,
                    enabled: source.enabled,
                    status,
                    failing,
                    run: match run {
                        _ if fetching => Some("Fetching now; a recipe can take several minutes.".to_owned()),
                        Some(run) if run.running => Some(format!("A fetch begun {} did not finish.", when(&run.at))),
                        Some(run) if run.ok => Some(format!("Fetched {}: {}.", when(&run.at), run.note)),
                        Some(run) => Some(format!("Fetch failed {}: {}", when(&run.at), run.note)),
                        None if source.recipe_title.is_empty() => {
                            Some("Choose its Calibre recipe and save.".to_owned())
                        }
                        None => None,
                    },
                    run_failed: !fetching
                        && (source.recipe_title.is_empty() || run.is_some_and(|run| !run.ok || run.running)),
                    fetching,
                    login_note,
                    login_missing,
                    username: saved.map(|login| login.username).unwrap_or_default(),
                    title: source.recipe_title,
                    folder: source.recipe,
                });
            }
            _ => feeds.push(FeedRow {
                id: source.id,
                has_session: !source.session_domain.is_empty(),
                session_site: match source.session_domain.as_str() {
                    "" => guess_site(&source.url),
                    domain => domain.to_owned(),
                },
                session_note: runs
                    .get(&format!("feed-{}", source.id))
                    .filter(|_| !source.session_domain.is_empty())
                    .map(|run| match run.ok {
                        true => format!("Last page fetched with it: {}.", when(&run.at)),
                        false => format!("Last page with it failed {}: {}", when(&run.at), run.note),
                    }),
                session_failed: !source.session_domain.is_empty()
                    && runs.get(&format!("feed-{}", source.id)).is_some_and(|run| !run.ok),
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
    let summary =
        format!("{total} source{}, {paused} paused, {failing_count} failing.", if total == 1 { "" } else { "s" });
    let checking = recipes.iter().any(|row| row.fetching)
        || feeds.iter().any(|row| row.check.as_deref().is_some_and(|note| note.starts_with("Checking")));
    let catalogue = catalogue.map(|list| {
        let custom =
            list.custom.into_iter().map(|file| Option_ { title: file, note: "your own recipe file".to_owned() });
        let calibre = list.recipes.into_iter().map(|recipe| Option_ {
            note: match recipe.login.as_str() {
                "yes" => format!("{} · needs a login", recipe.language),
                "optional" => format!("{} · login optional", recipe.language),
                _ => recipe.language,
            },
            title: recipe.title,
        });
        custom.chain(calibre).collect()
    });
    let page = SourcesPage {
        name: user.name,
        csrf: csrf_token(session).await?,
        builtins,
        feeds,
        recipes,
        catalogue: catalogue.unwrap_or_default(),
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
        Some("recipe") => Some("Recipe added. Its first issue is being fetched now."),
        Some("fetching") => Some("Asked the recipes container to fetch it now."),
        Some("login") => Some("Login saved. The recipe is being fetched with it now."),
        Some("nologin") => Some("Login removed."),
        Some("session") => Some("Session saved. Article pages from that site are fetched with it from the next run."),
        Some("nosession") => Some("Session removed."),
        Some("saved") => Some("Saved."),
        Some("removed") => Some("Source removed. Articles already delivered from it are kept."),
        _ => None,
    };
    render(&state, &auth, &session, StatusCode::OK, None, notice).await
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
    /// For a recipe: which Calibre recipe.
    recipe: Option<String>,
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
    render(state, auth, session, StatusCode::BAD_REQUEST, Some(problem), None).await
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
    finish(&state, &auth, &session, done.map(|_| Ok("added")), &name).await
}

pub async fn add_recipe(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<SourceForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let catalogue: Option<Catalogue> = read_json(&state.recipes_dir.join("catalogue.json"));
    let title = match check_title(form.recipe.as_deref().unwrap_or(""), catalogue.as_ref()) {
        Ok(title) => title,
        Err(problem) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
    };
    // Named after the recipe unless another name was given.
    let given = if form.name.trim().is_empty() { title.trim_end_matches(".recipe") } else { &form.name };
    let name = match check_name(given) {
        Ok(name) => name,
        Err(problem) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
    };
    let taken: Vec<String> =
        sqlx::query_scalar!("SELECT recipe FROM sources WHERE kind = 'recipe'").fetch_all(&state.pool).await?;
    let folder = folder_for(&title, &taken);
    let (pinned, now) = (form.pinned.is_some(), now_iso());
    // A recipe's issue is the only copy there is, so it is never fetched again from the site.
    let done = sqlx::query!(
        "INSERT INTO sources (name, kind, recipe, recipe_title, extract, pinned, enabled, created_at)
         VALUES (?, 'recipe', ?, ?, 'feed', ?, 1, ?)",
        name,
        folder,
        title,
        pinned,
        now
    )
    .execute(&state.pool)
    .await;
    if done.is_ok() {
        write_conf(&state).await?;
        request_run(&state, &folder)?;
    }
    finish(&state, &auth, &session, done.map(|_| Ok("recipe")), &name).await
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
    let Some(kind) =
        sqlx::query_scalar!("SELECT kind FROM sources WHERE id = ?", id).fetch_optional(&state.pool).await?
    else {
        return saved("saved");
    };
    let name = match check_name(&form.name) {
        Ok(name) => name,
        Err(problem) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
    };
    let (pinned, enabled) = (form.pinned.is_some(), form.enabled.is_some());
    let done = match kind.as_str() {
        "recipe" => {
            let catalogue: Option<Catalogue> = read_json(&state.recipes_dir.join("catalogue.json"));
            let title = match check_title(form.recipe.as_deref().unwrap_or(""), catalogue.as_ref()) {
                Ok(title) => title,
                Err(problem) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
            };
            let done = sqlx::query!(
                "UPDATE sources SET name = ?, recipe_title = ?, pinned = ?, enabled = ? WHERE id = ? AND kind = 'recipe'",
                name,
                title,
                pinned,
                enabled,
                id
            )
            .execute(&state.pool)
            .await;
            if done.is_ok() {
                write_conf(&state).await?;
            }
            done
        }
        "rss" => {
            let url = match form.address() {
                Ok(url) => url,
                Err(problem) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
            };
            let extract = form.extract();
            sqlx::query!(
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
            .await
        }
        // A built-in source is only switched on or off.
        _ => return saved("saved"),
    };
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

async fn recipe_folder(state: &AppState, id: i64) -> Result<Option<String>, AppError> {
    Ok(sqlx::query_scalar!("SELECT recipe FROM sources WHERE id = ? AND kind = 'recipe'", id)
        .fetch_optional(&state.pool)
        .await?)
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
    let folder = recipe_folder(&state, id).await?;
    sqlx::query!("DELETE FROM sources WHERE id = ? AND kind != 'builtin'", id).execute(&state.pool).await?;
    if let Some(path) = login_file(&state, &format!("feed-{id}")) {
        let _ = std::fs::remove_file(path); // a feed's session goes with it
    }
    if let Some(folder) = folder {
        // Its login goes with it; the issues already fetched stay where they are.
        if let Some(path) = login_file(&state, &folder) {
            let _ = std::fs::remove_file(path);
        }
        write_conf(&state).await?;
    }
    saved("removed")
}

/// Ask for a recipe to be fetched now, without waiting for the daily run.
pub async fn fetch(
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
    if let Some(folder) = recipe_folder(&state, id).await? {
        write_conf(&state).await?;
        request_run(&state, &folder)?;
    }
    saved("fetching")
}

#[derive(Deserialize)]
pub struct LoginForm {
    csrf: String,
    username: String,
    password: String,
}

/// Save a recipe's login where only the recipes container reads it. It is never shown again.
pub async fn set_login(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(form): Form<LoginForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let Some(folder) = recipe_folder(&state, id).await? else {
        return saved("saved");
    };
    let Some(path) = login_file(&state, &folder) else {
        let problem = "Logins cannot be saved: this install has no logins folder (FORENOON_LOGINS_DIR).".to_owned();
        return finish(&state, &auth, &session, Ok(Err(problem)), "").await;
    };
    let username = form.username.trim();
    if !plain(username, MAX_LOGIN) || !plain(&form.password, MAX_LOGIN) {
        let problem = "A login needs a username and a password.".to_owned();
        return finish(&state, &auth, &session, Ok(Err(problem)), "").await;
    }
    let content = serde_json::json!({ "username": username, "password": form.password }).to_string();
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    write_whole(&path, content.as_bytes(), 0o600)?;
    write_conf(&state).await?;
    request_run(&state, &folder)?;
    saved("login")
}

pub async fn remove_login(
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
    if let Some(path) = recipe_folder(&state, id).await?.and_then(|folder| login_file(&state, &folder)) {
        match std::fs::remove_file(path) {
            Err(error) if error.kind() != std::io::ErrorKind::NotFound => return Err(error.into()),
            _ => {}
        }
    }
    saved("nologin")
}

/// The site a feed's articles are probably on: its host without a leading "www." or "feeds.".
fn guess_site(url: &str) -> String {
    let host = url.split("://").nth(1).unwrap_or("").split(['/', ':', '?']).next().unwrap_or("").to_ascii_lowercase();
    let host = host
        .strip_prefix("www.")
        .or_else(|| host.strip_prefix("feeds."))
        .or_else(|| host.strip_prefix("rss."))
        .unwrap_or(&host);
    host.to_owned()
}

/// A site's name as the session fetcher will match it: a plain host name, not an address.
fn check_site(site: &str) -> Option<String> {
    let site = site.trim().trim_start_matches("https://").trim_end_matches('/').to_ascii_lowercase();
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

#[derive(Deserialize)]
pub struct SessionForm {
    csrf: String,
    site: String,
    cookie: String,
}

/// Save a feed's subscriber session where only the recipes container reads it. The cookie is
/// never shown again, never stored in the database, and sent to that one site only.
pub async fn set_session(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(form): Form<SessionForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let feed = sqlx::query_scalar!("SELECT id FROM sources WHERE id = ? AND kind = 'rss'", id)
        .fetch_optional(&state.pool)
        .await?;
    if feed.is_none() {
        return saved("saved");
    }
    let Some(path) = login_file(&state, &format!("feed-{id}")) else {
        let problem = "Sessions cannot be saved: this install has no logins folder (FORENOON_LOGINS_DIR).".to_owned();
        return finish(&state, &auth, &session, Ok(Err(problem)), "").await;
    };
    let Some(site) = check_site(&form.site) else {
        let problem = "Give the site the session is for, such as ft.com.".to_owned();
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
    sqlx::query!("UPDATE sources SET session_domain = ? WHERE id = ? AND kind = 'rss'", site, id)
        .execute(&state.pool)
        .await?;
    saved("session")
}

pub async fn remove_session(
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
    sqlx::query!("UPDATE sources SET session_domain = '' WHERE id = ? AND kind = 'rss'", id)
        .execute(&state.pool)
        .await?;
    if let Some(path) = login_file(&state, &format!("feed-{id}")) {
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
    fn folders_are_made_from_titles_and_never_shared() {
        assert_eq!(folder_for("Financial Times", &[]), "financial-times");
        assert_eq!(folder_for("The Economist (print)", &[]), "the-economist-print");
        assert_eq!(folder_for("mine.recipe", &[]), "mine");
        assert_eq!(folder_for("Ünï", &[]), "n");
        assert_eq!(folder_for("日本", &[]), "recipe");
        assert_eq!(folder_for("Wired", &["wired".to_owned(), "wired-2".to_owned()]), "wired-3");
        assert!(folder_for(&"long name ".repeat(20), &[]).len() <= 32);
    }

    #[test]
    fn a_sessions_site_is_a_plain_host_name() {
        assert_eq!(guess_site("https://www.ft.com/world?format=rss"), "ft.com");
        assert_eq!(guess_site("https://feeds.paper.example:8443/rss"), "paper.example");
        assert_eq!(check_site(" https://FT.com/ ").as_deref(), Some("ft.com"));
        assert_eq!(check_site("www.nytimes.com").as_deref(), Some("www.nytimes.com"));
        for bad in ["", "com", "localhost", "192.168.1.1", "ft.com/path", "ft.com:443", "a b.com", "-x.com", "x..com"] {
            assert_eq!(check_site(bad), None, "{bad}");
        }
    }

    #[test]
    fn a_recipe_must_be_one_the_list_has() {
        let list = Catalogue {
            recipes: vec![Recipe { title: "Paid Paper".into(), language: "en".into(), login: "yes".into() }],
            custom: vec!["mine.recipe".into()],
        };
        assert_eq!(check_title(" Paid Paper ", Some(&list)).unwrap(), "Paid Paper");
        assert_eq!(check_title("mine.recipe", Some(&list)).unwrap(), "mine.recipe");
        assert!(check_title("Other", Some(&list)).is_err());
        assert_eq!(check_title("Other", None).unwrap(), "Other", "no list yet: taken on trust");
        for bad in ["", "a # b", "a = b", "a\nb"] {
            assert!(check_title(bad, None).is_err());
        }
        assert_eq!(
            (list.login("Paid Paper"), list.login("mine.recipe"), list.login("x")),
            (Some("yes"), Some("optional"), None)
        );
    }
}
