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
    extract::{ConnectInfo, FromRequestParts, Path, Query, Request, State},
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
    mail::{Mailer, Message},
    ratings::{self, Signal},
};

const ISSUES_PER_PAGE: usize = 14;
const MAX_DESCRIPTION_CHARS: usize = 260;
const WORDS_PER_MINUTE: f64 = 230.0;
const MAX_NAME: usize = 80;
const MAX_INTERESTS: usize = 4000;
const MAX_DAILY_STORIES: i64 = 50;
const CSRF_KEY: &str = "csrf";
const SEEN_KEY: &str = "seen";
// Sign-in attempts allowed in the limiter's window: from one address, from one address for
// one name, and for one name from anywhere. The last is high enough that one address cannot
// lock a reader out by guessing at their name.
const ATTEMPTS_PER_CLIENT: usize = 30;
const ATTEMPTS_PER_CLIENT_AND_NAME: usize = 10;
const ATTEMPTS_PER_NAME: usize = 100;
// Per hour: emails to one account or address, reset requests and reset attempts from one address.
const EMAILS_PER_ACCOUNT: usize = 5;
const RESET_REQUESTS_PER_CLIENT: usize = 5;
const RESET_ATTEMPTS_PER_CLIENT: usize = 10;
const VERIFY_LINK_SECONDS: i64 = 24 * 3600;
const RESET_LINK_SECONDS: i64 = 3600;
const SHOW: [(&str, &str); 3] = [("", "All"), ("rated", "Rated"), ("unrated", "Not rated")];

