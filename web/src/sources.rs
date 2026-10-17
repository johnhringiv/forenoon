//! The sources page: what the pipeline reads, and the subscriber sessions it reads with.
//! Built-in sources are front pages the pipeline knows how to read and are only switched on or
//! off; feeds are added by address; a session is a site's cookie, used for every page on it.
//!
//! Sources belong to the household and are read for the readers who follow them. One a reader
//! adds is theirs alone until they share it: nobody else sees it, except an admin, who sees
//! everything. Shared, it is followed for every reader who has not turned that off, as it is
//! for a new account; such a follow is made for them, not chosen, until they click Follow or
//! pin it. A feed another reader chose to follow is not removed, and only an admin changes
//! it. A session is the household's: every page on its site is fetched with it,
//! whoever follows the feed.

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
use chrono::NaiveDate;
use percent_encoding::{NON_ALPHANUMERIC, utf8_percent_encode};
use serde::{Deserialize, de::DeserializeOwned};
use sqlx::SqlitePool;
use tower_sessions::Session;

use crate::{
    auth::{AuthSession, User},
    db::{self, now_iso},
    pages::{AppError, AppState, NOT_FROM_HERE, Nav, Page, Viewer, allowed, html, reader, refused, web_url},
};

const MAX_NAME: usize = 80;
const MAX_URL: usize = 2000;
const DEFAULT_DAILY_MAX: i64 = 2;
const MAX_DAILY_MAX: i64 = 50;
const MAX_COOKIE: usize = 16 * 1024;
const MAX_FILE: u64 = 4 * 1024 * 1024;
const MAX_SEARCH: usize = 100;
/// The views of the feeds, as links with a count each.
const SHOW: [(&str, &str); 4] = [("", "All"), ("failing", "Failing"), ("paused", "Paused"), ("pinned", "Pinned")];
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
    /// The most of a day's places its articles may take; 0 for no limit.
    pub daily_max: i64,
    pub enabled: bool,
    /// "" (never checked), "pending", "ok" or "failed": the pipeline's check of the address.
    pub check_state: String,
    pub check_note: String,
    /// The account that added it; "" for a built-in, or a source from before readers had their own.
    pub added_by: String,
    pub added_by_name: String,
    /// Listed for the rest of the household to follow.
    pub shared: bool,
    /// When it was last shared; `None` when it never was, or was shared before this was kept.
    pub shared_at: Option<String>,
    /// Whether the reader it was listed for follows it, chose to rather than had it made for
    /// them, and pinned it.
    pub followed: bool,
    pub chosen: bool,
    pub pinned: bool,
    /// How many readers follow it, and how many others than this one chose to.
    pub followers: i64,
    pub chosen_by_others: i64,
}

impl Source {
    /// Whether this reader may change it: an admin, or the one who added it while no other
    /// reader chose to follow it. A follow made for a reader does not hold it.
    fn editable_by(&self, user: &User) -> bool {
        user.is_admin() || (self.added_by == user.id && self.chosen_by_others == 0)
    }

    /// Whether this reader may remove it: an admin or the one who added it, and only while no
    /// other reader chose to follow it. Pausing it is how an admin stops it for them too.
    fn removable_by(&self, user: &User) -> bool {
        (user.is_admin() || self.added_by == user.id) && self.chosen_by_others == 0
    }

    /// Whether this reader may share it with the household, or take that back.
    fn shareable_by(&self, user: &User) -> bool {
        user.is_admin() || self.added_by == user.id
    }
}

/// The sources this reader may see: every one for an admin; otherwise the ones they follow or
/// added, and the shared ones. Pinned ones first, as this reader pinned them.
pub async fn list(pool: &SqlitePool, user: &User) -> Result<Vec<Source>, sqlx::Error> {
    let admin = user.is_admin();
    sqlx::query_as!(
        Source,
        r#"SELECT s.id AS "id!", s.name, s.kind, s.url, s.builtin, s.extract, s.daily_max,
           s.enabled AS "enabled: bool", s.check_state, s.check_note, s.added_by,
           COALESCE(u.name, s.added_by) AS "added_by_name!: String", s.shared AS "shared: bool",
           s.shared_at, f.user_id IS NOT NULL AS "followed!: bool", COALESCE(f.chosen, 0) AS "chosen!: bool",
           COALESCE(f.pinned, 0) AS "pinned!: bool",
           (SELECT COUNT(*) FROM follows WHERE source_id = s.id) AS "followers!: i64",
           (SELECT COUNT(*) FROM follows WHERE source_id = s.id AND chosen = 1 AND user_id != ?)
             AS "chosen_by_others!: i64"
           FROM sources s
           LEFT JOIN follows f ON f.source_id = s.id AND f.user_id = ?
           LEFT JOIN users u ON u.id = s.added_by
           WHERE ? OR s.shared = 1 OR s.added_by = ? OR f.user_id IS NOT NULL
           ORDER BY COALESCE(f.pinned, 0) DESC, s.name COLLATE NOCASE"#,
        user.id,
        user.id,
        admin,
        user.id
    )
    .fetch_all(pool)
    .await
}

