//! Forenoon's web pages: sign in, then a newsstand of what was delivered and how it was rated.
//! Also the answer to the rating links in delivered books, which need no sign-in.

pub mod accounts;
pub mod archive;
pub mod auth;
pub mod db;
pub mod feedback;
mod hex;
pub mod icons;
pub mod landing;
pub mod limit;
pub mod mail;
pub mod pages;
pub mod proxy;
pub mod ratings;
pub mod reading;
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
use tower_http::compression::CompressionLayer;
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
    /// The pages are served over https: the session cookie is sent only that way (as
    /// `__Host-forenoon_session`), and browsers are told to use nothing else. With it on,
    /// signing in over plain http does not work, so when `base_url` is an https address a
    /// request for any other host is redirected there.
    pub secure_cookies: bool,
    pub mailer: mail::Mailer,
    /// Where the pages are reached, for links in emails.
    pub base_url: String,
    /// The data folder, where the pipeline leaves each article's thumbnail.
    pub data_dir: std::path::PathBuf,
    /// Where subscriber session cookies are kept, apart from the data folder so that the
    /// pipeline, which reads untrusted pages, never has them. `None`: sessions cannot be saved.
    pub logins_dir: Option<std::path::PathBuf>,
    /// Who a request is from, when the pages are behind a proxy or a tunnel.
    pub proxy: proxy::Proxy,
    /// The secret the pipeline signs rating links with (`FORENOON_FEEDBACK_SECRET`).
    pub feedback_secret: Option<Vec<u8>>,
    /// The address issues are sent from (`FORENOON_SMTP_SENDER`), which every Kindle must
    /// approve; shown beside the Kindle address fields. `None` when it is not set.
    pub sender: Option<String>,
    /// The hostname the landing page is served at (`FORENOON_WEB_LANDING`); see `landing`.
    /// `None` for an ordinary install.
    pub landing: Option<String>,
}

pub type Error = Box<dyn std::error::Error + Send + Sync>;

pub async fn app(pool: SqlitePool, options: Options) -> Result<Router, Error> {
    let store = SqliteStore::new(pool.clone());
    store.migrate().await?;
    tokio::spawn(store.clone().continuously_delete_expired(Duration::from_secs(3600)));
    // The __Host- prefix makes browsers refuse the cookie unless it is secure, set by this
    // host for every path, and has no Domain: another site under the same domain cannot plant one.
    let name = if options.secure_cookies { "__Host-forenoon_session" } else { "forenoon_session" };
    let sessions = SessionManagerLayer::new(store)
        .with_name(name)
        .with_secure(options.secure_cookies)
        .with_same_site(SameSite::Lax)
        .with_expiry(Expiry::OnInactivity(time::Duration::days(SESSION_DAYS)));
    let auth = AuthManagerLayerBuilder::new(Backend::new(pool.clone())?, sessions).build();
    let state = AppState {
        pool,
        sign_ins: Arc::new(Limiter::new(SIGN_IN_WINDOW)),
        mail_limits: Arc::new(Limiter::new(Duration::from_secs(3600))),
        public_limits: Arc::new(Limiter::evicting(Duration::from_secs(3600))),
        mailer: options.mailer,
        base_url: options.base_url.trim_end_matches('/').to_owned(),
        thumbs_dir: options.data_dir.join("thumbs"),
        sessions_dir: options.data_dir.join("sessions"),
        logins_dir: options.logins_dir,
        proxy: Arc::new(options.proxy),
        feedback_secret: options.feedback_secret.map(Arc::from),
        sender: options.sender.filter(|sender| !sender.is_empty()),
        // Evicting: these links are public, and a flood of addresses must not stop a Kindle.
        taps: Arc::new(Limiter::evicting(Duration::from_secs(60))),
    };
    let router = Router::new()
        .route("/", get(pages::newsstand))
        .route("/archive", get(archive::page).post(archive::add))
        .route("/archive/added/clear", post(archive::clear_failed))
        .route("/archive/refresh", post(archive::refresh))
        .route("/rate", post(pages::rate))
        .route("/thumb/{id}", get(pages::thumb))
        .route("/send", post(pages::send))
        .route("/report", post(pages::report))
        .route("/problems", get(pages::problems))
        .route("/account", get(pages::account))
        .route("/account/profile", post(pages::set_profile))
        .route("/account/reading", post(pages::set_reading))
        .route("/account/kindle", post(pages::set_kindle))
        .route("/account/password", post(pages::set_password))
        .route("/account/email", post(pages::set_email))
        .route("/account/email/cancel", post(pages::cancel_email))
        .route("/account/verify", post(pages::resend_verification))
        .route("/reading", get(reading::page))
        .route("/learned", get(reading::learned).post(reading::save))
        .route("/learned/restore", post(reading::restore))
        .route("/learned/switch", post(reading::switch))
        .route("/sources", get(sources::page).post(sources::add))
        .route("/sources/{id}", post(sources::update))
        .route("/sources/{id}/delete", post(sources::delete))
        .route("/sources/{id}/toggle", post(sources::toggle))
        .route("/sources/{id}/follow", post(sources::set_follow))
        .route("/sources/follow-new", post(sources::set_follow_new))
        .route("/sources/sessions", post(sources::set_session))
        .route("/sources/sessions/delete", post(sources::remove_session))
        .route("/settings", get(settings::page).post(settings::save))
        .route("/accounts", get(accounts::page).post(accounts::invite))
        .route("/accounts/{id}/resend", post(accounts::resend))
        .route("/accounts/{id}/revoke", post(accounts::revoke))
        .route("/logout", post(pages::logout))
        // Everything above needs a session; an anonymous request is sent to the sign-in page.
        .route_layer(middleware::from_fn(pages::require_login))
        .route("/login", get(pages::login_page).post(pages::login))
        .route("/forgot", get(pages::forgot_page).post(pages::forgot))
        .route("/reset/{token}", get(pages::reset_page).post(pages::reset))
        .route("/verify/{token}", get(pages::verify))
        .route("/join/{token}", get(accounts::join_page).post(accounts::join))
        .route("/healthz", get(pages::health))
        .route("/f/{token}", get(feedback::tap));
    let router = icons::FILES.iter().fold(router, |router, &(path, media_type, body)| {
        router.route(path, get(move || async move { icons::serve(media_type, body) }))
    });
    let router = router
        // Signed in or not: it names no page, so it gives nothing away.
        .fallback(pages::not_found)
        .layer(auth)
        .layer(DefaultBodyLimit::max(MAX_FORM_BYTES))
        .layer(middleware::from_fn(pages::security_headers))
        // Text only: images are already compressed, and tiny answers are not worth it.
        .layer(CompressionLayer::new());
    let router = if options.secure_cookies { router.layer(middleware::from_fn(pages::https_only)) } else { router };
    // Outermost, so a request for another host is sent on before anything else looks at it.
    let public = options.secure_cookies.then(|| pages::PublicAddress::new(&options.base_url, state.proxy.clone()));
    let router = match public.flatten() {
        Some(public) => router.layer(middleware::from_fn_with_state(public, pages::to_public_address)),
        None => router,
    };
    // Outside everything: the landing host is decided on before any other layer sees the request.
    let router = match options.landing {
        Some(host) => router.layer(middleware::from_fn_with_state(Arc::<str>::from(host), landing::gate)),
        None => router,
    };
    Ok(router.with_state(state))
}
