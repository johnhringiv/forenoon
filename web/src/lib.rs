//! Forenoon's web pages: sign in, then a newsstand of what was delivered and how it was rated.

pub mod auth;
pub mod db;
pub mod limit;
pub mod mail;
pub mod pages;
pub mod ratings;
pub mod settings;
pub mod sources;

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
    pub mailer: mail::Mailer,
    /// Where the pages are reached, for links in emails.
    pub base_url: String,
    /// The data folder, where the pipeline leaves each article's thumbnail.
    pub data_dir: std::path::PathBuf,
    /// Where recipe logins are kept, apart from the data folder so that the pipeline, which
    /// reads untrusted pages, never has them. `None`: logins cannot be saved.
    pub logins_dir: Option<std::path::PathBuf>,
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
    let state = AppState {
        pool,
        sign_ins: Arc::new(Limiter::new(SIGN_IN_WINDOW)),
        mail_limits: Arc::new(Limiter::new(Duration::from_secs(3600))),
        mailer: options.mailer,
        base_url: options.base_url.trim_end_matches('/').to_owned(),
        thumbs_dir: options.data_dir.join("thumbs"),
        recipes_dir: options.data_dir.join("recipes"),
        logins_dir: options.logins_dir,
    };
    Ok(Router::new()
        .route("/", get(pages::newsstand))
        .route("/rate", post(pages::rate))
        .route("/thumb/{id}", get(pages::thumb))
        .route("/send", post(pages::send))
        .route("/report", post(pages::report))
        .route("/problems", get(pages::problems))
        .route("/account", get(pages::account))
        .route("/account/profile", post(pages::set_profile))
        .route("/account/reading", post(pages::set_reading))
        .route("/account/password", post(pages::set_password))
        .route("/account/email", post(pages::set_email))
        .route("/account/email/cancel", post(pages::cancel_email))
        .route("/account/verify", post(pages::resend_verification))
        .route("/sources", get(sources::page).post(sources::add))
        .route("/sources/{id}", post(sources::update))
        .route("/sources/recipe", post(sources::add_recipe))
        .route("/sources/{id}/delete", post(sources::delete))
        .route("/sources/{id}/toggle", post(sources::toggle))
        .route("/sources/{id}/fetch", post(sources::fetch))
        .route("/sources/{id}/login", post(sources::set_login))
        .route("/sources/{id}/login/delete", post(sources::remove_login))
        .route("/sources/{id}/session", post(sources::set_session))
        .route("/sources/{id}/session/delete", post(sources::remove_session))
        .route("/settings", get(settings::page).post(settings::save))
        .route("/logout", post(pages::logout))
        // Everything above needs a session; an anonymous request is sent to the sign-in page.
        .route_layer(middleware::from_fn(pages::require_login))
        .route("/login", get(pages::login_page).post(pages::login))
        .route("/forgot", get(pages::forgot_page).post(pages::forgot))
        .route("/reset/{token}", get(pages::reset_page).post(pages::reset))
        .route("/verify/{token}", get(pages::verify))
        .route("/healthz", get(pages::health))
        .layer(auth)
        .layer(DefaultBodyLimit::max(MAX_FORM_BYTES))
        .layer(middleware::from_fn(pages::security_headers))
        .with_state(state))
}