/// One source, if this reader may see it.
async fn visible(pool: &SqlitePool, user: &User, id: i64) -> Result<Option<Source>, sqlx::Error> {
    Ok(list(pool, user).await?.into_iter().find(|source| source.id == id))
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

/// The most of a day's places a source may take: a whole number from 0 (no limit) to 50.
fn daily_max(sent: Option<&str>) -> Result<i64, String> {
    match sent.map(str::trim) {
        None => Ok(DEFAULT_DAILY_MAX),
        Some(text) => text
            .parse()
            .ok()
            .filter(|n| (0..=MAX_DAILY_MAX).contains(n))
            .ok_or_else(|| format!("Places a day: give a whole number from 0 (no limit) to {MAX_DAILY_MAX}.")),
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
    daily_max: i64,
    /// "2 a day", or "No limit".
    limit: String,
    enabled: bool,
    followed: bool,
    status: Option<String>,
    failing: bool,
    /// The colour of the dot before its name: ok, bad, wait, off or none.
    state: &'static str,
    /// The dot's tooltip.
    dot: String,
}

struct FeedRow {
    id: i64,
    name: String,
    address: String,
    extract: Vec<Choice>,
    /// Where its text is taken from, in a word or two, and in a sentence for its tooltip; "" for
    /// the usual way.
    mode: (&'static str, &'static str),
    /// This reader follows it, and pinned it.
    followed: bool,
    pinned: bool,
    /// How this reader follows it: "none" (a button to follow it stands for its dot), "made"
    /// (followed for them when it was shared or their account was made: a ring) or "chosen"
    /// (a filled dot).
    follow: &'static str,
    /// The dot's tooltip: how it is followed and how its last run went.
    dot: String,
    shared: bool,
    /// Shared lately and not followed by this reader: marked new.
    fresh: bool,
    /// Who added it, for an admin's eyes; "" when that is this reader or nobody in particular.
    added_by: String,
    /// This reader may change it, remove it, or share it.
    can_edit: bool,
    can_remove: bool,
    can_share: bool,
    /// Another reader chose to follow it: it is kept, whoever added it.
    kept: bool,
    daily_max: i64,
    limit: String,
    enabled: bool,
    state: &'static str,
    /// Failing, or its address could not be checked: listed first.
    attention: bool,
    /// Shown with its form open.
    open: bool,
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
    /// This reader may remove it: they saved it, or they are an admin.
    removable: bool,
}

struct View {
    label: &'static str,
    count: usize,
    current: bool,
    href: String,
}

#[derive(Template)]
#[template(path = "sources.html")]
struct SourcesPage {
    nav: Option<Nav>,
    csrf: String,
    /// A demo host: the list alone, with no form on it.
    demo: bool,
    admin: bool,
    /// This reader has feeds followed for them when they are shared.
    follow_new: bool,
    builtins: Vec<BuiltinRow>,
    sessions: Vec<SessionRow>,
    /// The feeds that need attention, then the rest this reader follows, then the ones they
    /// could follow, all as filtered.
    attention: Vec<FeedRow>,
    feeds: Vec<FeedRow>,
    available: Vec<FeedRow>,
    /// How many feeds this reader may see in all.
    total: usize,
    search: String,
    show: &'static str,
    filtered: bool,
    views: Vec<View>,
    /// The add form open: nothing to list yet, or a form came back with a problem.
    adding: bool,
    logins: bool,
    summary: String,
    error: Option<String>,
    notice: Option<&'static str>,
    /// An address is still being checked: the page reloads itself until it is done.
    checking: bool,
}

/// What a source's dot says, as its tooltip: how this reader follows it, and how its last run went.
fn dot(follow: &str, state: &str, problem: Option<&str>, status: Option<&str>) -> String {
    let follow = match follow {
        "chosen" => "Following",
        "made" => "Added for you: open it and keep it to make it yours",
        _ => "Not followed",
    };
    let health = match state {
        "off" => "Paused",
        "wait" => "Its address is being checked",
        "none" => "Not read yet",
        _ => problem.or(status).unwrap_or("Not read yet"),
    };
    format!("{follow}. {health}.")
}

fn limit(daily_max: i64) -> String {
    if daily_max == 0 { "No limit".to_owned() } else { format!("{daily_max} a day") }
}

/// "14 October 2026" from the pipeline's timestamp.
fn day(stamp: &str) -> String {
    NaiveDate::parse_from_str(stamp.get(..10).unwrap_or(stamp), "%Y-%m-%d")
        .map_or_else(|_| stamp.to_owned(), |day| day.format("%-d %B %Y").to_string())
}

fn matches(row: &FeedRow, search: &str, show: &str) -> bool {
    let text = search.to_lowercase();
    let named =
        text.is_empty() || row.name.to_lowercase().contains(&text) || row.address.to_lowercase().contains(&text);
    named
        && match show {
            "failing" => row.attention,
            "paused" => !row.enabled,
            "pinned" => row.followed && row.pinned,
            _ => true,
        }
}

fn view_url(show: &str, search: &str) -> String {
    let mut parts = Vec::new();
    if !show.is_empty() {
        parts.push(format!("show={show}"));
    }
    if !search.is_empty() {
        parts.push(format!("q={}", utf8_percent_encode(search, NON_ALPHANUMERIC)));
    }
    if parts.is_empty() { "/sources".to_owned() } else { format!("/sources?{}", parts.join("&")) }
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

/// The list as the reader asked to see it: `search` in a name or address, one of SHOW, and
/// the source just added, shown first with its form open.
struct Sieve {
    search: String,
    show: &'static str,
    new: Option<i64>,
}

impl Sieve {
    fn all() -> Sieve {
        Sieve { search: String::new(), show: "", new: None }
    }
}

async fn render(
    state: &AppState,
    viewer: &Viewer,
    status: StatusCode,
    error: Option<String>,
    notice: Option<&'static str>,
    sieve: Sieve,
) -> Page {
    let user = &viewer.user;
    let admin = user.is_admin();
    let health = health(&state.pool).await?;
    let sources = list(&state.pool, user).await?;
    let sites = sessions(&state.pool).await?;
    let runs: std::collections::HashMap<String, Run> =
        read_json(&state.sessions_dir.join("status.json")).unwrap_or_default();
    let (total, paused) = (sources.len(), sources.iter().filter(|source| !source.enabled).count());
    let followed_count = sources.iter().filter(|source| source.followed).count();
    let (mut builtins, mut feeds) = (Vec::new(), Vec::new());
    let mut failing_count = 0;
    // Stamps are written as now_iso writes them, so they compare as text.
    let lately =
        (chrono::Utc::now() - chrono::Duration::days(FRESH_DAYS)).format("%Y-%m-%dT%H:%M:%S+00:00").to_string();
    let site_names: Vec<String> = sites.iter().map(|(site, _)| site.clone()).collect();
    // The sites this reader has a feed on: the sessions they see, and may save or replace.
    let mut own_sites = Vec::new();
    for source in sources {
        let seen = health.iter().find(|entry| entry.source == source.name);
        let status = seen.map(|entry| match entry.failures {
            0 => format!("Read without trouble, {}", day(&entry.updated_at)),
            n => format!("Failed {n} run{} in a row: {}", if n == 1 { "" } else { "s" }, entry.last_error),
        });
        let failing = seen.is_some_and(|entry| entry.failures > 0);
        // An admin is told of every failing source; a reader, of the ones they follow.
        failing_count += usize::from(failing && source.enabled && (admin || source.followed));
        let state = match (source.enabled, failing, &status) {
            (false, _, _) => "off",
            (_, true, _) => "bad",
            (_, _, None) => "none",
            _ => "ok",
        };
        match source.kind.as_str() {
            "builtin" => builtins.push(BuiltinRow {
                id: source.id,
                about: about(&source.builtin),
                name: source.name,
                daily_max: source.daily_max,
                limit: limit(source.daily_max),
                enabled: source.enabled,
                followed: source.followed,
                dot: dot(if source.followed { "chosen" } else { "none" }, state, None, status.as_deref()),
                status,
                failing,
                state,
            }),
            _ => {
                let check_failed = source.check_state == "failed";
                let checking = source.check_state == "pending";
                let covered = covering(&site_names, &source.url);
                if source.followed {
                    own_sites.extend(covered.clone());
                }
                let follow = match (source.followed, source.chosen) {
                    (false, _) => "none",
                    (true, false) => "made",
                    (true, true) => "chosen",
                };
                let shown = match state {
                    "off" => "off",
                    _ if check_failed => "bad",
                    _ if checking => "wait",
                    state => state,
                };
                let problem = check_failed.then_some(source.check_note.as_str());
                feeds.push(FeedRow {
                    id: source.id,
                    dot: dot(follow, shown, problem, status.as_deref()),
                    follow,
                    // Said only where the reader sees the session itself: on feeds they follow.
                    session: if admin || source.followed { covered } else { None },
                    new: sieve.new == Some(source.id),
                    open: sieve.new == Some(source.id),
                    address: source.url.clone(),
                    extract: choices(&source.extract),
                    mode: match source.extract.as_str() {
                        "page" => ("Page text", "The text is always taken from the article's web page, not the feed"),
                        "feed" => ("Feed text", "The text is taken from the feed; the article's page is never fetched"),
                        "link" => ("Links only", "Never fetched: its picks are shown as links to the site"),
                        _ => ("", ""),
                    },
                    followed: source.followed,
                    pinned: source.pinned,
                    shared: source.shared,
                    fresh: source.shared
                        && !source.followed
                        && source.shared_at.as_deref().is_some_and(|at| at >= lately.as_str()),
                    added_by: if admin && source.added_by != user.id {
                        source.added_by_name.clone()
                    } else {
                        String::new()
                    },
                    can_edit: source.editable_by(user),
                    can_remove: source.removable_by(user),
                    can_share: source.shareable_by(user),
                    kept: source.chosen_by_others > 0,
                    daily_max: source.daily_max,
                    limit: limit(source.daily_max),
                    enabled: source.enabled,
                    attention: source.enabled && (failing || check_failed) && (admin || source.followed),
                    state: shown,
                    failing,
                    status,
                    check: match source.check_state.as_str() {
                        "pending" => Some("Checking this address; it takes up to a minute.".to_owned()),
                        "ok" | "failed" => Some(source.check_note.clone()),
                        _ => None,
                    },
                    check_failed,
                    name: source.name.clone(),
                });
            }
        }
    }
    // What was just added stays first until the page is loaded afresh, beside its check.
    feeds.sort_by_key(|row| !row.new);
    let feed_count = feeds.len();
    let named: Vec<FeedRow> = feeds.into_iter().filter(|row| matches(row, &sieve.search, "")).collect();
    let views = SHOW
        .iter()
        .map(|(key, label)| View {
            label,
            count: named.iter().filter(|row| matches(row, "", key)).count(),
            current: *key == sieve.show,
            href: view_url(key, &sieve.search),
        })
        .collect();
    let shown: Vec<FeedRow> = named.into_iter().filter(|row| matches(row, "", sieve.show)).collect();
    let (attention, rest): (Vec<FeedRow>, Vec<FeedRow>) = shown.into_iter().partition(|row| row.attention);
    let (feeds, available): (Vec<FeedRow>, Vec<FeedRow>) = rest.into_iter().partition(|row| row.followed);
    let sessions = sites
        .into_iter()
        .filter(|(site, _)| admin || own_sites.contains(site))
        .map(|(site, saved_by)| {
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
                removable: admin || saved_by == user.id,
                site,
            }
        })
        .collect();
    let summary = if admin {
        format!("{total} source{}, {paused} paused, {failing_count} failing.", if total == 1 { "" } else { "s" })
    } else {
        format!(
            "Following {followed_count} of {total} source{}, {failing_count} failing.",
            if total == 1 { "" } else { "s" }
        )
    };
    let checking = attention.iter().chain(&feeds).chain(&available).any(|row| row.state == "wait");
    let page = SourcesPage {
        nav: viewer.nav(state, "sources").await?,
        csrf: viewer.csrf().await?,
        demo: viewer.is_demo(),
        admin,
        follow_new: db::follows_new(&state.pool, &user.id).await?,
        builtins,
        sessions,
        adding: error.is_some() || feed_count == 0,
        attention,
        feeds,
        available,
        total: feed_count,
        filtered: !sieve.search.is_empty() || !sieve.show.is_empty(),
        search: sieve.search,
        show: sieve.show,
        views,
        logins: state.logins_dir.is_some(),
        summary,
        error,
        notice,
        checking,
    };
    html(status, page)
}

const ADMINS_ONLY: &str = "That is for an admin.";
/// How long a feed shared with the household is marked new for a reader who does not follow it.
const FRESH_DAYS: i64 = 14;
const NOT_YOURS: &str = "That source is not yours to change.";
const NO_SUCH: &str = "No such source.";
const KEPT_FOR_EDIT: &str =
    "Another reader chose to follow this feed, so only an admin changes its address and settings now.";
const KEPT_FOR_REMOVAL: &str =
    "Another reader follows this feed, so it is kept. You can unfollow it, or an admin can pause it.";

/// Refused, though asked from the page itself (one loaded before someone else followed the feed,
/// say): the page again, with why at the top, rather than a bare error.
async fn kept(state: &AppState, auth: &AuthSession, session: &Session, why: &str) -> Page {
    let viewer = Viewer::signed_in(auth, session).await?;
    render(state, &viewer, StatusCode::FORBIDDEN, Some(why.to_owned()), None, Sieve::all()).await
}

#[derive(Deserialize)]
pub struct PageQuery {
    saved: Option<String>,
    /// The source just added, shown first.
    new: Option<i64>,
    q: Option<String>,
    show: Option<String>,
}

pub async fn page(State(state): State<AppState>, viewer: Viewer, Query(query): Query<PageQuery>) -> Page {
    // A demo host's page says nothing was just done, whatever the address claims.
    let saved = if viewer.is_demo() { None } else { query.saved.as_deref() };
    let notice = match saved {
        Some("added") => Some("Source added. Its address is being checked; it is read from the next run."),
        Some("followed") => Some("Followed. It is read for you from the next run."),
        Some("unfollowed") => Some("Unfollowed. It is kept for whoever else follows it."),
        Some("follownew") => Some("Saved. It applies to feeds shared from now on."),
        Some("session") => Some("Session saved. Pages from that site are fetched with it from the next run."),
        Some("nosession") => Some("Session removed."),
        Some("saved") => Some("Saved."),
        Some("removed") => Some("Source removed. Articles already delivered from it are kept."),
        _ => None,
    };
    let sieve = Sieve {
        search: query.q.as_deref().unwrap_or_default().trim().chars().take(MAX_SEARCH).collect(),
        show: SHOW.iter().map(|(key, _)| *key).find(|key| Some(*key) == query.show.as_deref()).unwrap_or(""),
        new: query.new.filter(|_| !viewer.is_demo()),
    };
    render(&state, &viewer, StatusCode::OK, None, notice, sieve).await
}

/// `None` when the form may be acted on; otherwise the response that refuses it.
async fn guard(
    auth: &AuthSession,
    session: &Session,
    headers: &HeaderMap,
    csrf: &str,
) -> Result<Option<axum::response::Response>, AppError> {
    reader(auth)?;
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
    /// Absent when the reader may only pin or unpin the source.
    name: Option<String>,
    /// A feed's address, or a site's for the feed to be found on.
    address: Option<String>,
    extract: Option<String>,
    pinned: Option<String>,
    daily_max: Option<String>,
    enabled: Option<String>,
    shared: Option<String>,
}

impl SourceForm {
    fn address(&self) -> Result<String, String> {
        let address = self.address.as_deref().unwrap_or("").trim();
        if address.len() > MAX_URL || web_url(address).is_none() {
            return Err("A feed needs an http(s) address.".to_owned());
        }
        Ok(address.to_owned())
    }

    fn daily_max(&self) -> Result<i64, String> {
        daily_max(self.daily_max.as_deref())
    }

    fn extract(&self) -> &'static str {
        let wanted = self.extract.as_deref().unwrap_or("auto");
        EXTRACT.iter().map(|(key, _)| *key).find(|key| *key == wanted).unwrap_or("auto")
    }

    /// The name, address and places a day, or what is wrong with them.
    fn checked(&self) -> Result<(String, String, i64), String> {
        let name = check_name(self.name.as_deref().unwrap_or(""))?;
        Ok((name, self.address()?, self.daily_max()?))
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
    let viewer = Viewer::signed_in(auth, session).await?;
    render(state, &viewer, StatusCode::BAD_REQUEST, Some(problem), None, Sieve::all()).await
}

/// A feed just shared with the household: it is followed, unpinned and not chosen, for every
/// reader who has that on. Only on sharing, so an unfollow stands through later edits.
async fn shared_now(pool: &SqlitePool, source: i64) -> Result<(), sqlx::Error> {
    let now = now_iso();
    sqlx::query!("UPDATE sources SET shared_at = ? WHERE id = ?", now, source).execute(pool).await?;
    sqlx::query!(
        "INSERT OR IGNORE INTO follows (user_id, source_id, pinned, created_at, chosen)
         SELECT id, ?, 0, ?, 0 FROM users WHERE follow_new = 1",
        source,
        now
    )
    .execute(pool)
    .await?;
    Ok(())
}

/// Follow it for this reader, pinned or not. `chosen` when the reader asked for it (Follow,
/// adding its address, pinning it); a follow once chosen stays so.
async fn follow(pool: &SqlitePool, user: &str, source: i64, pinned: bool, chosen: bool) -> Result<(), sqlx::Error> {
    let now = now_iso();
    sqlx::query!(
        "INSERT INTO follows (user_id, source_id, pinned, created_at, chosen) VALUES (?, ?, ?, ?, ?)
         ON CONFLICT (user_id, source_id) DO UPDATE SET pinned = excluded.pinned,
         chosen = MAX(chosen, excluded.chosen)",
        user,
        source,
        pinned,
        now,
        chosen
    )
    .execute(pool)
    .await?;
    Ok(())
}

/// An address as one feed is the same as another: case, the scheme, a leading `www.` and a
/// trailing slash aside.
fn same_feed(url: &str) -> String {
    let url = url.trim().to_ascii_lowercase();
    let url = url.strip_prefix("https://").or_else(|| url.strip_prefix("http://")).unwrap_or(&url);
    url.strip_prefix("www.").unwrap_or(url).trim_end_matches('/').to_owned()
}

/// Add a feed and follow it. A feed this reader may already see, by address, is followed
/// instead. Another reader's private feed is not looked at, whatever its address: it gets a
/// row of its own, so nobody learns of a feed by guessing its address.
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
    let user = reader(&auth)?;
    let (name, url, daily_max) = match form.checked() {
        Ok(checked) => checked,
        Err(problem) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
    };
    let (extract, pinned, now) = (form.extract(), form.pinned.is_some(), now_iso());
    let shared = form.shared.is_some();
    let pool = &state.pool;
    let wanted = same_feed(&url);
    let existing = list(pool, &user).await?.into_iter().find(|s| s.kind == "rss" && same_feed(&s.url) == wanted);
    if let Some(source) = existing {
        follow(pool, &user.id, source.id, pinned, true).await?;
        return saved(&format!("followed&new={0}#s-{0}", source.id));
    }
    let done = sqlx::query!(
        "INSERT INTO sources (name, kind, url, extract, daily_max, enabled, created_at, check_state, added_by, shared)
                 VALUES (?, 'rss', ?, ?, ?, 1, ?, 'pending', ?, ?)",
        name,
        url,
        extract,
        daily_max,
        now,
        user.id,
        shared
    )
    .execute(pool)
    .await;
    let id = match done {
        Ok(row) => row.last_insert_rowid(),
        Err(error) => return finish(&state, &auth, &session, Err(error), &name).await,
    };
    follow(pool, &user.id, id, pinned, true).await?;
    if shared {
        shared_now(pool, id).await?;
    }
    saved(&format!("added&new={id}#s-{id}"))
}

/// Change a feed: its own settings for whoever may edit it, and this reader's pin.
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
    let user = reader(&auth)?;
    let pool = &state.pool;
    // A built-in source is only switched on or off and given its limit: this changes feeds alone.
    let Some(source) = visible(pool, &user, id).await?.filter(|source| source.kind == "rss") else {
        return refused(StatusCode::NOT_FOUND, NO_SUCH);
    };
    let pinned = form.pinned.is_some();
    if !source.editable_by(&user) {
        // Others follow it too: the adder still says whether it is shared, and anyone who
        // follows it pins it for themselves. The rest is an admin's now.
        if !source.followed && !source.shareable_by(&user) {
            return refused(StatusCode::FORBIDDEN, NOT_YOURS);
        }
        if form.name.is_some() || form.address.is_some() {
            // The page was loaded while the feed was still theirs alone to edit.
            return kept(&state, &auth, &session, KEPT_FOR_EDIT).await;
        }
        if source.followed {
            follow(pool, &user.id, id, pinned, pinned).await?;
        }
        if source.shareable_by(&user) {
            let shared = form.shared.is_some();
            sqlx::query!("UPDATE sources SET shared = ? WHERE id = ?", shared, id).execute(pool).await?;
            if shared && !source.shared {
                shared_now(pool, id).await?;
            }
        }
        return saved("saved");
    }
    let (name, url, daily_max) = match form.checked() {
        Ok(checked) => checked,
        Err(problem) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
    };
    let extract = form.extract();
    // Pausing is an admin's: it stops the feed for everyone. Whoever may edit may share.
    let enabled = if user.is_admin() { form.enabled.is_some() } else { source.enabled };
    let shared = form.shared.is_some();
    let done = sqlx::query!(
        "UPDATE sources SET name = ?, url = ?, extract = ?, daily_max = ?, enabled = ?, shared = ?,
         check_state = 'pending', check_note = '' WHERE id = ? AND kind = 'rss'",
        name,
        url,
        extract,
        daily_max,
        enabled,
        shared,
        id
    )
    .execute(pool)
    .await;
    if let Err(error) = done {
        return finish(&state, &auth, &session, Err(error), &name).await;
    }
    if source.followed {
        follow(pool, &user.id, id, pinned, pinned).await?;
    }
    if shared && !source.shared {
        shared_now(pool, id).await?;
    }
    saved("saved")
}

