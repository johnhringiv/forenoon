//! Forenoon's web pages: sign in, then a newsstand of what was delivered and how it was rated.

pub mod auth;
pub mod db;
pub mod limit;
pub mod pages;
pub mod ratings;

use std::{sync::Arc, time::Duration};

use axum::{
    Router,
    extract::DefaultBodyLimit,
    middleware,
    routing::{get, post},
};
use axum_login::AuthManagerLayerBuilder;
use sqlx::SqlitePool;
use tower_sessions::{
    ExpiredDeletion, Expiry, SessionManagerLayer,
    cookie::{SameSite, time},
};
use tower_sessions_sqlx_store::SqliteStore;

use crate::{auth::Backend, limit::Limiter, pages::AppState};

const SESSION_DAYS: i64 = 30;
const MAX_FORM_BYTES: usize = 32 * 1024; // room for the interests text, percent-encoded
const SIGN_IN_WINDOW: Duration = Duration::from_secs(300);

pub struct Options {
    /// Send the session cookie only over https. Needed once the pages are served that way;
    /// with it on, signing in over plain http does not work.
    pub secure_cookies: bool,
}

pub type Error = Box<dyn std::error::Error + Send + Sync>;

pub async fn app(pool: SqlitePool, options: Options) -> Result<Router, Error> {
    let store = SqliteStore::new(pool.clone());
    store.migrate().await?;
    tokio::spawn(store.clone().continuously_delete_expired(Duration::from_secs(3600)));
    let sessions = SessionManagerLayer::new(store)
        .with_name("forenoon_session")
        .with_secure(options.secure_cookies)
        .with_same_site(SameSite::Lax)
        .with_expiry(Expiry::OnInactivity(time::Duration::days(SESSION_DAYS)));
    let auth = AuthManagerLayerBuilder::new(Backend::new(pool.clone())?, sessions).build();
    let state = AppState { pool, sign_ins: Arc::new(Limiter::new(SIGN_IN_WINDOW)) };
    Ok(Router::new()
        .route("/", get(pages::newsstand))
        .route("/rate", post(pages::rate))
        .route("/account", get(pages::account))
        .route("/account/profile", post(pages::set_profile))
        .route("/account/reading", post(pages::set_reading))
        .route("/account/password", post(pages::set_password))
        .route("/logout", post(pages::logout))
        // Everything above needs a session; an anonymous request is sent to the sign-in page.
        .route_layer(middleware::from_fn(pages::require_login))
        .route("/login", get(pages::login_page).post(pages::login))
        .route("/healthz", get(pages::health))
        .layer(auth)
        .layer(DefaultBodyLimit::max(MAX_FORM_BYTES))
        .layer(middleware::from_fn(pages::security_headers))
        .with_state(state))
}
