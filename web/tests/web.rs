//! The pages end to end, against a database holding the pipeline's tables and this app's.

use axum::{
    Router,
    body::Body,
    http::{Request, Response, StatusCode, header},
};
use std::sync::{Arc, Mutex};

use forenoon_web::{
    Options, app, auth, db,
    mail::{Mailer, Message},
};
use http_body_util::BodyExt;
use sqlx::SqlitePool;
use tower::ServiceExt;

const PASSWORD: &str = "correct horse battery";

struct Site {
    router: Router,
    pool: SqlitePool,
    /// Every email the app sent.
    mail: Arc<Mutex<Vec<Message>>>,
    _folder: tempfile::TempDir,
}

impl Site {
    /// The link in the most recent email.
    fn emailed_link(&self) -> String {
        let mail = self.mail.lock().unwrap();
        let body = &mail.last().expect("an email").body;
        let (_, rest) = body.split_once("http://nas:8787").expect("a link to the site");
        rest.split_whitespace().next().unwrap().to_owned()
    }

    fn emails(&self) -> usize {
        self.mail.lock().unwrap().len()
    }
}

/// A browser: remembers the session cookie between requests.
#[derive(Default)]
struct Browser {
    cookie: Option<String>,
}

struct Reply {
    status: StatusCode,
    location: String,
    csp: String,
    set_cookie: String,
    text: String,
}

impl Reply {
    /// The token the page's forms carry.
    fn csrf(&self) -> String {
        let (_, rest) = self.text.split_once(r#"name="csrf" value=""#).expect("a form with a token");
        rest.split('"').next().unwrap().to_owned()
    }
}

async fn site(with_pipeline: bool) -> Site {
    let folder = tempfile::tempdir().unwrap();
    let pool = db::open(&folder.path().join("forenoon.sqlite3")).await.unwrap();
    if with_pipeline {
        sqlx::raw_sql(include_str!("../pipeline-schema.sql")).execute(&pool).await.unwrap();
    }
    for (id, name, role) in [("john", "John", "admin"), ("mary", "Mary", "reader")] {
        let hash = auth::hash_password(PASSWORD.to_owned()).await.unwrap();
        db::add_user(&pool, id, &format!("{id}@example.com"), name, &hash, role, "America/New_York").await.unwrap();
    }
    let mail = Arc::new(Mutex::new(Vec::new()));
    let options = Options {
        secure_cookies: false,
        mailer: Mailer::Memory(mail.clone()),
        base_url: "http://nas:8787/".to_owned(),
    };
    let router = app(pool.clone(), options).await.unwrap();
    Site { router, pool, mail, _folder: folder }
}

async fn article(pool: &SqlitePool, id: &str, url: &str, source: &str, title: &str, user: &str, sent: &str) {
    sqlx::query("INSERT INTO articles (id, url, source, title, author, first_seen) VALUES (?, ?, ?, ?, 'Ann', ?)")
        .bind(id)
        .bind(url)
        .bind(source)
        .bind(title)
        .bind(sent)
        .execute(pool)
        .await
        .unwrap();
    sqlx::query("INSERT INTO deliveries (user_id, article_id, delivered_at) VALUES (?, ?, ?)")
        .bind(user)
        .bind(id)
        .bind(sent)
        .execute(pool)
        .await
        .unwrap();
}

impl Browser {
    async fn send(&mut self, site: &Site, mut request: Request<Body>) -> Reply {
        if let Some(cookie) = &self.cookie {
            request.headers_mut().insert(header::COOKIE, cookie.parse().unwrap());
        }
        let response: Response<_> = site.router.clone().oneshot(request).await.unwrap();
        let text = |name: header::HeaderName| {
            response.headers().get(name).map(|value| value.to_str().unwrap().to_owned()).unwrap_or_default()
        };
        let set = text(header::SET_COOKIE);
        if set.contains("Max-Age=0") {
            self.cookie = None; // the server ended the session
        } else if !set.is_empty() {
            assert!(set.contains("HttpOnly") && set.contains("SameSite=Lax"), "{set}");
            self.cookie = Some(set.split(';').next().unwrap().to_owned());
        }
        let (status, location, csp) =
            (response.status(), text(header::LOCATION), text(header::CONTENT_SECURITY_POLICY));
        let body = response.into_body().collect().await.unwrap().to_bytes();
        Reply { status, location, csp, set_cookie: set, text: String::from_utf8(body.to_vec()).unwrap() }
    }