#[derive(Deserialize)]
pub struct FollowForm {
    csrf: String,
    /// "1" to follow, anything else to stop.
    follow: String,
    pinned: Option<String>,
}

/// Follow a source this reader may see, or stop following one.
pub async fn set_follow(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<i64>,
    Form(form): Form<FollowForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let user = reader(&auth)?;
    let pool = &state.pool;
    if form.follow != "1" {
        sqlx::query!("DELETE FROM follows WHERE user_id = ? AND source_id = ?", user.id, id).execute(pool).await?;
        return saved("unfollowed");
    }
    let Some(source) = visible(pool, &user, id).await? else {
        return refused(StatusCode::NOT_FOUND, NO_SUCH);
    };
    // Following is choosing it; a follow made for them keeps its pin.
    follow(pool, &user.id, id, form.pinned.is_some() || source.pinned, true).await?;
    saved("followed")
}

#[derive(Deserialize)]
pub struct ToggleForm {
    csrf: String,
    /// "1" to read it, anything else to stop.
    enabled: String,
    daily_max: Option<String>,
}

#[derive(Deserialize)]
pub struct FollowNewForm {
    csrf: String,
    follow_new: Option<String>,
}

/// Whether feeds are followed for this reader when they are shared with the household.
pub async fn set_follow_new(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<FollowNewForm>,
) -> Page {
    if let Some(response) = guard(&auth, &session, &headers, &form.csrf).await? {
        return Ok(response);
    }
    let user = reader(&auth)?;
    db::set_follows_new(&state.pool, &user.id, form.follow_new.is_some()).await?;
    saved("follownew")
}

