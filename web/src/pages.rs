//! The pages: sign in, the newsstand, rating changes and the account page.
//!
//! Every page except sign-in and the health check needs a session, and each one reads and
//! writes only the signed-in reader's rows. Changes are form posts that must carry the
//! session's token and come from this site. Everything shown comes from fetched articles and
//! is untrusted: the templates escape it, links and images are limited to http(s), and the
//! content security policy allows no scripts.

use std::{net::SocketAddr, str::FromStr, sync::Arc};

use askama::Template;
use axum::{
    Form,
    extract::{ConnectInfo, FromRequestParts, Query, Request, State},
    http::{HeaderMap, HeaderValue, StatusCode, header, request::Parts},
    middleware::Next,
    response::{Html, IntoResponse, Redirect, Response},
};
use chrono::{DateTime, FixedOffset};
use chrono_tz::Tz;
use percent_encoding::{NON_ALPHANUMERIC, utf8_percent_encode};
use serde::Deserialize;
use sqlx::SqlitePool;
use subtle::ConstantTimeEq;
use tower_sessions::Session;

use crate::{
    auth::{self, AuthSession, Credentials, User},
    db::{self, Delivered},
    limit::Limiter,
    ratings::{self, Signal},
};

const ISSUES_PER_PAGE: usize = 14;
const MAX_DESCRIPTION_CHARS: usize = 260;
const WORDS_PER_MINUTE: f64 = 230.0;
const MAX_NAME: usize = 80;
const CSRF_KEY: &str = "csrf";
const SHOW: [(&str, &str); 3] = [("", "All"), ("rated", "Rated"), ("unrated", "Not rated")];

#[derive(Clone)]
pub struct AppState {
    pub pool: SqlitePool,
    pub sign_ins: Arc<Limiter>,
}

/// Any failure a reader cannot fix: logged here, shown as a plain 500.
pub struct AppError(String);

impl<E: std::fmt::Display> From<E> for AppError {
    fn from(error: E) -> Self {
        AppError(error.to_string())
    }
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        eprintln!("error: {}", self.0);
        (StatusCode::INTERNAL_SERVER_ERROR, "Something went wrong. See the server log.").into_response()
    }
}

type Page = Result<Response, AppError>;

fn html(status: StatusCode, page: impl Template) -> Page {
    Ok((status, Html(page.render()?)).into_response())
}

fn refused(status: StatusCode, message: &'static str) -> Page {
    Ok((status, message).into_response())
}

pub async fn security_headers(request: Request, next: Next) -> Response {
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    for (name, value) in [
        (
            header::CONTENT_SECURITY_POLICY,
            "default-src 'none'; img-src http: https:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'self'",
        ),
        // Not `no-referrer`: that makes browsers send `Origin: null` on our own form posts.
        // Links and images to article sites opt out of the referrer one by one.
        (header::REFERRER_POLICY, "same-origin"),
        (header::X_CONTENT_TYPE_OPTIONS, "nosniff"),
        (header::X_FRAME_OPTIONS, "DENY"),
        (header::CACHE_CONTROL, "no-store"),
    ] {
        headers.insert(name, HeaderValue::from_static(value));
    }
    headers.insert("x-robots-tag", HeaderValue::from_static("noindex"));
    response
}

// requests that change something

/// Whether a form post came from one of our own pages, as far as the browser says.
fn same_origin(headers: &HeaderMap) -> bool {
    let text = |name| headers.get(name).and_then(|value: &HeaderValue| value.to_str().ok());
    // The browser's own verdict, when it gives one (it does on https and localhost).
    if let Some(site) = text(header::HeaderName::from_static("sec-fetch-site")) {
        return site == "same-origin";
    }
    // Otherwise the Origin header, which form posts carry. `null` (a sandboxed or
    // referrer-less page) is not us. Our own pages send a real one because the referrer
    // policy is `same-origin`; under `no-referrer` browsers would send `null` here.
    match text(header::ORIGIN) {
        Some(origin) => text(header::HOST) == Some(origin.split_once("://").map_or(origin, |(_, host)| host)),
        None => true,
    }
}