    async fn get(&mut self, site: &Site, path: &str) -> Reply {
        self.send(site, Request::get(path).body(Body::empty()).unwrap()).await
    }

    async fn post(&mut self, site: &Site, path: &str, form: &[(&str, &str)]) -> Reply {
        self.post_from(site, path, form, None).await
    }

    async fn post_from(&mut self, site: &Site, path: &str, form: &[(&str, &str)], origin: Option<&str>) -> Reply {
        let encode =
            |text: &str| percent_encoding::utf8_percent_encode(text, percent_encoding::NON_ALPHANUMERIC).to_string();
        let body = form.iter().map(|(key, value)| format!("{key}={}", encode(value))).collect::<Vec<_>>().join("&");
        let mut request = Request::post(path)
            .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
            .header(header::HOST, "nas:8787");
        if let Some(origin) = origin {
            request = request.header(header::ORIGIN, origin);
        }
        self.send(site, request.body(Body::from(body)).unwrap()).await
    }

    async fn sign_in(&mut self, site: &Site, id: &str) -> Reply {
        self.post(site, "/login", &[("email", &format!("{id}@example.com")), ("password", PASSWORD)]).await
    }
}

#[tokio::test]
async fn pages_need_a_session_and_sign_in_is_limited() {
    let site = site(true).await;
    let mut browser = Browser::default();

    // Anonymous: every page and every change leads to the sign-in page; only health is open.
    for path in ["/", "/account"] {
        let reply = browser.get(&site, path).await;
        assert!(reply.status == StatusCode::SEE_OTHER && reply.location == "/login", "{path}");
    }
    for path in ["/rate", "/logout", "/account/password", "/account/profile"] {
        let reply = browser.post(&site, path, &[("csrf", "x"), ("article", "a1")]).await;
        // A 303, not a 307: the browser must not re-post the form to the sign-in page.
        assert!(reply.status == StatusCode::SEE_OTHER && reply.location == "/login", "{path}");
    }
    assert_eq!(browser.get(&site, "/healthz").await.text, "ok");
    let page = browser.get(&site, "/login").await;
    assert!(page.status == StatusCode::OK && page.csp.contains("default-src 'none'") && !page.csp.contains("script"));
    assert!(browser.cookie.is_none(), "the sign-in page must not start a session");

    // Wrong password, unknown account, and a form posted from another site.
    let wrong = browser.post(&site, "/login", &[("email", "john@example.com"), ("password", "not the password")]).await;
    assert!(wrong.status == StatusCode::UNAUTHORIZED && wrong.text.contains("Wrong email or password."));
    let unknown = browser.post(&site, "/login", &[("email", "nobody@example.com"), ("password", PASSWORD)]).await;
    assert!(unknown.status == StatusCode::UNAUTHORIZED && unknown.text.contains("Wrong email or password."));
    let form = [("email", "john@example.com"), ("password", PASSWORD)];
    let foreign = browser.post_from(&site, "/login", &form, Some("https://evil.example")).await;
    assert_eq!(foreign.status, StatusCode::FORBIDDEN);
    assert!(browser.cookie.is_none());

    // The right password signs in; signing out ends the session.
    let reply = browser.post_from(&site, "/login", &form, Some("http://nas:8787")).await;
    assert!(reply.status == StatusCode::SEE_OTHER && reply.location == "/");
    let page = browser.get(&site, "/").await;
    assert!(page.status == StatusCode::OK && page.text.contains("Nothing has been delivered yet."));
    // The day's first visit saves the session, which is what moves its expiry; later ones do not.
    assert!(page.set_cookie.contains("Max-Age=2592000"), "{}", page.set_cookie);
    assert!(browser.get(&site, "/").await.set_cookie.is_empty());
    assert_eq!(browser.post(&site, "/logout", &[("csrf", "wrong")]).await.status, StatusCode::FORBIDDEN);
    assert_eq!(browser.post(&site, "/logout", &[("csrf", &page.csrf())]).await.location, "/login");
    assert!(browser.get(&site, "/").await.status.is_redirection());

    // Guessing is cut off, for the name even when the right password finally arrives.
    for _ in 0..10 {
        browser.post(&site, "/login", &[("email", "mary@example.com"), ("password", "a wrong guess")]).await;
    }
    let late = browser.post(&site, "/login", &[("email", " Mary@Example.com "), ("password", PASSWORD)]).await;
    assert!(late.status == StatusCode::TOO_MANY_REQUESTS && late.text.contains("Too many attempts"));
}

#[tokio::test]
async fn newsstand_shows_only_the_readers_own_deliveries_as_untrusted_text() {
    let site = site(true).await;
    let pool = &site.pool;
    article(
        pool,
        "a1",
        "https://blog.example/post",
        "Blog <b>",
        "A <script>title</script>",
        "john",
        "2026-10-10T10:00:00+00:00",
    )
    .await;
    article(pool, "a2", "javascript:alert(1)", "Other", "Second", "john", "2026-10-10T10:00:00+00:00").await;
    article(pool, "m1", "https://blog.example/mary", "Blog", "Mary's article", "mary", "2026-10-10T10:00:00+00:00")
        .await;
    sqlx::query("UPDATE articles SET word_count = 2300, image_url = 'https://blog.example/lead.jpg', description = ? WHERE id = 'a1'")
        .bind("A description. ".repeat(40))
        .execute(pool)
        .await
        .unwrap();
    sqlx::query("UPDATE articles SET image_url = 'data:image/png;base64,AAAA' WHERE id = 'a2'")
        .execute(pool)
        .await
        .unwrap();
    sqlx::query(
        "INSERT INTO selections (user_id, article_id, selected_at, policy, score, reason, rank, explore)
         VALUES ('john', 'a1', '2026-10-10T10:00:00+00:00', 'claude-rank-v1', 8.5, 'Deep \"compiler\" work', 1, 0)",
    )
    .execute(pool)
    .await
    .unwrap();
    sqlx::query(
        "INSERT INTO ratings (user_id, article_id, rating, finished, updated_at) VALUES ('john', 'a1', 4, 1, 'x')",
    )
    .execute(pool)
    .await
    .unwrap();
    for day in 11..30 {
        let (id, url, sent) =
            (format!("b{day}"), format!("https://blog.example/{day}"), format!("2026-10-{day}T10:00:00+00:00"));
        article(pool, &id, &url, "Blog", &format!("Post {day}"), "john", &sent).await;
    }

    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    let page = browser.get(&site, "/").await;
    assert!(page.text.contains("21 articles in 20 issues. 1 rated."), "{}", page.text);
    assert!(
        page.text.contains("Post 29") && page.text.contains("Older issues") && !page.text.contains("A &#60;script")
    );
    assert!(!page.text.contains("Mary's article") && !page.text.contains("Mary&#39;s article"));

    let (_, rest) = page.text.rsplit_once(r#"<a href=""#).unwrap();
    let older = browser.get(&site, rest.split('"').next().unwrap()).await;
    let text = &older.text;
    assert!(text.contains("Saturday 10 October 2026") && text.contains("2 articles, sent 06:00"), "{text}"); // local time
    assert!(text.contains("A &#60;script&#62;title&#60;/script&#62;") && !text.contains("<script"));
    assert!(
        text.contains(r#"href="https://blog.example/post""#) && text.contains(r#"src="https://blog.example/lead.jpg""#)
    );
    assert!(!text.contains("javascript:") && !text.contains("data:image"));
    assert!(
        text.contains("Picked because: Deep &#34;compiler&#34; work") && text.contains("Score 8.5, no. 1 of the day")
    );
    assert!(text.contains("Rated 4 of 5") && text.contains("Finished") && text.contains("2,300 words, 10 min"));
    assert!(text.contains("Newest") && text.contains("Blog &#60;b&#62; · Ann"));
    assert_eq!(browser.get(&site, "/?before=nonsense&show=nonsense").await.status, StatusCode::OK);

    // Another reader sees only their own, whatever the address says.
    // Signing in over a session that is already signed in starts a new one.
    let mut other = Browser::default();
    other.sign_in(&site, "john").await;
    let johns = other.cookie.clone();
    other.sign_in(&site, "mary").await;
    assert!(other.cookie.is_some() && other.cookie != johns);
    let mut stale = Browser { cookie: johns };
    assert!(stale.get(&site, "/").await.status.is_redirection(), "the old session id must be dead");
    let page = other.get(&site, "/?user=john").await;
    assert!(page.text.contains("1 article in 1 issue. 0 rated.") && page.text.contains("Mary&#39;s article"));
    assert!(!page.text.contains("Post 29"));
}

#[tokio::test]
async fn ratings_change_only_for_the_signed_in_reader_from_their_own_page() {
    let site = site(true).await;
    let pool = &site.pool;
    for n in [1, 2] {
        article(
            pool,
            &format!("a{n}"),
            &format!("https://blog.example/{n}"),
            "Blog",
            &format!("Post {n}"),
            "john",
            "2026-10-10T10:00:00+00:00",
        )
        .await;
    }
    article(pool, "m1", "https://blog.example/m", "Blog", "Mary's", "mary", "2026-10-10T10:00:00+00:00").await;
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    let page = browser.get(&site, "/").await;
    let csrf = page.csrf();
    assert!(page.csp.contains("form-action 'self'"));
    let none_yet = browser.get(&site, "/?show=rated").await.text;
    assert!(none_yet.contains("No articles match this filter.") && !none_yet.contains("Older issues"));
    let rating = |article: &'static str| db::get_rating(pool, "john", article);
    let stored = |rating, finished, reason: &str| Some(db::Rating { rating, finished, reason: reason.to_owned() });

    let base = [("csrf", csrf.as_str()), ("article", "a1"), ("back", "/?show=rated")];
    let with = |extra: &[(&'static str, &'static str)]| [&base[..], extra].concat();
    let done = browser.post(&site, "/rate", &with(&[("rating", "2")])).await;
    assert!(done.status == StatusCode::SEE_OTHER && done.location == "/?show=rated#a-a1", "{}", done.location);
    browser.post(&site, "/rate", &with(&[("finished", "1")])).await;
    browser.post(&site, "/rate", &with(&[("reason", "thin")])).await;
    assert_eq!(rating("a1").await.unwrap(), stored(Some(2), true, "thin"));
    browser.post(&site, "/rate", &with(&[("reason", "more-topic")])).await; // not for a low rating
    assert_eq!(rating("a1").await.unwrap(), stored(Some(2), true, "thin"));
    let log: Vec<(String, String)> =
        sqlx::query_as("SELECT signal, source FROM feedback_events ORDER BY id").fetch_all(pool).await.unwrap();
    let signals: Vec<&str> = log.iter().map(|(signal, _)| signal.as_str()).collect();
    assert_eq!(signals, ["r2", "f2", "why-thin"]);
    assert!(log.iter().all(|(_, source)| source == "web"));

    // The filter shows one or the other; the rated card shows its reason and a selected option.
    let (rated, unrated) =
        (browser.get(&site, "/?show=rated").await.text, browser.get(&site, "/?show=unrated").await.text);
    assert!(
        rated.contains("Post 1")
            && !rated.contains("Post 2")
            && unrated.contains("Post 2")
            && !unrated.contains("Post 1")
    );
    assert!(
        rated.contains("Too thin, nothing in it")
            && rated.contains(
                r#"<button name="reason" value="thin" title="Too thin, nothing in it" class="on">Thin</button>"#
            )
    );

    // Re-rating high drops the reason that no longer fits.
    browser.post(&site, "/rate", &with(&[("rating", "5")])).await;
    assert_eq!(rating("a1").await.unwrap(), stored(Some(5), true, ""));

    // Refused: a wrong token, another site's form, an article never delivered to this reader
    // (including one delivered to someone else), and a redirect off the site.
    let wrong = [("csrf", "wrong"), ("article", "a1"), ("rating", "1")];
    assert_eq!(browser.post(&site, "/rate", &wrong).await.status, StatusCode::FORBIDDEN);
    let form = with(&[("rating", "1")]);
    assert_eq!(
        browser.post_from(&site, "/rate", &form, Some("https://evil.example")).await.status,
        StatusCode::FORBIDDEN
    );
    for article in ["nope", "m1"] {
        let form = [("csrf", csrf.as_str()), ("article", article), ("rating", "1")];
        assert_eq!(browser.post(&site, "/rate", &form).await.status, StatusCode::NOT_FOUND);
    }
    assert!(db::get_rating(pool, "john", "m1").await.unwrap().is_none());
    assert!(db::get_rating(pool, "mary", "m1").await.unwrap().is_none());
    let away = [("csrf", csrf.as_str()), ("article", "a1"), ("back", "//evil.example/x"), ("rating", "3")];
    assert_eq!(browser.post(&site, "/rate", &away).await.location, "/#a-a1");
    assert_eq!(rating("a1").await.unwrap(), stored(Some(3), true, ""));

    // Finished alone, then un-finishing it, leaves nothing; so does Clear.
    let second = [("csrf", csrf.as_str()), ("article", "a2"), ("finished", "1")];
    browser.post(&site, "/rate", &second).await;
    assert_eq!(rating("a2").await.unwrap(), stored(None, true, ""));
    let second = [("csrf", csrf.as_str()), ("article", "a2"), ("finished", "0")];
    browser.post(&site, "/rate", &second).await;
    assert!(rating("a2").await.unwrap().is_none());
    browser.post(&site, "/rate", &with(&[("clear", "1")])).await;
    assert!(rating("a1").await.unwrap().is_none());
}

#[tokio::test]
async fn account_page_changes_the_profile_and_the_password() {
    let site = site(false).await; // also: the pages work before the pipeline has ever run
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    assert!(browser.get(&site, "/").await.text.contains("Nothing has been delivered yet."));
    let page = browser.get(&site, "/account").await;
    let csrf = page.csrf();
    assert!(page.text.contains("America/New_York") && page.text.contains("admin") && !page.text.contains("argon2"));

    let bad = [("csrf", csrf.as_str()), ("name", "John R"), ("timezone", "Mars/Olympus")];
    assert_eq!(browser.post(&site, "/account/profile", &bad).await.status, StatusCode::BAD_REQUEST);
    let good = [("csrf", csrf.as_str()), ("name", "John R"), ("timezone", "Europe/London")];
    // A change answers with a redirect, so reloading the result does not post the form again.
    let saved = browser.post(&site, "/account/profile", &good).await;
    assert!(saved.status == StatusCode::SEE_OTHER && saved.location == "/account?saved=profile");
    assert!(browser.get(&site, &saved.location).await.text.contains("Saved."));
    let user = auth::find_user(&site.pool, "john").await.unwrap().unwrap();
    assert_eq!((user.name.as_str(), user.timezone.as_str()), ("John R", "Europe/London"));
    // Reading settings: the pipeline reads these. Only an admin's Kindle addresses change.
    let reading = |interests: &'static str, daily: &'static str, kindle: &'static str| {
        [
            ("csrf", csrf.clone()),
            ("interests", interests.to_owned()),
            ("daily_stories", daily.to_owned()),
            ("kindle_addresses", kindle.to_owned()),
        ]
    };
    for (form, message) in [
        (reading("Compilers.", "0", "a@kindle.com"), "Stories a day must be a number from 1 to 50."),
        (reading("Compilers.", "16", "not an address"), "not an address is not an email address."),
    ] {
        let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
        let reply = browser.post(&site, "/account/reading", &form).await;
        assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains(message), "{message}");
    }
    let form = reading("Compilers.\r\nNot watches. <b>", "16", " a@kindle.com \n\nb@kindle.com");
    let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
    let saved = browser.post(&site, "/account/reading", &form).await;
    assert_eq!(saved.location, "/account?saved=reading");
    let page = browser.get(&site, &saved.location).await.text;
    assert!(page.contains("Not watches. &#60;b&#62;</textarea>") && page.contains(r#"value="16""#), "{page}");
    let user = auth::find_user(&site.pool, "john").await.unwrap().unwrap();
    assert_eq!((user.interests.as_str(), user.daily_stories), ("Compilers.\nNot watches. <b>", 16));
    assert_eq!(user.kindle_addresses, "a@kindle.com\nb@kindle.com");

    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    let page = mary.get(&site, "/account").await;
    assert!(page.text.contains("An admin sets this.") && !page.text.contains(r#"name="kindle_addresses""#));
    let token = page.csrf();
    let form = [
        ("csrf", token.as_str()),
        ("interests", "Sailing."),
        ("daily_stories", "5"),
        ("kindle_addresses", "evil@example.com"),
    ];
    assert_eq!(mary.post(&site, "/account/reading", &form).await.status, StatusCode::SEE_OTHER);
    let user = auth::find_user(&site.pool, "mary").await.unwrap().unwrap();
    assert_eq!((user.interests.as_str(), user.daily_stories, user.kindle_addresses.as_str()), ("Sailing.", 5, ""));

    let no_token = [("csrf", "wrong"), ("name", "X"), ("timezone", "UTC")];
    assert_eq!(browser.post(&site, "/account/profile", &no_token).await.status, StatusCode::FORBIDDEN);

    // A second browser signed in as the same reader is signed out by the change.
    let mut second = Browser::default();
    second.sign_in(&site, "john").await;
    assert_eq!(second.get(&site, "/").await.status, StatusCode::OK);

    let new = "a much better password";
    let change = |current: &'static str, password: &'static str, again: &'static str| {
        [
            ("csrf", csrf.clone()),
            ("current", current.to_owned()),
            ("password", password.to_owned()),
            ("again", again.to_owned()),
        ]
    };
    for (form, message) in [
        (change("not it at all", new, new), "The current password is wrong."),
        (change(PASSWORD, new, "something else"), "The two new passwords are not the same."),
        (change(PASSWORD, "short", "short"), "at least 10 characters"),
    ] {
        let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
        let reply = browser.post(&site, "/account/password", &form).await;
        assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains(message), "{message}");
    }
    let form = change(PASSWORD, new, new);
    let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
    let changed = browser.post(&site, "/account/password", &form).await;
    assert_eq!(changed.location, "/account?saved=password");
    assert!(browser.get(&site, &changed.location).await.text.contains("Password changed."));

    assert_eq!(browser.get(&site, "/").await.status, StatusCode::OK); // this browser stays signed in
    assert!(second.get(&site, "/").await.status.is_redirection()); // the other does not
    let mut fresh = Browser::default();
    assert_eq!(fresh.sign_in(&site, "john").await.status, StatusCode::UNAUTHORIZED); // the old password is dead
    let reply = fresh.post(&site, "/login", &[("email", "john@example.com"), ("password", new)]).await;
    assert_eq!(reply.status, StatusCode::SEE_OTHER);
}

#[tokio::test]
async fn email_is_confirmed_softly_and_restores_a_forgotten_password() {
    let site = site(false).await;
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;

    // Unconfirmed, the account works fully; a banner offers to send the link again.
    let page = browser.get(&site, "/").await;
    assert!(page.status == StatusCode::OK && page.text.contains("Confirm <b>john@example.com</b>"));
    let csrf = page.csrf();

    // A reset is not sent to an unconfirmed address, and the answer does not say so.
    let asked = browser.post(&site, "/forgot", &[("email", "john@example.com")]).await;
    assert!(asked.status == StatusCode::OK && asked.text.contains("If that address belongs to an account"));
    assert_eq!(site.emails(), 0);

    let sent = browser.post(&site, "/account/verify", &[("csrf", &csrf)]).await;
    assert_eq!(sent.location, "/account?saved=sent");
    assert_eq!(site.mail.lock().unwrap()[0].to, "john@example.com");
    let link = site.emailed_link();
    assert!(link.starts_with("/verify/"), "{link}");
    // Only the token's hash is stored.
    let stored: Vec<(String,)> =
        sqlx::query_as("SELECT token_hash FROM email_tokens").fetch_all(&site.pool).await.unwrap();
    assert!(stored.len() == 1 && !link.contains(&stored[0].0));

    // The link works without a session, once.
    let mut elsewhere = Browser::default();
    assert_eq!(elsewhere.get(&site, "/verify/not-a-token").await.status, StatusCode::BAD_REQUEST);
    assert!(elsewhere.get(&site, &link).await.text.contains("john@example.com is confirmed"));
    assert_eq!(elsewhere.get(&site, &link).await.status, StatusCode::BAD_REQUEST);
    assert!(!browser.get(&site, "/").await.text.contains("Confirm <b>"));

    // Forgotten password: the same answer for a stranger's address, a link only for ours.
    let unknown = elsewhere.post(&site, "/forgot", &[("email", "nobody@example.com")]).await;
    assert!(unknown.text.contains("If that address belongs to an account") && site.emails() == 1);
    let foreign =
        elsewhere.post_from(&site, "/forgot", &[("email", "john@example.com")], Some("https://evil.example")).await;
    assert!(foreign.status == StatusCode::FORBIDDEN && site.emails() == 1);
    elsewhere.post(&site, "/forgot", &[("email", " John@Example.com ")]).await;
    assert_eq!(site.emails(), 2);
    let reset = site.emailed_link();
    assert!(reset.starts_with("/reset/") && elsewhere.get(&site, &reset).await.text.contains("New password"));
    assert_eq!(elsewhere.get(&site, "/reset/not-a-token").await.status, StatusCode::BAD_REQUEST);

    let new = "restored by email link";
    let short = elsewhere.post(&site, &reset, &[("password", "short"), ("again", "short")]).await;
    assert!(short.status == StatusCode::BAD_REQUEST && short.text.contains("at least 10 characters"));
    let done = elsewhere.post(&site, &reset, &[("password", new), ("again", new)]).await;
    assert_eq!(done.location, "/login?reset=1");
    assert!(elsewhere.get(&site, &done.location).await.text.contains("Password changed."));
    // The link is used up, the old password is dead, and the signed-in browser is signed out.
    assert_eq!(
        elsewhere.post(&site, &reset, &[("password", new), ("again", new)]).await.status,
        StatusCode::BAD_REQUEST
    );
    assert!(browser.get(&site, "/").await.status.is_redirection());
    assert_eq!(elsewhere.sign_in(&site, "john").await.status, StatusCode::UNAUTHORIZED);
    let back = elsewhere.post(&site, "/login", &[("email", "john@example.com"), ("password", new)]).await;
    assert_eq!(back.status, StatusCode::SEE_OTHER);

    // Changing the address needs the password, and waits for the new address to be confirmed.
    let csrf = elsewhere.get(&site, "/account").await.csrf();
    let change = |email: &'static str, current: &'static str| {
        [("csrf", csrf.clone()), ("email", email.to_owned()), ("current", current.to_owned())]
    };
    for (form, message) in [
        (change("new@example.com", "not the password"), "The current password is wrong."),
        (change("mary@example.com", new), "Another account already uses that address."),
        (change("not an address", new), "That is not an email address."),
    ] {
        let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
        let reply = elsewhere.post(&site, "/account/email", &form).await;
        assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains(message), "{message}");
    }
    let form = change("New@Example.com", new);
    let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
    assert_eq!(elsewhere.post(&site, "/account/email", &form).await.location, "/account?saved=sent");
    assert_eq!(site.mail.lock().unwrap().last().unwrap().to, "new@example.com");
    let user = auth::find_user(&site.pool, "john").await.unwrap().unwrap();
    assert_eq!(
        (user.email.as_deref(), user.pending_email.as_deref()),
        (Some("john@example.com"), Some("new@example.com"))
    );
    assert!(elsewhere.get(&site, "/account").await.text.contains("A change to <b>new@example.com</b> is waiting"));

    elsewhere.get(&site, &site.emailed_link()).await;
    let user = auth::find_user(&site.pool, "john").await.unwrap().unwrap();
    assert_eq!((user.email.as_deref(), user.pending_email.as_deref()), (Some("new@example.com"), None));
    assert!(user.email_verified_at.is_some());
    // Sign-in follows the address.
    let mut fresh = Browser::default();
    let old = fresh.post(&site, "/login", &[("email", "john@example.com"), ("password", new)]).await;
    assert_eq!(old.status, StatusCode::UNAUTHORIZED);
    let now = fresh.post(&site, "/login", &[("email", "new@example.com"), ("password", new)]).await;
    assert_eq!(now.status, StatusCode::SEE_OTHER);
}