/// Switch a built-in source on or off, for everyone; admins only.
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
    if !reader(&auth)?.is_admin() {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    let daily_max = match daily_max(form.daily_max.as_deref()) {
        Ok(daily_max) => daily_max,
        Err(problem) => return finish(&state, &auth, &session, Ok(Err(problem)), "").await,
    };
    let enabled = form.enabled == "1";
    sqlx::query!(
        "UPDATE sources SET enabled = ?, daily_max = ? WHERE id = ? AND kind = 'builtin'",
        enabled,
        daily_max,
        id
    )
    .execute(&state.pool)
    .await?;
    saved("saved")
}

#[derive(Deserialize)]
pub struct CsrfForm {
    csrf: String,
}

/// Remove a feed, for whoever may edit it. Articles already delivered from it are kept.
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
    let user = reader(&auth)?;
    let pool = &state.pool;
    let Some(source) = visible(pool, &user, id).await?.filter(|source| source.kind != "builtin") else {
        return refused(StatusCode::NOT_FOUND, NO_SUCH);
    };
    if source.chosen_by_others > 0 {
        return kept(&state, &auth, &session, KEPT_FOR_REMOVAL).await;
    }
    if !source.removable_by(&user) {
        return refused(StatusCode::FORBIDDEN, NOT_YOURS);
    }
    let mut tx = pool.begin().await?;
    sqlx::query!("DELETE FROM follows WHERE source_id = ?", id).execute(&mut *tx).await?;
    sqlx::query!("DELETE FROM sources WHERE id = ? AND kind != 'builtin'", id).execute(&mut *tx).await?;
    tx.commit().await?;
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

