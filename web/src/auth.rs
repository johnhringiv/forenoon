//! Accounts and passwords.
//!
//! Passwords are stored as Argon2id hashes. Sessions are handled by `axum-login`: a session
//! is tied to the account's password hash, so changing a password signs out every other
//! browser.

use std::fmt;

use argon2::{
    Argon2,
    password_hash::{PasswordHasher, PasswordVerifier, phc::PasswordHash},
};
use axum_login::{AuthUser, AuthnBackend, UserId};
use serde::Deserialize;
use sqlx::SqlitePool;

pub const MIN_PASSWORD: usize = 10;
pub const MAX_PASSWORD: usize = 256;
pub const MAX_ID: usize = 64;
pub const ROLES: [&str; 2] = ["admin", "reader"];
pub const MAX_KINDLE_ADDRESSES: usize = 5;

impl User {
    pub fn is_admin(&self) -> bool {
        self.role == "admin"
    }

    /// Invited by an admin and yet to choose a password: it cannot sign in, and the pipeline
    /// delivers nothing to it.
    pub fn is_invited(&self) -> bool {
        self.invited_at.is_some()
    }

    /// The address still waiting to be confirmed, if any: a requested change, else an
    /// unconfirmed current one.
    pub fn unconfirmed_email(&self) -> Option<&str> {
        match (&self.pending_email, &self.email, &self.email_verified_at) {
            (Some(pending), _, _) => Some(pending),
            (None, Some(email), None) => Some(email),
            _ => None,
        }
    }
}

fn email_shaped(address: &str) -> bool {
    let shaped = address.split_once('@').is_some_and(|(name, host)| {
        !name.is_empty() && host.contains('.') && !host.starts_with('.') && !host.ends_with('.') && !host.contains('@')
    });
    shaped
        && address.len() <= 254
        && !address.chars().any(|c| c.is_whitespace() || c.is_control() || ",;<>\"".contains(c))
}

/// An address as it is stored and compared: trimmed and lowercase, so casing or stray spaces
/// cannot make two accounts of one address. `None` when it is not an email address.
pub fn normal_email(text: &str) -> Option<String> {
    let email = text.trim().to_lowercase();
    email_shaped(&email).then_some(email)
}

/// The addresses in a block of text, one per line, or what is wrong with it. Any email-shaped
/// address: Kindle's end in `@kindle.com`, but other readers take mail too (PocketBook's end in
/// `@pbsync.com`), and a reader can only ever send their own issues to the address they typed.
pub fn kindle_addresses(text: &str) -> Result<Vec<String>, String> {
    let addresses: Vec<String> =
        text.lines().map(str::trim).filter(|line| !line.is_empty()).map(str::to_owned).collect();
    if addresses.len() > MAX_KINDLE_ADDRESSES {
        return Err(format!("At most {MAX_KINDLE_ADDRESSES} Kindle addresses."));
    }
    match addresses.iter().find(|address| !email_shaped(address)) {
        Some(address) => Err(format!("{address} is not an email address.")),
        None => Ok(addresses),
    }
}

#[derive(Clone, sqlx::FromRow)]
pub struct User {
    pub id: String,
    pub name: String,
    pub password_hash: String,
    pub role: String,
    pub timezone: String,
    /// Where this reader's books are sent, one address per line.
    pub kindle_addresses: String,
    pub daily_stories: i64,
    pub interests: String,
    /// What the reader signs in with; `None` only on an account made before sign-in by email.
    pub email: Option<String>,
    pub email_verified_at: Option<String>,
    /// A requested change of address, until the link sent to it is opened.
    pub pending_email: Option<String>,
    /// When an admin invited this reader; `None` once they have joined (or were never invited).
    pub invited_at: Option<String>,
}

// Written by hand so that a logged user never includes the password hash.
impl fmt::Debug for User {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("User").field("id", &self.id).field("role", &self.role).finish_non_exhaustive()
    }
}

impl AuthUser for User {
    type Id = String;

    fn id(&self) -> String {
        self.id.clone()
    }

    fn session_auth_hash(&self) -> &[u8] {
        self.password_hash.as_bytes()
    }
}

#[derive(Debug)]
pub enum AuthError {
    Db(sqlx::Error),
    Hash(String),
}

impl fmt::Display for AuthError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            AuthError::Db(e) => write!(f, "database: {e}"),
            AuthError::Hash(e) => write!(f, "password hashing: {e}"),
        }
    }
}

impl std::error::Error for AuthError {}

