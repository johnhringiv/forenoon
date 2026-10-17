//! The Accounts page (admins only) and the join page it leads to.
//!
//! An admin invites a reader by email address. The account exists from that moment, with no
//! password, and the address gets a link that works for a week, once. Opening it is the only
//! way in: there is no sign-up page, and the account cannot sign in until the reader has
//! chosen a password on the join page. An admin can send the link again or take the
//! invitation back, which removes the account.

use std::str::FromStr;

use askama::Template;
use axum::{
    Form,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Redirect},
};
use chrono_tz::Tz;
use serde::Deserialize;
use tower_sessions::Session;

use crate::{
    auth::{self, AuthSession, User},
    db,
    mail::{Mailer, Message},
    pages::{
        AppError, AppState, Client, MAX_NAME, NOT_FROM_HERE, Nav, Page, allowed, csrf_token, html, local, message,
        random_token, reader, refused, same_origin,
    },
};

const ADMINS_ONLY: &str = "Accounts are managed by an admin.";
const INVITE_LINK_SECONDS: i64 = 7 * 24 * 3600;
/// Invitation emails one admin may send in an hour.
const INVITES_PER_ADMIN: usize = 20;
/// Join forms one address may post in an hour: the link is a secret, but a guessed one is
/// answered the same way whatever is sent with it.
const JOIN_ATTEMPTS_PER_CLIENT: usize = 20;
const JOIN_INVALID: &str =
    "This invitation link has expired or was already used. Ask the admin who invited you for a new one.";

struct Row {
    id: String,
    name: String,
    role: String,
    email: String,
    kindle: String,
    /// "Added 3 October 2026", or how the invitation stands.
    state: String,
    invited: bool,
}

#[derive(Template)]
#[template(path = "accounts.html")]
struct AccountsPage {
    nav: Option<Nav>,
    csrf: String,
    rows: Vec<Row>,
    error: Option<String>,
    notice: Option<String>,
    /// The link just emailed, for an admin to pass on by hand when the email does not arrive.
    link: Option<String>,
    // The invite form, as typed when it was refused.
    email: String,
    id: String,
    name: String,
    role: String,
}

fn day(stamp: &str, zone: Tz) -> String {
    local(stamp, zone).map_or_else(|| stamp.to_owned(), |at| at.format("%-d %B %Y").to_string())
}

async fn rows(state: &AppState, viewer: &User) -> Result<Vec<Row>, AppError> {
    let zone = Tz::from_str(&viewer.timezone).unwrap_or(Tz::UTC);
    let now = db::now_iso();
    let rows = db::list_users(&state.pool)
        .await?
        .into_iter()
        .map(|user| {
            let state = match (&user.invited_at, &user.invite_expires_at) {
                (None, _) => format!("Added {}", day(&user.created_at, zone)),
                (Some(_), Some(until)) if *until >= now => {
                    format!("Invited; the link works until {}", day(until, zone))
                }
                (Some(_), _) => "Invited; the link has expired".to_owned(),
            };
            let kindle = db::kindle_line(&user);
            Row {
                invited: user.invited_at.is_some(),
                id: user.id,
                name: user.name,
                role: user.role,
                email: user.email.unwrap_or_else(|| "no email: cannot sign in".to_owned()),
                kindle,
                state,
            }
        })
        .collect();
    Ok(rows)
}

struct Shown {
    status: StatusCode,
    error: Option<String>,
    notice: Option<String>,
    link: Option<String>,
    typed: Option<InviteForm>,
}

async fn render(state: &AppState, user: &User, session: &Session, shown: Shown) -> Page {
    let typed = shown.typed.unwrap_or_default();
    let page = AccountsPage {
        nav: Nav::new(state, user, "accounts").await?,
        csrf: csrf_token(session).await?,
        rows: rows(state, user).await?,
        error: shown.error,
        notice: shown.notice,
        link: shown.link,
        email: typed.email,
        id: typed.id,
        name: typed.name,
        role: if typed.role.is_empty() { "reader".to_owned() } else { typed.role },
    };
    html(shown.status, page)
}

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
    let user = reader(&auth)?;
    if !user.is_admin() {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    let notice = match query.saved.as_deref() {
        Some("revoked") => Some("Invitation taken back; the account is gone.".to_owned()),
        _ => None,
    };
    let shown = Shown { status: StatusCode::OK, error: None, notice, link: None, typed: None };
    render(&state, &user, &session, shown).await
}