/// Every site with a session, and who saved it.
async fn sessions(pool: &SqlitePool) -> Result<Vec<(String, String)>, sqlx::Error> {
    let rows =
        sqlx::query!(r#"SELECT domain AS "domain!", saved_by FROM sessions ORDER BY domain"#).fetch_all(pool).await?;
    Ok(rows.into_iter().map(|row| (row.domain, row.saved_by)).collect())
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

/// Whether this reader has a feed on `site`, which lets them save or replace its session.
async fn has_feed_on(pool: &SqlitePool, user: &User, site: &str) -> Result<bool, sqlx::Error> {
    let sites = [site.to_owned()];
    Ok(list(pool, user)
        .await?
        .iter()
        .any(|source| source.followed && source.kind == "rss" && covering(&sites, &source.url).is_some()))
}

#[derive(Deserialize)]
pub struct SessionForm {
    csrf: String,
    site: String,
    cookie: String,
}

/// Save a subscriber session for a site, where only the browser container reads it. Every page
/// on that site, from any feed or added by hand, is fetched with it. The cookie is never shown
/// again, never stored in the database, and sent to that one site only. A reader saves one for
/// a site they have a feed on; an admin, for any.
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
    let user = reader(&auth)?;
    let Some(site) = check_site(&form.site) else {
        let problem = "Give the site the session is for, such as ft.com.".to_owned();
        return finish(&state, &auth, &session, Ok(Err(problem)), "").await;
    };
    if !user.is_admin() && !has_feed_on(&state.pool, &user, &site).await? {
        let problem = format!("Add or follow a feed on {site} first; a session is for the pages of a site you read.");
        return finish(&state, &auth, &session, Ok(Err(problem)), "").await;
    }
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
    // Replacing a cookie makes the session the replacer's to remove.
    sqlx::query!(
        "INSERT INTO sessions (domain, created_at, saved_by) VALUES (?, ?, ?)
         ON CONFLICT (domain) DO UPDATE SET saved_by = excluded.saved_by",
        site,
        now,
        user.id
    )
    .execute(&state.pool)
    .await?;
    saved("session")
}

#[derive(Deserialize)]
pub struct RemoveSessionForm {
    csrf: String,
    site: String,
}

/// Remove a session: whoever saved it, or an admin.
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
    let user = reader(&auth)?;
    let Some(site) = check_site(&form.site) else { return saved("nosession") };
    let saved_by =
        sqlx::query_scalar!("SELECT saved_by FROM sessions WHERE domain = ?", site).fetch_optional(&state.pool).await?;
    match saved_by {
        None => return saved("nosession"),
        Some(saved_by) if saved_by != user.id && !user.is_admin() => {
            // Told apart only by a reader who sees the session anyway; to anyone else there
            // is no session to speak of.
            if has_feed_on(&state.pool, &user, &site).await? {
                return kept(
                    &state,
                    &auth,
                    &session,
                    "That session was saved by someone else; an admin can remove it.",
                )
                .await;
            }
            return saved("nosession");
        }
        Some(_) => {}
    }
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

    #[test]
    fn an_address_names_the_same_feed_whatever_its_dressing() {
        let same = same_feed("https://www.paper.example/rss");
        for url in ["http://paper.example/rss", "HTTPS://Paper.Example/rss/", " www.paper.example/rss "] {
            assert_eq!(same_feed(url), same, "{url}");
        }
        assert_ne!(same_feed("https://paper.example/rss.xml"), same);
        assert_ne!(same_feed("https://blogs.paper.example/rss"), same);
    }

    #[test]
    fn a_feed_is_covered_by_the_most_particular_session_for_its_host() {
        let sites = ["ft.com".to_owned(), "blogs.ft.com".to_owned(), "nytimes.com".to_owned()];
        assert_eq!(covering(&sites, "https://www.ft.com/rss").as_deref(), Some("ft.com"));
        assert_eq!(covering(&sites, "https://blogs.ft.com/x").as_deref(), Some("blogs.ft.com"));
        assert_eq!(covering(&sites, "https://FT.COM/").as_deref(), Some("ft.com"));
        assert_eq!(covering(&sites, "https://notft.com/rss"), None);
        assert_eq!(covering(&sites, "nonsense"), None);
    }
}