impl From<sqlx::Error> for AuthError {
    fn from(e: sqlx::Error) -> Self {
        AuthError::Db(e)
    }
}

/// Why this password cannot be used, or `None` when it can.
pub fn password_problem(password: &str) -> Option<String> {
    let length = password.chars().count();
    if length < MIN_PASSWORD {
        Some(format!("The password must be at least {MIN_PASSWORD} characters."))
    } else if length > MAX_PASSWORD {
        Some(format!("The password must be at most {MAX_PASSWORD} characters."))
    } else {
        None
    }
}

/// Whether this can be an account id: the pipeline's reader ids are short plain words. At
/// least one letter or digit, so an id is never `.` or `..`, which a browser would take out of
/// the paths it appears in.
pub fn valid_id(id: &str) -> bool {
    id.len() <= MAX_ID
        && id.chars().all(|c| c.is_ascii_alphanumeric() || "_.-".contains(c))
        && id.chars().any(|c| c.is_ascii_alphanumeric())
}

/// Slow by design; call it off the async threads (see `hash_password`).
fn hash_blocking(password: &str) -> Result<String, AuthError> {
    Argon2::default()
        .hash_password(password.as_bytes())
        .map(|hash| hash.to_string())
        .map_err(|e| AuthError::Hash(e.to_string()))
}

fn verify_blocking(password: &str, stored: &str) -> bool {
    PasswordHash::new(stored)
        .map(|hash| Argon2::default().verify_password(password.as_bytes(), &hash).is_ok())
        .unwrap_or(false)
}

pub async fn hash_password(password: String) -> Result<String, AuthError> {
    tokio::task::spawn_blocking(move || hash_blocking(&password)).await.map_err(|e| AuthError::Hash(e.to_string()))?
}

pub async fn verify_password(password: String, stored: String) -> Result<bool, AuthError> {
    tokio::task::spawn_blocking(move || verify_blocking(&password, &stored))
        .await
        .map_err(|e| AuthError::Hash(e.to_string()))
}

#[derive(Clone, Deserialize)]
pub struct Credentials {
    pub email: String,
    pub password: String,
}

#[derive(Clone)]
pub struct Backend {
    pool: SqlitePool,
    /// Checked when the account does not exist, so that case takes as long as a wrong password.
    decoy_hash: String,
}

impl Backend {
    pub fn new(pool: SqlitePool) -> Result<Self, AuthError> {
        Ok(Self { pool, decoy_hash: hash_blocking("no such account")? })
    }
}

pub async fn find_user(pool: &SqlitePool, id: &str) -> Result<Option<User>, sqlx::Error> {
    sqlx::query_as!(
        User,
        r#"SELECT id AS "id!", name, password_hash, role, timezone, kindle_addresses, daily_stories, interests,
           email, email_verified_at, pending_email, invited_at
           FROM users WHERE id = ?"#,
        id
    )
    .fetch_optional(pool)
    .await
}

/// The account that signs in with this address (already normalised).
pub async fn find_user_by_email(pool: &SqlitePool, email: &str) -> Result<Option<User>, sqlx::Error> {
    sqlx::query_as!(
        User,
        r#"SELECT id AS "id!", name, password_hash, role, timezone, kindle_addresses, daily_stories, interests,
           email, email_verified_at, pending_email, invited_at
           FROM users WHERE email = ?"#,
        email
    )
    .fetch_optional(pool)
    .await
}

impl AuthnBackend for Backend {
    type User = User;
    type Credentials = Credentials;
    type Error = AuthError;

    async fn authenticate(&self, creds: Credentials) -> Result<Option<User>, AuthError> {
        if creds.password.chars().count() > MAX_PASSWORD {
            return Ok(None);
        }
        let user = match normal_email(&creds.email) {
            Some(email) => find_user_by_email(&self.pool, &email).await?,
            None => None,
        };
        // An invited account has no password yet. It is checked against the decoy like an
        // account that does not exist, so the time taken does not say which it is.
        let stored = match user.as_ref().filter(|user| !user.is_invited()) {
            Some(user) => user.password_hash.clone(),
            None => self.decoy_hash.clone(),
        };
        let matches = verify_password(creds.password, stored).await?;
        Ok(user.filter(|user| matches && !user.is_invited()))
    }

    async fn get_user(&self, id: &UserId<Self>) -> Result<Option<User>, AuthError> {
        Ok(find_user(&self.pool, id).await?)
    }
}

pub type AuthSession = axum_login::AuthSession<Backend>;