/// Email the link that lets an invited reader join, and return it.
async fn send_invitation(
    state: &AppState,
    admin: &User,
    id: &str,
    email: &str,
) -> Result<(String, Option<String>), AppError> {
    let token = random_token()?;
    db::new_token(&state.pool, &token, id, db::INVITE, email, INVITE_LINK_SECONDS).await?;
    let link = format!("{}/join/{token}", state.base_url);
    let approve = match &state.sender {
        Some(sender) => format!(
            "\nFor issues to reach a Kindle, its Amazon account must list {sender} under Approved Personal Document \
             E-mail List, in Personal Document Settings; the join page says where.\n"
        ),
        None => String::new(),
    };
    let body = format!(
        "{} has invited you to Forenoon, a daily issue of articles chosen for you and sent to your Kindle.\n\n\
         Open this link to choose a password and finish setting up your account:\n\n{link}\n\n\
         It works for 7 days. You will sign in as {email}.\n{approve}",
        admin.name
    );
    let mail = Message { to: email.to_owned(), subject: "You are invited to Forenoon".to_owned(), body };
    // A failure is not a reason to lose the invitation: the admin is shown the link to pass on.
    let mut failed = state.mailer.send(mail).await.err().map(|error| {
        eprintln!("error: could not send an invitation: {error}");
        format!("The email could not be sent ({error}). Give them the link below instead.")
    });
    if matches!(state.mailer, Mailer::Log) {
        failed = Some("No mail server is set, so nothing was emailed. Give them the link below.".to_owned());
    }
    Ok((link, failed))
}

#[derive(Deserialize, Default, Clone)]
pub struct InviteForm {
    #[serde(default)]
    csrf: String,
    #[serde(default)]
    email: String,
    #[serde(default)]
    id: String,
    #[serde(default)]
    name: String,
    #[serde(default)]
    role: String,
}

/// An account id from an address, when the admin gives none: the part before the @, in the
/// characters an id may have.
fn id_from_email(email: &str) -> String {
    let local = email.split('@').next().unwrap_or("");
    let id: String =
        local.chars().filter(|c| c.is_ascii_alphanumeric() || "_.-".contains(*c)).take(auth::MAX_ID).collect();
    if auth::valid_id(&id) { id } else { "reader".to_owned() }
}