#[derive(Clone)]
pub struct AppState {
    pub pool: SqlitePool,
    pub sign_ins: Arc<Limiter>,
    /// Counts emails sent and reset attempts, per hour.
    pub mail_limits: Arc<Limiter>,
    pub mailer: Mailer,
    /// Where the pages are reached, for links in emails; no trailing slash.
    pub base_url: String,
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

pub(crate) type Page = Result<Response, AppError>;

pub(crate) fn html(status: StatusCode, page: impl Template) -> Page {
    Ok((status, Html(page.render()?)).into_response())
}

pub(crate) fn refused(status: StatusCode, message: &'static str) -> Page {
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
pub(crate) async fn csrf_token(session: &Session) -> Result<String, AppError> {
    if let Some(token) = session.get::<String>(CSRF_KEY).await? {
        return Ok(token);
    }
    let token = random_token()?;
    session.insert(CSRF_KEY, &token).await?;
    Ok(token)
}

/// Only our own pages may ask for a change: a form on another site has neither the token
/// nor our origin.
pub(crate) async fn allowed(headers: &HeaderMap, session: &Session, sent: &str) -> Result<bool, AppError> {
    let token = csrf_token(session).await?;
    Ok(same_origin(headers) && bool::from(token.as_bytes().ct_eq(sent.as_bytes())))
}

pub(crate) const NOT_FROM_HERE: &str = "This page is out of date. Go back, reload it and try again.";

/// The client's address, for limiting sign-in attempts.
pub struct Client(String);

impl<S: Sync> FromRequestParts<S> for Client {
    type Rejection = std::convert::Infallible;

    async fn from_request_parts(parts: &mut Parts, _: &S) -> Result<Self, Self::Rejection> {
        let address = parts.extensions.get::<ConnectInfo<SocketAddr>>().map(|info| info.0.ip().to_string());
        Ok(Client(address.unwrap_or_else(|| "unknown".to_owned())))
    }
}

/// The signed-in reader. The routes using this sit behind `require_login`, so a request
/// without one is a bug rather than an anonymous visitor.
pub(crate) fn reader(auth: &AuthSession) -> Result<User, AppError> {
    auth.user.clone().ok_or_else(|| AppError("no signed-in user on a protected page".to_owned()))
}

// sign in and out

/// Sends anyone without a session to the sign-in page. A 303, so a form posted from a page
/// whose session has ended becomes a plain visit to sign-in instead of being posted there.
pub async fn require_login(auth: AuthSession, session: Session, request: Request, next: Next) -> Response {
    if auth.user.is_none() {
        return Redirect::to("/login").into_response();
    }
    // A session lasts 30 days from its last use, but its expiry only moves when it is saved,
    // and it is saved only when changed. Noting the day does that once a day.
    let today = chrono::Utc::now().date_naive().to_string();
    if session.get::<String>(SEEN_KEY).await.ok().flatten().as_deref() != Some(today.as_str())
        && let Err(error) = session.insert(SEEN_KEY, &today).await
    {
        eprintln!("error: could not extend a session: {error}");
    }
    next.run(request).await
}

#[derive(Template)]
#[template(path = "login.html")]
struct LoginPage {
    email: String,
    error: Option<&'static str>,
    notice: Option<&'static str>,
}

#[derive(Deserialize)]
pub struct LoginQuery {
    reset: Option<String>,
}

pub async fn login_page(auth: AuthSession, Query(query): Query<LoginQuery>) -> Page {
    if auth.user.is_some() {
        return Ok(Redirect::to("/").into_response());
    }
    let notice = query.reset.map(|_| "Password changed. Sign in with the new one.");
    html(StatusCode::OK, LoginPage { email: String::new(), error: None, notice })
}

pub async fn login(
    State(state): State<AppState>,
    mut auth: AuthSession,
    session: Session,
    Client(client): Client,
    headers: HeaderMap,
    Form(creds): Form<Credentials>,
) -> Page {
    if !same_origin(&headers) {
        return refused(StatusCode::FORBIDDEN, "Refused: this request did not come from the sign-in page.");
    }
    let email: String = creds.email.trim().to_lowercase().chars().take(254).collect();
    let failed = |status, error| html(status, LoginPage { email: email.clone(), error: Some(error), notice: None });
    // Checked in this order and stopping at the first refusal, so a blocked address cannot
    // fill the table with names.
    let allowed = state.sign_ins.allow(&format!("client {client}"), ATTEMPTS_PER_CLIENT)
        && state.sign_ins.allow(&format!("client {client} name {email}"), ATTEMPTS_PER_CLIENT_AND_NAME)
        && state.sign_ins.allow(&format!("name {email}"), ATTEMPTS_PER_NAME);
    if !allowed {
        return failed(StatusCode::TOO_MANY_REQUESTS, "Too many attempts. Try again in a few minutes.");
    }
    // Start from a fresh session. One that is already signed in would keep its id and its
    // token, and `logout` alone empties it without changing the id.
    if auth.user.is_some() {
        auth.logout().await?;
        session.cycle_id().await?;
    }
    let Some(user) = auth.authenticate(Credentials { email: email.clone(), password: creds.password }).await? else {
        return failed(StatusCode::UNAUTHORIZED, "Wrong email or password.");
    };
    auth.login(&user).await?;
    Ok(Redirect::to("/").into_response())
}

// emailed links: confirming an address, resetting a password

#[derive(Template)]
#[template(path = "message.html")]
struct MessagePage {
    heading: &'static str,
    text: String,
}

fn message(status: StatusCode, heading: &'static str, text: impl Into<String>) -> Page {
    html(status, MessagePage { heading, text: text.into() })
}

/// Email a link that confirms `email` for this account. Refused past a few an hour.
async fn send_verification(state: &AppState, user: &User, email: &str) -> Result<bool, AppError> {
    if !state.mail_limits.allow(&format!("verify {}", user.id), EMAILS_PER_ACCOUNT) {
        return Ok(false);
    }
    let token = random_token()?;
    db::new_token(&state.pool, &token, &user.id, db::VERIFY, email, VERIFY_LINK_SECONDS).await?;
    let body = format!(
        "Open this link to confirm {email} for your Forenoon account:\n\n{}/verify/{token}\n\n\
         It works for 24 hours. Once confirmed, this address can be used to reset a forgotten password.\n\
         If you did not ask for this, ignore this message.\n",
        state.base_url
    );
    state
        .mailer
        .send(Message { to: email.to_owned(), subject: "Confirm your email for Forenoon".to_owned(), body })
        .await?;
    Ok(true)
}

/// The link in a confirmation email. It needs no session: it may be opened on another device.
pub async fn verify(State(state): State<AppState>, Path(token): Path<String>) -> Page {
    let invalid = || {
        message(
            StatusCode::BAD_REQUEST,
            "Link not valid",
            "This link has expired or was already replaced by a newer one. Sign in and send a new one from your account page.",
        )
    };
    let Some(found) = db::read_token(&state.pool, &token, db::VERIFY).await? else {
        return invalid();
    };
    db::drop_tokens(&state.pool, &found.user_id, db::VERIFY).await?;
    if !db::confirm_email(&state.pool, &found.user_id, &found.email).await? {
        return invalid();
    }
    // A reset link sent to the previous address must not outlive the change.
    db::drop_tokens(&state.pool, &found.user_id, db::RESET).await?;
    message(
        StatusCode::OK,
        "Email confirmed",
        format!("{} is confirmed. You sign in with it, and it can be used to reset a forgotten password.", found.email),
    )
}

/// Send the confirmation email again, from the banner.
pub async fn resend_verification(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<CsrfForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let Some(email) = user.unconfirmed_email().map(str::to_owned) else {
        return Ok(Redirect::to("/account").into_response());
    };
    let sent = send_verification(&state, &user, &email).await?;
    Ok(Redirect::to(if sent { "/account?saved=sent" } else { "/account?saved=wait" }).into_response())
}

#[derive(Template)]
#[template(path = "forgot.html")]
struct ForgotPage {
    error: Option<&'static str>,
}

pub async fn forgot_page() -> Page {
    html(StatusCode::OK, ForgotPage { error: None })
}

#[derive(Deserialize)]
pub struct ForgotForm {
    email: String,
}

/// Email a reset link, when the address is a confirmed one. The answer is the same either
/// way, so this page cannot be used to find out who has an account.
pub async fn forgot(
    State(state): State<AppState>,
    Client(client): Client,
    headers: HeaderMap,
    Form(form): Form<ForgotForm>,
) -> Page {
    if !same_origin(&headers) {
        return refused(StatusCode::FORBIDDEN, "Refused: this request did not come from this site.");
    }
    if !state.mail_limits.allow(&format!("forgot client {client}"), RESET_REQUESTS_PER_CLIENT) {
        return html(
            StatusCode::TOO_MANY_REQUESTS,
            ForgotPage { error: Some("Too many requests. Try again in an hour.") },
        );
    }
    let user = match auth::normal_email(&form.email) {
        Some(email) => auth::find_user_by_email(&state.pool, &email).await?,
        None => None,
    };
    if let Some(user) = user.filter(|user| user.email_verified_at.is_some())
        && let Some(email) = user.email.clone()
        && state.mail_limits.allow(&format!("reset {}", user.id), EMAILS_PER_ACCOUNT)
    {
        let token = random_token()?;
        db::new_token(&state.pool, &token, &user.id, db::RESET, &email, RESET_LINK_SECONDS).await?;
        let body = format!(
            "Open this link to choose a new password for your Forenoon account:\n\n{}/reset/{token}\n\n\
             It works for 60 minutes. If you did not ask for this, ignore this message; your password is unchanged.\n",
            state.base_url
        );
        let mail = Message { to: email, subject: "Reset your Forenoon password".to_owned(), body };
        // A failure is logged, not shown: the page must not reveal whether the address has an account.
        if let Err(error) = state.mailer.send(mail).await {
            eprintln!("error: could not send a reset email: {error}");
        }
    }
    message(
        StatusCode::OK,
        "Check your email",
        "If that address belongs to an account and has been confirmed, a link to reset the password is on its way. It works for 60 minutes.",
    )
}

#[derive(Template)]
#[template(path = "reset.html")]
struct ResetPage {
    token: String,
    error: Option<String>,
}

const RESET_INVALID: &str = "This link has expired or was already used. Ask for a new one from the sign-in page.";

pub async fn reset_page(State(state): State<AppState>, Path(token): Path<String>) -> Page {
    if db::read_token(&state.pool, &token, db::RESET).await?.is_none() {
        return message(StatusCode::BAD_REQUEST, "Link not valid", RESET_INVALID);
    }
    html(StatusCode::OK, ResetPage { token, error: None })
}

#[derive(Deserialize)]
pub struct ResetForm {
    password: String,
    again: String,
}

pub async fn reset(
    State(state): State<AppState>,
    Client(client): Client,
    headers: HeaderMap,
    Path(token): Path<String>,
    Form(form): Form<ResetForm>,
) -> Page {
    if !same_origin(&headers) {
        return refused(StatusCode::FORBIDDEN, "Refused: this request did not come from this site.");
    }
    if !state.mail_limits.allow(&format!("reset client {client}"), RESET_ATTEMPTS_PER_CLIENT) {
        return message(StatusCode::TOO_MANY_REQUESTS, "Too many attempts", "Try again in an hour.");
    }
    let Some(found) = db::read_token(&state.pool, &token, db::RESET).await? else {
        return message(StatusCode::BAD_REQUEST, "Link not valid", RESET_INVALID);
    };
    let problem = if form.password != form.again {
        Some("The two passwords are not the same.".to_owned())
    } else {
        auth::password_problem(&form.password)
    };
    if problem.is_some() {
        return html(StatusCode::BAD_REQUEST, ResetPage { token, error: problem });
    }
    let hash = auth::hash_password(form.password).await?;
    // Used once. Changing the hash also signs the account out everywhere.
    db::drop_tokens(&state.pool, &found.user_id, db::RESET).await?;
    db::set_password(&state.pool, &found.user_id, &hash).await?;
    Ok(Redirect::to("/login?reset=1").into_response())
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
    short: &'static str,
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
    /// The question the reason buttons answer.
    question: &'static str,
    /// Whether a "Send to Kindle" button is offered, and what became of the last request.
    sendable: bool,
    sent: Option<String>,
    /// Whether the reader has reported this article as broken or incomplete.
    reported: bool,
}

struct Issue {
    /// When it was delivered, as stored.
    stamp: String,
    heading: String,
    count: String,
    stories: Vec<Story>,
}

/// What one card on the page holds: an article, or a story's articles with its lead first.
struct Story {
    /// The story's key (its lead article's id), or "" for an article on its own.
    key: String,
    /// The heading over the articles after the lead.
    more: String,
    cards: Vec<Card>,
}

#[derive(Template)]
#[template(path = "newsstand.html")]
struct NewsstandPage {
    name: String,
    /// Whether to show the links only an admin can use.
    admin: bool,
    /// How many problem reports there are, for the admin's link.
    problems: i64,
    csrf: String,
    /// The address still to be confirmed, for the banner.
    unconfirmed: Option<String>,
    summary: String,
    filters: Vec<Filter>,
    issues: Vec<Issue>,
    /// Shown when there are no issues.
    empty: &'static str,
    back: String,
    newest: Option<String>,
    older: Option<String>,
}

/// The URL when it is plain http(s) with a host and nothing a browser might read differently.
pub(crate) fn web_url(url: &str) -> Option<String> {
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
            .map(|(key, label)| ReasonOption { key, short: ratings::short(key), label, selected: *key == row.why })
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
        question: ratings::question(row.rating),
        sendable: false,
        sent: None,
        reported: false,
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
        admin: user.is_admin(),
        problems: 0,
        csrf: csrf_token(&session).await?,
        unconfirmed: user.unconfirmed_email().map(str::to_owned),
        summary: "0 articles in 0 issues. 0 rated.".to_owned(),
        filters,
        issues: Vec::new(),
        empty: if show.is_empty() { "Nothing has been delivered yet." } else { "No articles match this filter." },
        back: page_url(show, before.as_deref()),
        newest: before.as_ref().map(|_| page_url(show, None)),
        older: None,
    };
    if !db::pipeline_ready(&state.pool).await? {
        return html(StatusCode::OK, page);
    }
    if user.is_admin() {
        page.problems = db::problem_count(&state.pool).await?;
    }
    let totals = db::totals(&state.pool, &user.id).await?;
    page.summary = format!(
        "{} in {}. {} rated.",
        count(totals.articles as usize, "article"),
        count(totals.issues as usize, "issue"),
        totals.rated
    );
    // One more than a page, to know whether an older page exists.
    let mut times = db::issue_times(&state.pool, &user.id, before.as_deref(), show, ISSUES_PER_PAGE as i64 + 1).await?;
    if times.len() > ISSUES_PER_PAGE {
        times.truncate(ISSUES_PER_PAGE);
        page.older = times.last().map(|oldest| page_url(show, Some(oldest)));
    }
    let (Some(newest), Some(oldest)) = (times.first(), times.last()) else {
        return html(StatusCode::OK, page);
    };
    for row in db::delivered(&state.pool, &user.id, oldest, newest, show).await? {
        if page.issues.last().is_none_or(|issue| issue.stamp != row.delivered_at) {
            page.issues.push(Issue {
                stamp: row.delivered_at.clone(),
                heading: String::new(),
                count: String::new(),
                stories: Vec::new(),
            });
        }
        let Some(issue) = page.issues.last_mut() else { continue };
        let (key, leads) = (row.story.clone(), row.story == row.id);
        // Articles of one story are not always next to each other in the ranking order.
        match issue.stories.iter_mut().find(|story| !key.is_empty() && story.key == key) {
            Some(story) if leads => story.cards.insert(0, card(row)),
            Some(story) => story.cards.push(card(row)),
            None => issue.stories.push(Story { key, more: String::new(), cards: vec![card(row)] }),
        }
    }
    // What each article's "Send to Kindle" shows: offered to a reader with a Kindle address,
    // unless a request is already waiting.
    let can_send = !user.kindle_addresses.trim().is_empty();
    let requests = db::send_states(&state.pool, &user.id).await?;
    let reported = db::reported(&state.pool, &user.id).await?;
    for card in page.issues.iter_mut().flat_map(|issue| &mut issue.stories).flat_map(|story| &mut story.cards) {
        let last = requests.iter().rev().find(|request| request.article_id == card.id);
        card.reported = reported.contains(&card.id);
        card.sendable = can_send && last.is_none_or(|request| request.status != "queued");
        card.sent = last.map(|request| match request.status.as_str() {
            "queued" => "On its way to your Kindle, within a few minutes.".to_owned(),
            "sent" => {
                let day = local(&request.requested_at, zone).map(|at| at.format("%-d %B").to_string());
                format!("Sent to your Kindle{}.", day.map(|day| format!(" on {day}")).unwrap_or_default())
            }
            _ => format!("Could not be sent: {}", request.error),
        });
    }
    for issue in &mut page.issues {
        let articles: usize = issue.stories.iter().map(|story| story.cards.len()).sum();
        issue.count = count(articles, "article");
        if articles > issue.stories.len() {
            let stories = issue.stories.len();
            issue.count = format!("{} in {stories} {}", issue.count, if stories == 1 { "story" } else { "stories" });
        }
        for story in &mut issue.stories {
            story.more = format!("{} more on this story", story.cards.len().saturating_sub(1));
        }
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
    reason: Option<String>,
}

/// Tell whoever runs the install that an article came out broken or incomplete. This is about
/// the delivery, so it is kept apart from the rating and leaves it as it was.
pub async fn report(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<SendForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    if !db::pipeline_ready(&state.pool).await? || !db::is_delivered(&state.pool, &user.id, &form.article).await? {
        return refused(StatusCode::NOT_FOUND, "No such delivered article.");
    }
    if !db::reported(&state.pool, &user.id).await?.contains(&form.article) {
        db::report_problem(&state.pool, &user.id, &form.article).await?;
    }
    back_to(&form.back, &form.article)
}

struct ProblemRow {
    when: String,
    who: String,
    title: String,
    link: Option<String>,
    source: String,
    detail: String,
}

#[derive(Template)]
#[template(path = "problems.html")]
struct ProblemsPage {
    name: String,
    csrf: String,
    rows: Vec<ProblemRow>,
}

/// For admins: the articles readers reported as broken or incomplete.
pub async fn problems(State(state): State<AppState>, auth: AuthSession, session: Session) -> Page {
    let user = reader(&auth)?;
    if !user.is_admin() {
        return refused(StatusCode::FORBIDDEN, "Problem reports are for an admin.");
    }
    let zone = Tz::from_str(&user.timezone).unwrap_or(Tz::UTC);
    let found = if db::pipeline_ready(&state.pool).await? { db::problems(&state.pool, 200).await? } else { Vec::new() };
    let rows = found
        .into_iter()
        .map(|problem| ProblemRow {
            when: local(&problem.reported_at, zone)
                .map_or(problem.reported_at.clone(), |at| at.format("%-d %B %Y, %H:%M").to_string()),
            who: format!(
                "{}, {}",
                problem.user_id,
                if problem.via == "web" { "on the web pages" } else { "from the Kindle" }
            ),
            link: web_url(&problem.url),
            title: problem.title,
            source: problem.source,
            detail: format!(
                "{} words. Extractor: {}",
                thousands(problem.words),
                if problem.extraction.is_empty() { "no record" } else { &problem.extraction }
            ),
        })
        .collect();
    html(StatusCode::OK, ProblemsPage { name: user.name, csrf: csrf_token(&session).await?, rows })
}

#[derive(Deserialize)]
pub struct SendForm {
    csrf: String,
    article: String,
    back: Option<String>,
}

/// Ask for one delivered article to be sent to the reader's own Kindle again. This only queues
/// it; the pipeline builds and sends the book.
pub async fn send(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<SendForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    if user.kindle_addresses.trim().is_empty() {
        return refused(StatusCode::BAD_REQUEST, "Your account has no Kindle address; an admin sets it.");
    }
    if !db::pipeline_ready(&state.pool).await? || !db::is_delivered(&state.pool, &user.id, &form.article).await? {
        return refused(StatusCode::NOT_FOUND, "No such delivered article.");
    }
    match db::queue_send(&state.pool, &user.id, &form.article).await? {
        db::Queued::TooMany => {
            refused(StatusCode::TOO_MANY_REQUESTS, "That is the most articles that can be sent in a day.")
        }
        db::Queued::Added | db::Queued::Waiting => back_to(&form.back, &form.article),
    }
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
    } else if rated.is_some() {
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

#[derive(Deserialize)]
pub struct AccountQuery {
    saved: Option<String>,
}

pub async fn account(auth: AuthSession, session: Session, Query(query): Query<AccountQuery>) -> Page {
    let notice = match query.saved.as_deref() {
        Some("profile") | Some("reading") => Some("Saved."),
        Some("sent") => Some("Confirmation email sent. Open the link in it within 24 hours."),
        Some("wait") => Some("Too many confirmation emails for now. Try again in an hour."),
        Some("email") => Some("Email change cancelled."),
        Some("password") => Some("Password changed."),
        _ => None,
    };
    let page = AccountPage { user: reader(&auth)?, csrf: csrf_token(&session).await?, error: None, notice };
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
    let user = reader(&auth)?;
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
    // A redirect, so reloading the result does not post the form again.
    Ok(Redirect::to("/account?saved=profile").into_response())
}

#[derive(Deserialize)]
pub struct EmailForm {
    csrf: String,
    email: String,
    current: String,
}

/// Change the address the reader signs in with. It needs the password, because whoever
/// controls the address can reset the password. The change waits for the new address to be
/// confirmed; until then the old one keeps working.
pub async fn set_email(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<EmailForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let email = auth::normal_email(&form.email);
    let problem = if !state.sign_ins.allow(&format!("change {}", user.id), ATTEMPTS_PER_CLIENT_AND_NAME) {
        Some("Too many attempts. Try again in a few minutes.")
    } else if form.current.chars().count() > auth::MAX_PASSWORD
        || !auth::verify_password(form.current, user.password_hash.clone()).await?
    {
        Some("The current password is wrong.")
    } else {
        match &email {
            None => Some("That is not an email address."),
            Some(email) if user.email.as_deref() == Some(email) && user.pending_email.is_none() => {
                Some("That is already your email address.")
            }
            Some(email) if db::email_taken(&state.pool, email, &user.id).await? => {
                Some("Another account already uses that address.")
            }
            Some(_) => None,
        }
    };
    if let (None, Some(email)) = (problem, email) {
        if user.email.as_deref() == Some(email.as_str()) {
            // Back to the current address: drop the pending change.
            db::cancel_email_change(&state.pool, &user.id).await?;
            return Ok(Redirect::to("/account?saved=email").into_response());
        }
        db::request_email(&state.pool, &user.id, &email).await?;
        let sent = send_verification(&state, &user, &email).await?;
        return Ok(Redirect::to(if sent { "/account?saved=sent" } else { "/account?saved=wait" }).into_response());
    }
    let page = AccountPage { user, csrf: form.csrf, error: problem.map(str::to_owned), notice: None };
    html(StatusCode::BAD_REQUEST, page)
}

pub async fn cancel_email(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<CsrfForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    db::cancel_email_change(&state.pool, &user.id).await?;
    Ok(Redirect::to("/account?saved=email").into_response())
}

#[derive(Deserialize)]
pub struct ReadingForm {
    csrf: String,
    interests: String,
    daily_stories: String,
    /// Sent only by an admin's form; anyone else's is ignored.
    kindle_addresses: Option<String>,
}

/// What the pipeline should pick for this reader, how much, and (admins only) where it is sent.
pub async fn set_reading(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<ReadingForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let interests = form.interests.replace("\r\n", "\n");
    let interests = interests.trim();
    let daily = form.daily_stories.trim().parse::<i64>().ok().filter(|n| (1..=MAX_DAILY_STORIES).contains(n));
    // The addresses decide where content is emailed, so only an admin may change them.
    let addresses = form.kindle_addresses.filter(|_| user.is_admin()).map(|text| auth::kindle_addresses(&text));
    let problem = if interests.chars().count() > MAX_INTERESTS {
        Some(format!("Interests can be at most {MAX_INTERESTS} characters."))
    } else if daily.is_none() {
        Some(format!("Stories a day must be a number from 1 to {MAX_DAILY_STORIES}."))
    } else if let Some(Err(problem)) = &addresses {
        Some(problem.clone())
    } else {
        None
    };
    if let (None, Some(daily)) = (&problem, daily) {
        db::set_reading(&state.pool, &user.id, interests, daily).await?;
        if let Some(Ok(addresses)) = addresses {
            db::set_kindle_addresses(&state.pool, &user.id, &addresses.join("\n")).await?;
        }
        return Ok(Redirect::to("/account?saved=reading").into_response());
    }
    html(StatusCode::BAD_REQUEST, AccountPage { user, csrf: form.csrf, error: problem, notice: None })
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
    let problem = if !state.sign_ins.allow(&format!("change {}", user.id), ATTEMPTS_PER_CLIENT_AND_NAME) {
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
    Ok(Redirect::to("/account?saved=password").into_response())
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