fn random_token() -> Result<String, AppError> {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

/// The token every form on this session's pages must send back.
async fn csrf_token(session: &Session) -> Result<String, AppError> {
    if let Some(token) = session.get::<String>(CSRF_KEY).await? {
        return Ok(token);
    }
    let token = random_token()?;
    session.insert(CSRF_KEY, &token).await?;
    Ok(token)
}

/// Only our own pages may ask for a change: a form on another site has neither the token
/// nor our origin.
async fn allowed(headers: &HeaderMap, session: &Session, sent: &str) -> Result<bool, AppError> {
    let token = csrf_token(session).await?;
    Ok(same_origin(headers) && bool::from(token.as_bytes().ct_eq(sent.as_bytes())))
}

const NOT_FROM_HERE: &str = "This page is out of date. Go back, reload it and try again.";

/// The client's address, for limiting sign-in attempts.
pub struct Client(String);

impl<S: Sync> FromRequestParts<S> for Client {
    type Rejection = std::convert::Infallible;

    async fn from_request_parts(parts: &mut Parts, _: &S) -> Result<Self, Self::Rejection> {
        let address = parts.extensions.get::<ConnectInfo<SocketAddr>>().map(|info| info.0.ip().to_string());
        Ok(Client(address.unwrap_or_else(|| "unknown".to_owned())))
    }
}

/// The signed-in reader. The routes using this sit behind `login_required`, so a request
/// without one is a bug rather than an anonymous visitor.
fn reader(auth: &AuthSession) -> Result<User, AppError> {
    auth.user.clone().ok_or_else(|| AppError("no signed-in user on a protected page".to_owned()))
}

// sign in and out

#[derive(Template)]
#[template(path = "login.html")]
struct LoginPage {
    id: String,
    error: Option<&'static str>,
}

pub async fn login_page(auth: AuthSession) -> Page {
    if auth.user.is_some() {
        return Ok(Redirect::to("/").into_response());
    }
    html(StatusCode::OK, LoginPage { id: String::new(), error: None })
}

pub async fn login(
    State(state): State<AppState>,
    mut auth: AuthSession,
    Client(client): Client,
    headers: HeaderMap,
    Form(creds): Form<Credentials>,
) -> Page {
    if !same_origin(&headers) {
        return refused(StatusCode::FORBIDDEN, "Refused: this request did not come from the sign-in page.");
    }
    let id = creds.id.trim().to_owned();
    let failed =
        |status, error| html(status, LoginPage { id: id.chars().take(auth::MAX_ID).collect(), error: Some(error) });
    // Both are counted on every attempt, so neither many names from one address nor one
    // name from many addresses gets unlimited guesses.
    let by_client = state.sign_ins.allow(&format!("client {client}"));
    let by_name = state.sign_ins.allow(&format!("name {}", id.to_lowercase()));
    if !by_client || !by_name {
        return failed(StatusCode::TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.");
    }
    let Some(user) = auth.authenticate(Credentials { id: id.clone(), password: creds.password }).await? else {
        return failed(StatusCode::UNAUTHORIZED, "Wrong name or password.");
    };
    auth.login(&user).await?;
    Ok(Redirect::to("/").into_response())
}

#[derive(Deserialize)]
pub struct CsrfForm {
    csrf: String,
}

pub async fn logout(mut auth: AuthSession, session: Session, headers: HeaderMap, Form(form): Form<CsrfForm>) -> Page {
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    auth.logout().await?;
    Ok(Redirect::to("/login").into_response())
}

// the newsstand

struct Filter {
    href: String,
    label: &'static str,
    current: bool,
}

struct Star {
    n: i64,
    on: bool,
}

struct ReasonOption {
    key: &'static str,
    label: &'static str,
    selected: bool,
}

struct Card {
    id: String,
    link: Option<String>,
    image: Option<String>,
    title: String,
    kicker: String,
    desc: Option<String>,
    picked: Option<String>,
    tags: Vec<&'static str>,
    facts: Vec<String>,
    marks: Vec<String>,
    unrated: bool,
    finished: bool,
    stars: Vec<Star>,
    reasons: Vec<ReasonOption>,
}

struct Issue {
    /// When it was delivered, as stored.
    stamp: String,
    heading: String,
    count: String,
    cards: Vec<Card>,
}

#[derive(Template)]
#[template(path = "newsstand.html")]
struct NewsstandPage {
    name: String,
    csrf: String,
    summary: String,
    filters: Vec<Filter>,
    issues: Vec<Issue>,
    back: String,
    newest: Option<String>,
    older: Option<String>,
}

/// The URL when it is plain http(s) with a host and nothing a browser might read differently.
fn web_url(url: &str) -> Option<String> {
    let url = url.trim();
    let lower = url.to_ascii_lowercase();
    let rest = lower.strip_prefix("https://").or_else(|| lower.strip_prefix("http://"))?;
    let has_host = rest.chars().next().is_some_and(|c| c.is_ascii_alphanumeric() || c == '[');
    let plain = url.chars().all(|c| !c.is_control() && !c.is_whitespace() && c != '\\');
    (has_host && plain).then(|| url.to_owned())
}

fn shorten(text: &str, limit: usize) -> String {
    let text = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if text.chars().count() <= limit {
        return text;
    }
    let cut: String = text.chars().take(limit).collect();
    let cut = cut.rsplit_once(' ').map_or(cut.as_str(), |(head, _)| head);
    format!("{cut}…")
}

fn count(n: usize, noun: &str) -> String {
    format!("{n} {noun}{}", if n == 1 { "" } else { "s" })
}

fn thousands(n: i64) -> String {
    let digits = n.to_string();
    let mut out = String::new();
    for (i, digit) in digits.chars().enumerate() {
        if i > 0 && (digits.len() - i).is_multiple_of(3) {
            out.push(',');
        }
        out.push(digit);
    }
    out
}

fn card(row: Delivered) -> Card {
    let author = if row.author.to_lowercase() == row.source.to_lowercase() { "" } else { row.author.as_str() };
    let kicker =
        [row.source.as_str(), author].into_iter().filter(|part| !part.is_empty()).collect::<Vec<_>>().join(" · ");
    let policy = row.policy.as_deref();
    let mut tags = Vec::new();
    if policy == Some("pinned") {
        tags.push("Pinned");
    }
    if row.explore == Some(true) {
        tags.push("Exploration pick");
    }
    let mut facts = Vec::new();
    if let Some(score) = row.score
        && !matches!(policy, None | Some("pinned") | Some("newest-first"))
    {
        let rank = row.rank.filter(|rank| *rank > 0).map(|rank| format!(", no. {rank} of the day")).unwrap_or_default();
        facts.push(format!("Score {score}{rank}"));
    }
    if row.word_count > 0 {
        let minutes = (row.word_count as f64 / WORDS_PER_MINUTE).round().max(1.0);
        facts.push(format!("{} words, {minutes} min", thousands(row.word_count)));
    }
    let finished = row.finished == Some(true);
    let mut marks = Vec::new();
    if let Some(rating) = row.rating {
        marks.push(format!("Rated {rating} of 5"));
    }
    if finished {
        marks.push("Finished".to_owned());
    }
    if let Some(label) = ratings::label(&row.why) {
        facts.push(label.to_owned());
    }
    let reasons = match row.rating {
        Some(_) => ratings::reasons_for(row.rating)
            .iter()
            .map(|(key, label)| ReasonOption { key, label, selected: *key == row.why })
            .collect(),
        None => Vec::new(),
    };
    Card {
        link: web_url(&row.url),
        image: web_url(&row.image_url),
        title: row.title,
        kicker,
        desc: (!row.description.is_empty()).then(|| shorten(&row.description, MAX_DESCRIPTION_CHARS)),
        picked: row.picked.filter(|reason| !reason.is_empty()),
        tags,
        facts,
        marks,
        unrated: row.rating.is_none() && !finished,
        finished,
        stars: (1..=5).map(|n| Star { n, on: row.rating == Some(n) }).collect(),
        reasons,
        id: row.id,
    }
}

/// The newsstand's address for a filter and a page.
fn page_url(show: &str, before: Option<&str>) -> String {
    let mut parts = Vec::new();
    if !show.is_empty() {
        parts.push(format!("show={show}"));
    }
    if let Some(before) = before {
        parts.push(format!("before={}", utf8_percent_encode(before, NON_ALPHANUMERIC)));
    }
    if parts.is_empty() { "/".to_owned() } else { format!("/?{}", parts.join("&")) }
}

fn local(stamp: &str, zone: Tz) -> Option<DateTime<Tz>> {
    DateTime::<FixedOffset>::parse_from_rfc3339(stamp).ok().map(|at| at.with_timezone(&zone))
}

#[derive(Deserialize)]
pub struct NewsstandQuery {
    before: Option<String>,
    show: Option<String>,
}

pub async fn newsstand(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    Query(query): Query<NewsstandQuery>,
) -> Page {
    let user = reader(&auth)?;
    let zone = Tz::from_str(&user.timezone).unwrap_or(Tz::UTC);
    let show = SHOW.iter().map(|(key, _)| *key).find(|key| Some(*key) == query.show.as_deref()).unwrap_or("");
    let before = query.before.filter(|stamp| DateTime::<FixedOffset>::parse_from_rfc3339(stamp).is_ok());
    let filters =
        SHOW.iter().map(|(key, label)| Filter { href: page_url(key, None), label, current: *key == show }).collect();
    let mut page = NewsstandPage {
        name: user.name.clone(),
        csrf: csrf_token(&session).await?,
        summary: "0 articles in 0 issues. 0 rated.".to_owned(),
        filters,
        issues: Vec::new(),
        back: page_url(show, before.as_deref()),
        newest: before.as_ref().map(|_| page_url(show, None)),
        older: None,
    };
    if !db::pipeline_ready(&state.pool).await? {
        return html(StatusCode::OK, page);
    }
    let totals = db::totals(&state.pool, &user.id).await?;
    page.summary = format!(
        "{} in {}. {} rated.",
        count(totals.articles as usize, "article"),
        count(totals.issues as usize, "issue"),
        totals.rated
    );
    // One more than a page, to know whether an older page exists.
    let mut times = db::issue_times(&state.pool, &user.id, before.as_deref(), ISSUES_PER_PAGE as i64 + 1).await?;
    if times.len() > ISSUES_PER_PAGE {
        times.truncate(ISSUES_PER_PAGE);
        page.older = times.last().map(|oldest| page_url(show, Some(oldest)));
    }
    let (Some(newest), Some(oldest)) = (times.first(), times.last()) else {
        return html(StatusCode::OK, page);
    };
    for row in db::delivered(&state.pool, &user.id, oldest, newest).await? {
        if (show == "rated" && row.rating.is_none()) || (show == "unrated" && row.rating.is_some()) {
            continue;
        }
        match page.issues.last_mut() {
            Some(issue) if issue.stamp == row.delivered_at => issue.cards.push(card(row)),
            _ => page.issues.push(Issue {
                stamp: row.delivered_at.clone(),
                heading: String::new(),
                count: String::new(),
                cards: vec![card(row)],
            }),
        }
    }
    for issue in &mut page.issues {
        issue.count = count(issue.cards.len(), "article");
        match local(&issue.stamp, zone) {
            Some(when) => {
                issue.heading = when.format("%A %-d %B %Y").to_string();
                issue.count = format!("{}, sent {}", issue.count, when.format("%H:%M"));
            }
            None => issue.heading = issue.stamp.clone(),
        }
    }
    html(StatusCode::OK, page)
}

#[derive(Deserialize)]
pub struct RateForm {
    csrf: String,
    article: String,
    back: Option<String>,
    rating: Option<String>,
    finished: Option<String>,
    clear: Option<String>,
    set: Option<String>,
    reason: Option<String>,
}

/// Where to go after a change: a path on this site and nothing else.
fn safe_back(back: Option<&str>) -> &str {
    back.filter(|path| {
        path.starts_with('/') && !path.starts_with("//") && path.chars().all(|c| c.is_ascii_graphic() && c != '\\')
    })
    .unwrap_or("/")
}

/// Set, change or clear one of the reader's own ratings, then go back to the page it was on.
pub async fn rate(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<RateForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let pool = &state.pool;
    if !db::pipeline_ready(pool).await? || !db::is_delivered(pool, &user.id, &form.article).await? {
        return refused(StatusCode::NOT_FOUND, "No such delivered article.");
    }
    let now = db::get_rating(pool, &user.id, &form.article).await?;
    let rated = now.as_ref().and_then(|rating| rating.rating);
    let finished = now.as_ref().is_some_and(|rating| rating.finished);
    let chosen = form.rating.as_deref().and_then(|n| n.parse::<i64>().ok()).filter(|n| (1..=5).contains(n));
    let signal = if form.clear.is_some() {
        None
    } else if let Some(rating) = chosen {
        Some(Signal::Rated { rating, finished })
    } else if let Some(flag @ ("0" | "1")) = form.finished.as_deref() {
        match (rated, flag == "1") {
            (Some(rating), finished) => Some(Signal::Rated { rating, finished }),
            (None, true) => Some(Signal::Done),
            (None, false) => None, // finished was all there was
        }
    } else if form.set.as_deref() == Some("reason") && rated.is_some() {
        match form.reason {
            Some(reason) if ratings::fits(&reason, rated) => Some(Signal::Why(reason)),
            _ => return back_to(&form.back, &form.article), // not a reason for this rating: ignored
        }
    } else {
        return back_to(&form.back, &form.article);
    };
    match signal {
        Some(signal) => db::record_feedback(pool, &user.id, &form.article, &signal).await?,
        None => db::clear_rating(pool, &user.id, &form.article).await?,
    }
    back_to(&form.back, &form.article)
}

fn back_to(back: &Option<String>, article: &str) -> Page {
    let anchor = utf8_percent_encode(article, NON_ALPHANUMERIC);
    Ok(Redirect::to(&format!("{}#a-{anchor}", safe_back(back.as_deref()))).into_response())
}

// the account page

#[derive(Template)]
#[template(path = "account.html")]
struct AccountPage {
    user: User,
    csrf: String,
    error: Option<String>,
    notice: Option<&'static str>,
}

pub async fn account(auth: AuthSession, session: Session) -> Page {
    let page = AccountPage { user: reader(&auth)?, csrf: csrf_token(&session).await?, error: None, notice: None };
    html(StatusCode::OK, page)
}

#[derive(Deserialize)]
pub struct ProfileForm {
    csrf: String,
    name: String,
    timezone: String,
}

pub async fn set_profile(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<ProfileForm>,
) -> Page {
    let mut user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let csrf = form.csrf;
    let (name, timezone) = (form.name.trim(), form.timezone.trim());
    let problem = if name.is_empty() || name.chars().count() > MAX_NAME || name.chars().any(char::is_control) {
        Some(format!("The display name must be 1 to {MAX_NAME} characters."))
    } else if Tz::from_str(timezone).is_err() {
        Some("That is not a time zone name. Use one such as America/New_York.".to_owned())
    } else {
        None
    };
    if let Some(error) = problem {
        return html(StatusCode::BAD_REQUEST, AccountPage { user, csrf, error: Some(error), notice: None });
    }
    db::set_profile(&state.pool, &user.id, name, timezone).await?;
    (user.name, user.timezone) = (name.to_owned(), timezone.to_owned());
    html(StatusCode::OK, AccountPage { user, csrf, error: None, notice: Some("Saved.") })
}

#[derive(Deserialize)]
pub struct PasswordForm {
    csrf: String,
    current: String,
    password: String,
    again: String,
}

pub async fn set_password(
    State(state): State<AppState>,
    mut auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<PasswordForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let csrf = form.csrf;
    // Guessing the current password here is limited like guessing it at sign-in.
    let problem = if !state.sign_ins.allow(&format!("name {}", user.id.to_lowercase())) {
        Some("Too many attempts. Try again in a few minutes.".to_owned())
    } else if form.current.chars().count() > auth::MAX_PASSWORD
        || !auth::verify_password(form.current, user.password_hash.clone()).await?
    {
        Some("The current password is wrong.".to_owned())
    } else if form.password != form.again {
        Some("The two new passwords are not the same.".to_owned())
    } else {
        auth::password_problem(&form.password)
    };
    if let Some(error) = problem {
        return html(StatusCode::BAD_REQUEST, AccountPage { user, csrf, error: Some(error), notice: None });
    }
    let hash = auth::hash_password(form.password).await?;
    db::set_password(&state.pool, &user.id, &hash).await?;
    // Sessions are tied to the hash, so every browser is now signed out; sign this one back in.
    let user = User { password_hash: hash, ..user };
    auth.login(&user).await?;
    let csrf = csrf_token(&session).await.unwrap_or(csrf);
    html(StatusCode::OK, AccountPage { user, csrf, error: None, notice: Some("Password changed.") })
}

pub async fn health() -> &'static str {
    "ok"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_plain_web_links_are_kept() {
        assert_eq!(web_url(" https://blog.example/post "), Some("https://blog.example/post".to_owned()));
        for bad in ["javascript:alert(1)", "data:image/png;base64,AAAA", "https://", "http:///x", "https://a b", ""] {
            assert_eq!(web_url(bad), None, "{bad}");
        }
    }

    #[test]
    fn text_helpers() {
        assert_eq!(shorten("one  two\nthree", 50), "one two three");
        assert_eq!(shorten("one two three", 9), "one two…");
        assert_eq!(
            (thousands(999), thousands(2300), thousands(1234567)),
            ("999".into(), "2,300".into(), "1,234,567".into())
        );
        assert_eq!((count(1, "issue"), count(2, "issue")), ("1 issue".into(), "2 issues".into()));
    }

    #[test]
    fn going_back_stays_on_this_site() {
        assert_eq!(safe_back(Some("/?show=rated")), "/?show=rated");
        for bad in ["//evil.example/x", "/\\evil.example", "https://evil.example", "/a b", "/é"] {
            assert_eq!(safe_back(Some(bad)), "/", "{bad}");
        }
        assert_eq!(
            page_url("rated", Some("2026-10-10T10:00:00+00:00")),
            "/?show=rated&before=2026%2D10%2D10T10%3A00%3A00%2B00%3A00"
        );
    }

    #[test]
    fn a_form_must_come_from_this_site() {
        let headers = |pairs: &[(&'static str, &'static str)]| {
            pairs
                .iter()
                .map(|(k, v)| (header::HeaderName::from_static(k), HeaderValue::from_static(v)))
                .collect::<HeaderMap>()
        };
        assert!(same_origin(&headers(&[("origin", "http://nas:8787"), ("host", "nas:8787")])));
        assert!(!same_origin(&headers(&[("origin", "https://evil.example"), ("host", "nas:8787")])));
        assert!(!same_origin(&headers(&[("origin", "null"), ("host", "nas:8787")])));
        assert!(!same_origin(&headers(&[("sec-fetch-site", "cross-site"), ("host", "nas:8787")])));
        assert!(same_origin(&headers(&[("sec-fetch-site", "same-origin"), ("origin", "https://x"), ("host", "x")])));
        assert!(same_origin(&headers(&[("host", "nas:8787")])));
    }
}