pub async fn invite(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<InviteForm>,
) -> Page {
    let user = reader(&auth)?;
    if !user.is_admin() {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let typed = InviteForm {
        csrf: String::new(),
        email: form.email.trim().to_owned(),
        id: form.id.trim().to_owned(),
        name: form.name.trim().to_owned(),
        role: form.role.clone(),
    };
    let refuse = |error: String| async {
        let shown = Shown {
            status: StatusCode::BAD_REQUEST,
            error: Some(error),
            notice: None,
            link: None,
            typed: Some(typed.clone()),
        };
        render(&state, &user, &session, shown).await
    };
    let Some(email) = auth::normal_email(&typed.email) else {
        return refuse("That is not an email address.".to_owned()).await;
    };
    let id = if typed.id.is_empty() { id_from_email(&email) } else { typed.id.clone() };
    if !auth::valid_id(&id) {
        return refuse(format!(
            "The id must be 1 to {} letters, digits, '_', '.' or '-', with a letter or digit in it.",
            auth::MAX_ID
        ))
        .await;
    }
    let name = if typed.name.is_empty() { id.clone() } else { typed.name.clone() };
    if name.chars().count() > MAX_NAME || name.chars().any(char::is_control) {
        return refuse(format!("The name must be at most {MAX_NAME} characters.")).await;
    }
    if !auth::ROLES.contains(&typed.role.as_str()) {
        return refuse("Choose a role.".to_owned()).await;
    }
    if auth::find_user(&state.pool, &id).await?.is_some() {
        return refuse(format!("There is already an account {id}. Give the new one another id.")).await;
    }
    if db::email_taken(&state.pool, &email, &id).await? {
        return refuse("Another account already uses that address.".to_owned()).await;
    }
    if !state.mail_limits.allow(&format!("invite {}", user.id), INVITES_PER_ADMIN) {
        return refuse("Too many invitations for now. Try again in an hour.".to_owned()).await;
    }
    // The admin's zone to start with: a household mostly shares one. The reader sets theirs on joining.
    db::invite_user(&state.pool, &id, &email, &name, &typed.role, &user.timezone).await?;
    let (link, failed) = send_invitation(&state, &user, &id, &email).await?;
    let notice = failed.is_none().then(|| format!("Invitation sent to {email}."));
    let shown = Shown { status: StatusCode::OK, error: failed, notice, link: Some(link), typed: None };
    render(&state, &user, &session, shown).await
}

#[derive(Deserialize)]
pub struct CsrfForm {
    csrf: String,
}

/// Send the invited reader a fresh link; the earlier one stops working.
pub async fn resend(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<String>,
    Form(form): Form<CsrfForm>,
) -> Page {
    let user = reader(&auth)?;
    if !user.is_admin() {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let Some(invited) = auth::find_user(&state.pool, &id).await?.filter(User::is_invited) else {
        return refused(StatusCode::NOT_FOUND, "That is not an open invitation.");
    };
    let Some(email) = invited.email.clone() else {
        return refused(StatusCode::NOT_FOUND, "That is not an open invitation.");
    };
    if !state.mail_limits.allow(&format!("invite {}", user.id), INVITES_PER_ADMIN) {
        let shown = Shown {
            status: StatusCode::TOO_MANY_REQUESTS,
            error: Some("Too many invitations for now. Try again in an hour.".to_owned()),
            notice: None,
            link: None,
            typed: None,
        };
        return render(&state, &user, &session, shown).await;
    }
    let (link, failed) = send_invitation(&state, &user, &id, &email).await?;
    let notice = failed.is_none().then(|| format!("A new link was sent to {email}; the old one no longer works."));
    let shown = Shown { status: StatusCode::OK, error: failed, notice, link: Some(link), typed: None };
    render(&state, &user, &session, shown).await
}

/// Take an invitation back: the account and its link go. Nothing else refers to an account
/// that has never signed in.
pub async fn revoke(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(id): Path<String>,
    Form(form): Form<CsrfForm>,
) -> Page {
    let user = reader(&auth)?;
    if !user.is_admin() {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    if !db::revoke_invite(&state.pool, &id).await? {
        return refused(StatusCode::NOT_FOUND, "That is not an open invitation.");
    }
    Ok(Redirect::to("/accounts?saved=revoked").into_response())
}

// the join page, reached from the emailed link

#[derive(Template)]
#[template(path = "join.html")]
struct JoinPage {
    nav: Option<Nav>,
    token: String,
    email: String,
    name: String,
    timezone: String,
    kindle: String,
    error: Option<String>,
    /// The address issues are sent from, for the Kindle hint; `None` when no mail server is set.
    sender: Option<String>,
}

/// The invited account behind a link, when the link is good and the account still waits.
async fn invited_by(state: &AppState, token: &str) -> Result<Option<(User, String)>, AppError> {
    let Some(found) = db::read_token(&state.pool, token, db::INVITE).await? else { return Ok(None) };
    Ok(auth::find_user(&state.pool, &found.user_id).await?.filter(User::is_invited).map(|user| (user, found.email)))
}

pub async fn join_page(State(state): State<AppState>, Path(token): Path<String>) -> Page {
    let Some((user, email)) = invited_by(&state, &token).await? else {
        return message(StatusCode::BAD_REQUEST, "Link not valid", JOIN_INVALID);
    };
    let page = JoinPage {
        nav: None,
        token,
        email,
        name: user.name,
        timezone: user.timezone,
        kindle: String::new(),
        error: None,
        sender: state.sender.clone(),
    };
    html(StatusCode::OK, page)
}

#[derive(Deserialize)]
pub struct JoinForm {
    name: String,
    password: String,
    again: String,
    timezone: String,
    #[serde(default)]
    kindle_addresses: String,
}

/// The reader chooses a password and becomes the account's owner; the browser is signed in.
pub async fn join(
    State(state): State<AppState>,
    mut auth: AuthSession,
    session: Session,
    Client(client): Client,
    headers: HeaderMap,
    Path(token): Path<String>,
    Form(form): Form<JoinForm>,
) -> Page {
    if !same_origin(&headers) {
        return refused(StatusCode::FORBIDDEN, "Refused: this request did not come from this site.");
    }
    if !state.public_limits.allow(&format!("join client {client}"), JOIN_ATTEMPTS_PER_CLIENT) {
        return message(StatusCode::TOO_MANY_REQUESTS, "Too many attempts", "Try again in an hour.");
    }
    let Some((invited, email)) = invited_by(&state, &token).await? else {
        return message(StatusCode::BAD_REQUEST, "Link not valid", JOIN_INVALID);
    };
    let name = form.name.trim();
    let timezone = form.timezone.trim();
    let kindle = auth::kindle_addresses(&form.kindle_addresses);
    let problem = if name.is_empty() || name.chars().count() > MAX_NAME || name.chars().any(char::is_control) {
        Some(format!("The display name must be 1 to {MAX_NAME} characters."))
    } else if form.password != form.again {
        Some("The two passwords are not the same.".to_owned())
    } else if let Some(problem) = auth::password_problem(&form.password) {
        Some(problem)
    } else if Tz::from_str(timezone).is_err() {
        Some("That is not a time zone name. Use one such as America/New_York.".to_owned())
    } else {
        kindle.as_ref().err().cloned()
    };
    if let Some(error) = problem {
        let page = JoinPage {
            nav: None,
            token,
            email,
            name: form.name,
            timezone: form.timezone,
            kindle: form.kindle_addresses,
            error: Some(error),
            sender: state.sender.clone(),
        };
        return html(StatusCode::BAD_REQUEST, page);
    }
    let hash = auth::hash_password(form.password).await?;
    let addresses = kindle.unwrap_or_default().join("\n");
    // One transaction: the account is theirs, the link is used up, and everything shared with
    // the household so far is followed, so the first issue has something in it.
    if !db::join(&state.pool, &invited.id, name, &hash, timezone, &addresses).await? {
        return message(StatusCode::BAD_REQUEST, "Link not valid", JOIN_INVALID);
    }
    // Signed in from a fresh session, as `login` does.
    if auth.user.is_some() {
        auth.logout().await?;
        session.cycle_id().await?;
    }
    let Some(user) = auth::find_user(&state.pool, &invited.id).await? else {
        return message(StatusCode::BAD_REQUEST, "Link not valid", JOIN_INVALID);
    };
    auth.login(&user).await?;
    Ok(Redirect::to("/reading?saved=joined").into_response())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_id_is_taken_from_the_address_when_none_is_given() {
        assert_eq!(id_from_email("jane.doe@example.com"), "jane.doe");
        assert_eq!(id_from_email("j+kindle@example.com"), "jkindle");
        assert_eq!(id_from_email("+@example.com"), "reader");
        assert_eq!(id_from_email("..@example.com"), "reader", "never a path segment a browser drops");
        assert!(!auth::valid_id("..") && !auth::valid_id("-") && auth::valid_id("a.b"));
        assert!(auth::valid_id(&id_from_email(&format!("{}@example.com", "x".repeat(100)))));
    }
}
