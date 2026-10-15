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
    folder: tempfile::TempDir,
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
        data_dir: folder.path().to_owned(),
        logins_dir: Some(folder.path().join("logins")),
    };
    let router = app(pool.clone(), options).await.unwrap();
    Site { router, pool, mail, folder }
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
        Reply { status, location, csp, set_cookie: set, text: String::from_utf8_lossy(&body).into_owned() }
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
    assert!(rated.contains("Too thin, nothing in it") && rated.contains("What was wrong?"));
    assert!(rated.contains(
        r#"<button name="reason" value="thin" title="Too thin, nothing in it" class="on">Too thin</button>"#
    ));

    // What was wrong is one thing: another choice replaces it; pressing it again takes it back.
    browser.post(&site, "/rate", &with(&[("reason", "long")])).await;
    assert_eq!(rating("a1").await.unwrap(), stored(Some(2), true, "long"));
    browser.post(&site, "/rate", &with(&[("reason", "long")])).await;
    assert_eq!(rating("a1").await.unwrap(), stored(Some(2), true, ""));
    browser.post(&site, "/rate", &with(&[("reason", "thin")])).await;

    // Re-rating high drops the reason that no longer fits. What was liked can be several things.
    browser.post(&site, "/rate", &with(&[("rating", "5")])).await;
    assert_eq!(rating("a1").await.unwrap(), stored(Some(5), true, ""));
    browser.post(&site, "/rate", &with(&[("reason", "depth")])).await;
    browser.post(&site, "/rate", &with(&[("reason", "more-topic")])).await;
    assert_eq!(rating("a1").await.unwrap(), stored(Some(5), true, "more-topic,depth"));
    let page = browser.get(&site, "/?show=rated").await.text;
    assert!(page.contains("What did you like?") && page.matches(r#"class="on">The "#).count() == 2);
    browser.post(&site, "/rate", &with(&[("reason", "depth")])).await;
    assert_eq!(rating("a1").await.unwrap(), stored(Some(5), true, "more-topic"));
    browser.post(&site, "/rate", &with(&[("reason", "more-topic")])).await;

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

#[tokio::test]
async fn a_storys_articles_share_one_card_with_the_lead_first() {
    let site = site(true).await;
    let pool = &site.pool;
    let sent = "2026-10-10T10:00:00+00:00";
    // Delivered order is by title, which puts a follow-up before its lead.
    article(pool, "follow", "https://deno.example/blog", "deno.example via Blog", "An announcement", "john", sent)
        .await;
    article(pool, "lead", "https://blog.example/deno", "Blog", "Deno is joining Cloudflare", "john", sent).await;
    article(pool, "alone", "https://blog.example/other", "Blog", "Something else", "john", sent).await;
    sqlx::query("UPDATE deliveries SET story = 'lead' WHERE article_id IN ('lead', 'follow')")
        .execute(pool)
        .await
        .unwrap();

    // The follow-up has a thumbnail of its own.
    std::fs::create_dir_all(site.folder.path().join("thumbs")).unwrap();
    std::fs::write(site.folder.path().join("thumbs/follow.jpg"), b"jpeg").unwrap();

    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    let text = browser.get(&site, "/").await.text;
    assert!(text.contains("3 articles in 2 stories, sent 06:00"), "{text}");
    assert_eq!(text.matches("<article").count(), 2);
    let story = text.split("<article").find(|part| part.contains(r#"class="story""#)).expect("a story card");
    let (lead, more) = story.split_once(r#"<div class="more">"#).expect("the story's other articles");
    assert!(
        lead.contains(r#"id="a-lead""#)
            && lead.contains("Deno is joining Cloudflare")
            && !lead.contains("An announcement")
    );
    assert!(
        story.contains("One story · 2 articles, delivered together")
            && more.contains("Also in this story")
            && more.contains(r#"id="a-follow""#)
            && more.contains("An announcement")
    );
    assert!(!story.contains("Something else"));
    assert!(more.contains(r#"<img src="/thumb/follow""#), "a story's other articles keep their picture");

    // The story can be sent as one book: one request, apart from any for a single article.
    assert!(!text.contains("Send story to Kindle"), "no Kindle address yet");
    db::set_kindle_addresses(pool, "john", "john@kindle.com").await.unwrap();
    let text = browser.get(&site, "/").await.text;
    assert_eq!(text.matches("Send story to Kindle").count(), 1, "offered on the story, not on a lone article");
    let csrf = text.split(r#"name="csrf" value=""#).nth(1).unwrap().split('"').next().unwrap().to_owned();
    let whole = [("csrf", csrf.as_str()), ("article", "lead"), ("story", "1"), ("back", "/")];
    assert_eq!(browser.post(&site, "/send", &whole).await.location, "/#a-lead");
    browser.post(&site, "/send", &whole).await; // pressed twice: still one request
    browser.post(&site, "/send", &[("csrf", &csrf), ("article", "lead"), ("back", "/")]).await;
    let rows: Vec<(String, bool)> =
        sqlx::query_as("SELECT article_id, story FROM send_requests ORDER BY id").fetch_all(pool).await.unwrap();
    assert_eq!(rows, [("lead".to_owned(), true), ("lead".to_owned(), false)]);
    let text = browser.get(&site, "/").await.text;
    assert!(!text.contains("Send story to Kindle") && text.matches("On its way to your Kindle").count() == 2);
    sqlx::query("UPDATE send_requests SET status = 'failed', error = 'too large' WHERE story = 1")
        .execute(pool)
        .await
        .unwrap();
    let text = browser.get(&site, "/").await.text;
    let head = text.split(r#"<div class="story-head">"#).nth(1).unwrap().split("</div>").next().unwrap();
    assert!(head.contains("Could not be sent: too large") && head.contains("Send story to Kindle"));

    // Each article in the story is still rated on its own.
    let csrf = text.split(r#"name="csrf" value=""#).nth(1).unwrap().split('"').next().unwrap().to_owned();
    let done = browser.post(&site, "/rate", &[("csrf", &csrf), ("article", "follow"), ("rating", "4")]).await;
    assert_eq!(done.location, "/#a-follow");
    assert_eq!(db::get_rating(pool, "john", "follow").await.unwrap().unwrap().rating, Some(4));
    assert!(db::get_rating(pool, "john", "lead").await.unwrap().is_none());

    // With one of its articles rated, the story still shows whole under either filter.
    for show in ["rated", "unrated"] {
        let text = browser.get(&site, &format!("/?show={show}")).await.text;
        assert!(text.contains(r#"id="a-lead""#) && text.contains(r#"id="a-follow""#), "{show}");
        assert_eq!(text.contains("Something else"), show == "unrated", "{show}");
    }
}

#[tokio::test]
async fn sources_are_managed_by_admins_only() {
    let site = site(true).await;
    let mut admin = Browser::default();
    admin.sign_in(&site, "john").await;
    let page = admin.get(&site, "/sources").await;
    assert!(page.status == StatusCode::OK && page.text.contains("No feeds yet"));
    assert!(admin.get(&site, "/").await.text.contains(r#"<a href="/sources">Sources</a>"#));
    let csrf = page.csrf();

    // A reader cannot see or change them, even with a valid token of their own.
    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    assert!(!mary.get(&site, "/").await.text.contains("/sources"));
    assert_eq!(mary.get(&site, "/sources").await.status, StatusCode::FORBIDDEN);
    let token = mary.get(&site, "/account").await.csrf();
    let form = [("csrf", token.as_str()), ("name", "Evil"), ("address", "https://evil.example/feed")];
    assert_eq!(mary.post(&site, "/sources", &form).await.status, StatusCode::FORBIDDEN);

    let add = |name: &'static str, address: &'static str, extra: &[(&'static str, &'static str)]| {
        [
            &[("csrf", csrf.clone()), ("name", name.to_owned()), ("address", address.to_owned())][..],
            &extra.iter().map(|(key, value)| (*key, (*value).to_owned())).collect::<Vec<_>>()[..],
        ]
        .concat()
    };
    for (form, message) in [
        (add("", "https://blog.example/feed", &[]), "A source needs a name"),
        (add("Blog", "javascript:alert(1)", &[]), "A feed needs an http(s) address."),
        (add("Blog", "https://blog.example/feed", &[("daily_max", "51")]), "Places a day: give a whole number"),
        (add("Blog", "https://blog.example/feed", &[("daily_max", "-1")]), "Places a day: give a whole number"),
    ] {
        let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
        let reply = admin.post(&site, "/sources", &form).await;
        assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains(message), "{message}");
    }
    for form in [
        add("Blog <b>", "https://blog.example/feed", &[("extract", "page"), ("pinned", "1"), ("daily_max", "0")]),
        add("Other", "https://other.example/", &[]),
    ] {
        let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
        assert!(admin.post(&site, "/sources", &form).await.location.starts_with("/sources?saved=added&new="));
    }
    let again = add("Other", "https://third.example/feed", &[]);
    let again: Vec<(&str, &str)> = again.iter().map(|(key, value)| (*key, value.as_str())).collect();
    assert!(admin.post(&site, "/sources", &again).await.text.contains("already a source named Other"));
    assert_eq!(
        admin
            .post(&site, "/sources", &[("csrf", "wrong"), ("name", "X"), ("address", "https://x.example/f")])
            .await
            .status,
        StatusCode::FORBIDDEN
    );

    // A new feed waits for the pipeline to check its address, and the page reloads meanwhile.
    let text = admin.get(&site, "/sources").await.text;
    assert!(text.contains("Checking this address") && text.contains(r#"<meta http-equiv="refresh""#));
    let feeds = |all: Vec<forenoon_web::sources::Source>| -> Vec<forenoon_web::sources::Source> {
        all.into_iter().filter(|source| source.kind == "rss").collect()
    };
    let stored = feeds(forenoon_web::sources::list(&site.pool).await.unwrap());
    assert!(stored.iter().all(|source| source.check_state == "pending"));
    sqlx::query("UPDATE sources SET check_state = 'ok', check_note = 'Feed found at https://blog.example/rss.xml: 3 articles.' WHERE name != 'Other'")
        .execute(&site.pool).await.unwrap();
    sqlx::query("UPDATE sources SET check_state = 'failed', check_note = 'Not usable: no feed found at this address.' WHERE name = 'Other'")
        .execute(&site.pool)
        .await
        .unwrap();
    let text = admin.get(&site, "/sources").await.text;
    assert!(
        text.contains("Feed found at https://blog.example/rss.xml")
            && text.contains(r#"<p class="hint failing">Not usable: no feed found at this address.</p>"#)
    );
    assert!(!text.contains("http-equiv"));
    let rows: Vec<_> = stored
        .iter()
        .map(|s| (s.name.as_str(), s.url.as_str(), s.extract.as_str(), s.pinned, s.daily_max, s.enabled))
        .collect();
    assert_eq!(
        rows,
        [
            ("Blog <b>", "https://blog.example/feed", "page", true, 0, true), // pinned first
            ("Other", "https://other.example/", "auto", false, 2, true),      // 2 places when not given
        ]
    );

    // The pipeline's record of each source's last runs is shown beside it.
    sqlx::query("INSERT INTO source_health (source, failures, last_error, updated_at) VALUES ('Other', 2, 'HTTP 404', '2026-10-12T10:00:00+00:00')")
        .execute(&site.pool).await.unwrap();
    let text = admin.get(&site, "/sources").await.text;
    assert!(text.contains("4 sources, 0 paused, 1 failing.") && text.contains("Failed 2 runs in a row: HTTP 404"));
    assert!(text.contains("Blog &#60;b&#62;") && !text.contains("Blog <b>"));

    // Pause one, rename it, then remove the other.
    let (blog, other) = (stored[0].id, stored[1].id);
    let edit = [
        ("csrf", csrf.as_str()),
        ("name", "The Blog"),
        ("address", "https://blog.example/atom"),
        ("extract", "auto"),
        ("daily_max", "3"),
    ];
    assert_eq!(admin.post(&site, &format!("/sources/{blog}"), &edit).await.location, "/sources?saved=saved");
    assert_eq!(
        admin.post(&site, &format!("/sources/{other}/delete"), &[("csrf", &csrf)]).await.location,
        "/sources?saved=removed"
    );
    assert_eq!(
        mary.post(&site, &format!("/sources/{blog}/delete"), &[("csrf", &token)]).await.status,
        StatusCode::FORBIDDEN
    );
    let stored = feeds(forenoon_web::sources::list(&site.pool).await.unwrap());
    assert_eq!(stored.len(), 1);
    assert_eq!(
        (stored[0].name.as_str(), stored[0].url.as_str(), stored[0].pinned, stored[0].daily_max, stored[0].enabled),
        ("The Blog", "https://blog.example/atom", false, 3, false)
    );
    assert!(admin.get(&site, "/sources").await.text.contains("3 sources, 1 paused, 0 failing."));

    // A link-only feed: its pages are never fetched, and its picks are shown as links.
    let paper = add("Paper", "https://paper.example/feed", &[("extract", "link")]);
    let paper: Vec<(&str, &str)> = paper.iter().map(|(key, value)| (*key, value.as_str())).collect();
    assert!(admin.post(&site, "/sources", &paper).await.location.starts_with("/sources?saved=added&new="));
    let stored = feeds(forenoon_web::sources::list(&site.pool).await.unwrap());
    assert!(stored.iter().any(|s| s.name == "Paper" && s.extract == "link"));
}

#[tokio::test]
async fn a_sites_subscriber_session_is_kept_apart_and_never_shown() {
    let site = site(true).await;
    let logins = site.folder.path().join("logins");
    let mut admin = Browser::default();
    admin.sign_in(&site, "john").await;
    let csrf = admin.get(&site, "/sources").await.csrf();
    // A feed just added comes first, marked, until the page is loaded afresh.
    let add = [("csrf", csrf.as_str()), ("name", "Paper"), ("address", "https://www.paper.example/rss")];
    let added = admin.post(&site, "/sources", &add).await.location;
    let other = [("csrf", csrf.as_str()), ("name", "Aardvark"), ("address", "https://aardvark.example/rss")];
    admin.post(&site, "/sources", &other).await;
    let feed = forenoon_web::sources::list(&site.pool).await.unwrap().into_iter().find(|s| s.name == "Paper").unwrap();
    assert_eq!(added, format!("/sources?saved=added&new={0}#s-{0}", feed.id));
    let text = admin.get(&site, &format!("/sources?saved=added&new={}", feed.id)).await.text;
    let (paper, aardvark) = (text.find(r#"value="Paper""#).unwrap(), text.find(r#"value="Aardvark""#).unwrap());
    assert!(paper < aardvark && text.contains("Just added"));
    let text = admin.get(&site, "/sources").await.text;
    assert!(text.find(r#"value="Aardvark""#).unwrap() < text.find(r#"value="Paper""#).unwrap());
    assert!(!text.contains("Just added"));
    // The new-session form guesses nothing: no site is filled in for you.
    assert!(text.contains(r#"<input name="site" maxlength="253" placeholder="nytimes.com""#));
    assert!(!text.contains("fetched with your"));

    let path = "/sources/sessions";
    for (site_name, cookie, message) in [
        ("localhost", "id=1", "Give the site the session is for"),
        ("paper.example", "no pairs here", "Paste the Cookie header"),
        ("paper.example", "id=1\r\nX-Evil: 1", "Paste the Cookie header"),
    ] {
        let reply = admin.post(&site, path, &[("csrf", &csrf), ("site", site_name), ("cookie", cookie)]).await;
        assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains(message), "{message}");
    }
    let form = [("csrf", csrf.as_str()), ("site", "Paper.Example"), ("cookie", "Cookie: id=s3cret; theme=dark")];
    assert_eq!(admin.post(&site, path, &form).await.location, "/sources?saved=session");
    let file = logins.join("site-paper.example.json");
    assert_eq!(
        std::fs::read_to_string(&file).unwrap(),
        r#"{"cookie":"id=s3cret; theme=dark","domain":"paper.example"}"#
    );
    use std::os::unix::fs::PermissionsExt;
    assert_eq!(std::fs::metadata(&file).unwrap().permissions().mode() & 0o777, 0o600);
    // Saved for the site, and every feed on it says so; saving it again replaces the cookie.
    let text = admin.get(&site, "/sources").await.text;
    assert!(text.contains("<b class=\"wide\">paper.example</b>") && text.contains("No page fetched with it yet."));
    assert!(text.contains("Its pages are fetched with your paper.example session.") && !text.contains("s3cret"));
    admin.post(&site, path, &[("csrf", &csrf), ("site", "paper.example"), ("cookie", "id=newer")]).await;
    assert!(std::fs::read_to_string(&file).unwrap().contains("id=newer"));
    let count: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM sessions").fetch_one(&site.pool).await.unwrap();
    assert_eq!(count, 1);

    // How the last page fetched with it went, and the day the site says its sign-in ends.
    std::fs::create_dir_all(site.folder.path().join("sessions")).unwrap();
    let status = |body: &str| std::fs::write(site.folder.path().join("sessions/status.json"), body).unwrap();
    status(r#"{"site-paper.example": {"at": "2026-10-13T06:01:00-04:00", "ok": false, "note": "HTTP 403"}}"#);
    let text = admin.get(&site, "/sources").await.text;
    assert!(text.contains(r#"<p class="hint failing">Last page with it failed 2026-10-13 06:01: HTTP 403</p>"#));
    status(
        r#"{"site-paper.example": {"at": "2026-10-14T06:01:00-04:00", "ok": true, "note": "x", "expires": "2027-10-14"}}"#,
    );
    let text = admin.get(&site, "/sources").await.text;
    assert!(
        text.contains("Last page fetched with it: 2026-10-14 06:01. The site&#39;s sign-in lasts until 2027-10-14.")
    );

    // Not for a reader; removing a feed leaves the site's session; removing the session takes it.
    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    let token = mary.get(&site, "/account").await.csrf();
    let form = [("csrf", token.as_str()), ("site", "evil.example"), ("cookie", "id=mine")];
    assert_eq!(mary.post(&site, path, &form).await.status, StatusCode::FORBIDDEN);
    assert!(!logins.join("site-evil.example.json").exists());
    admin.post(&site, &format!("/sources/{}/delete", feed.id), &[("csrf", &csrf)]).await;
    assert!(file.exists());
    let remove = [("csrf", csrf.as_str()), ("site", "paper.example")];
    assert_eq!(admin.post(&site, "/sources/sessions/delete", &remove).await.location, "/sources?saved=nosession");
    assert!(!file.exists() && !admin.get(&site, "/sources").await.text.contains("<b class=\"wide\">paper.example</b>"));
}

#[tokio::test]
async fn built_in_sources_are_only_switched_on_or_off() {
    let site = site(true).await;
    let mut admin = Browser::default();
    admin.sign_in(&site, "john").await;
    let page = admin.get(&site, "/sources").await;
    let csrf = page.csrf();
    assert!(page.text.contains("<b class=\"wide\">Hacker News</b>") && page.text.contains("Lobsters"));
    let builtin = |all: Vec<forenoon_web::sources::Source>, key: &str| {
        all.into_iter().find(|source| source.builtin == key).expect("a built-in source")
    };
    let hn = builtin(forenoon_web::sources::list(&site.pool).await.unwrap(), "hackernews");
    assert!(hn.kind == "builtin" && hn.enabled && hn.daily_max == 0); // many publishers: no limit

    let path = format!("/sources/{}/toggle", hn.id);
    let reply = admin.post(&site, &path, &[("csrf", &csrf), ("enabled", "1"), ("daily_max", "x")]).await;
    assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains("Places a day: give a whole number"));
    let off = [("csrf", csrf.as_str()), ("enabled", "0"), ("daily_max", "4")];
    assert_eq!(admin.post(&site, &path, &off).await.location, "/sources?saved=saved");
    let hn = builtin(forenoon_web::sources::list(&site.pool).await.unwrap(), "hackernews");
    assert!(!hn.enabled && hn.daily_max == 4);
    assert!(
        admin
            .get(&site, "/sources")
            .await
            .text
            .contains(r#"name="daily_max" type="number" step="1" min="0" max="50" value="4""#)
    );
    assert!(admin.get(&site, "/sources").await.text.contains(r#"<button name="enabled" value="1">Turn on</button>"#));

    // It cannot be renamed, given an address or removed; and a feed cannot be toggled this way.
    let edit = [("csrf", csrf.as_str()), ("name", "Mine"), ("address", "https://evil.example/feed"), ("enabled", "1")];
    admin.post(&site, &format!("/sources/{}", hn.id), &edit).await;
    admin.post(&site, &format!("/sources/{}/delete", hn.id), &[("csrf", &csrf)]).await;
    let hn = builtin(forenoon_web::sources::list(&site.pool).await.unwrap(), "hackernews");
    assert_eq!((hn.name.as_str(), hn.url.as_str(), hn.enabled), ("Hacker News", "", false));

    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    let token = mary.get(&site, "/account").await.csrf();
    assert_eq!(mary.post(&site, &path, &[("csrf", &token), ("enabled", "1")]).await.status, StatusCode::FORBIDDEN);
    assert_eq!(admin.post(&site, &path, &[("csrf", "wrong"), ("enabled", "1")]).await.status, StatusCode::FORBIDDEN);
    assert!(!builtin(forenoon_web::sources::list(&site.pool).await.unwrap(), "hackernews").enabled);
}

#[tokio::test]
async fn settings_are_saved_by_admins_and_only_where_they_differ_from_the_default() {
    let site = site(true).await;
    let mut admin = Browser::default();
    admin.sign_in(&site, "john").await;
    let page = admin.get(&site, "/settings").await;
    let csrf = page.csrf();
    assert!(page.text.contains(r#"<input name="delivery_time" type="time" value="06:00" required>"#));
    assert!(page.text.contains(r#"name="llm.enabled" value="1" checked"#) && !page.text.contains("changed"));
    assert!(admin.get(&site, "/").await.text.contains(r#"<a href="/settings">Settings</a>"#));
    // What the model used, priced by the pipeline when it logged each call; a call to a model
    // with no known price is said apart. Nothing about prices is a setting.
    assert!(
        page.text.contains("Today: 0 of 600,000 tokens, about $0.0000.") && !page.text.contains("Price per million")
    );
    let now = forenoon_web::db::now_iso();
    for (model, cost) in [("claude-haiku-5-5", Some(0.0125)), ("claude-new-9", None)] {
        sqlx::query(
            "INSERT INTO llm_calls (created_at, purpose, model, input_tokens, output_tokens, status,
             cache_write_tokens, cache_read_tokens, cost) VALUES (?, 'rank', ?, 1000, 200, 'ok', 300, 50000, ?)",
        )
        .bind(&now)
        .bind(model)
        .bind(cost)
        .execute(&site.pool)
        .await
        .unwrap();
    }
    let text = admin.get(&site, "/settings").await.text;
    assert!(text.contains("Today: 103,000 of 600,000 tokens, about $0.0125.") && text.contains("This month: 103,000"));
    assert!(text.contains("1 calls this month were to a model with no known price"));

    // Every field as the page shows it, with a few changed.
    let defaults = [
        ("format", "issue"),
        ("max_age_days", "3"),
        ("max_article_words", "45000"),
        ("images.grayscale", "1"),
        ("llm.enabled", "1"),
        ("llm.model", "claude-haiku-5-5"),
        ("llm.daily_token_cap", "600000"),
        ("llm.read_candidates", "40"),
        ("llm.explore_slots", "1"),
        ("llm.choose_issue", "1"),
        ("llm.group_stories", "1"),
        ("llm.follow_links", "1"),
        ("llm.show_reasons", "1"),
        ("llm.check_extraction", "1"),
        ("llm.learn", "1"),
        ("alerts.enabled", "1"),
        ("alerts.source_failures", "3"),
        ("keep_pages_days", "-1"),
        ("keep_issues_days", "-1"),
    ];
    let form = |changes: &[(&'static str, &'static str)]| -> Vec<(&'static str, String)> {
        let mut form: Vec<(&'static str, String)> = vec![("csrf", csrf.clone())];
        form.extend(defaults.iter().map(|(key, value)| (*key, (*value).to_owned())));
        form.extend(changes.iter().map(|(key, value)| (*key, (*value).to_owned())));
        form
    };
    let send = |form: Vec<(&'static str, String)>| form;
    let changed =
        send(form(&[("delivery_time", "07:30"), ("max_story_entries", "4"), ("alerts.to", "Ops@Example.com")]));
    let changed: Vec<(&str, &str)> = changed.iter().map(|(key, value)| (*key, value.as_str())).collect();
    assert_eq!(admin.post(&site, "/settings", &changed).await.location, "/settings?saved=1");
    let stored: Vec<(String, String)> =
        sqlx::query_as("SELECT key, value FROM settings ORDER BY key").fetch_all(&site.pool).await.unwrap();
    let stored: Vec<(&str, &str)> = stored.iter().map(|(key, value)| (key.as_str(), value.as_str())).collect();
    // The unticked box (alerts.on_empty) is stored as off; everything left at its default is not stored.
    assert_eq!(
        stored,
        [
            ("alerts.on_empty", "0"),
            ("alerts.to", "ops@example.com"),
            ("delivery_time", "07:30"),
            ("max_story_entries", "4")
        ]
    );
    let text = admin.get(&site, "/settings?saved=1").await.text;
    assert!(
        text.contains("Saved.") && text.contains(r#"value="07:30""#) && text.matches("changed</span>").count() == 4
    );

    // Back to the defaults: the rows go away.
    let back = form(&[("delivery_time", "06:00"), ("max_story_entries", "3"), ("alerts.on_empty", "1")]);
    let back: Vec<(&str, &str)> = back.iter().map(|(key, value)| (*key, value.as_str())).collect();
    admin.post(&site, "/settings", &back).await;
    let left: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM settings").fetch_one(&site.pool).await.unwrap();
    assert_eq!(left, 0);

    // One wrong value saves nothing and keeps what was typed.
    let wrong = form(&[("delivery_time", "25:00"), ("max_story_entries", "9")]);
    let wrong: Vec<(&str, &str)> = wrong.iter().map(|(key, value)| (*key, value.as_str())).collect();
    let reply = admin.post(&site, "/settings", &wrong).await;
    assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains("Deliver at: give a time such as 06:00."));
    assert!(reply.text.contains(r#"name="max_story_entries" type="number" step="1" min="1" max="10" value="9""#));
    let left: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM settings").fetch_one(&site.pool).await.unwrap();
    assert_eq!(left, 0);

    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    assert_eq!(mary.get(&site, "/settings").await.status, StatusCode::FORBIDDEN);
    let token = mary.get(&site, "/account").await.csrf();
    let form = [("csrf", token.as_str()), ("delivery_time", "01:00")];
    assert_eq!(mary.post(&site, "/settings", &form).await.status, StatusCode::FORBIDDEN);
    assert_eq!(admin.post(&site, "/settings", &[("csrf", "wrong")]).await.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn send_to_kindle_only_queues_the_readers_own_delivered_articles() {
    let site = site(true).await;
    let pool = &site.pool;
    let sent = "2026-10-10T10:00:00+00:00";
    article(pool, "a1", "https://blog.example/1", "Blog", "Post 1", "john", sent).await;
    article(pool, "m1", "https://blog.example/m", "Blog", "Mary's", "mary", sent).await;
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;

    // Without a Kindle address there is no button and a request is refused.
    let page = browser.get(&site, "/").await;
    let csrf = page.csrf();
    assert!(!page.text.contains("Send to Kindle"));
    let ask =
        |article: &'static str| [("csrf", csrf.clone()), ("article", article.to_owned()), ("back", "/".to_owned())];
    let form = ask("a1");
    let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
    assert_eq!(browser.post(&site, "/send", &form).await.status, StatusCode::BAD_REQUEST);

    db::set_kindle_addresses(pool, "john", "john@kindle.com").await.unwrap();
    assert!(browser.get(&site, "/").await.text.contains("<button class=\"kindle\">Send to Kindle</button>"));
    assert_eq!(browser.post(&site, "/send", &form).await.location, "/#a-a1");
    browser.post(&site, "/send", &form).await; // pressed twice: still one request
    let page = browser.get(&site, "/").await.text;
    assert!(
        page.contains("On its way to your Kindle")
            && !page.contains("<button class=\"kindle\">Send to Kindle</button>")
    );

    // Not someone else's article, not an unknown one, not without the page's token.
    for other in ["m1", "nope"] {
        let form = ask(other);
        let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
        assert_eq!(browser.post(&site, "/send", &form).await.status, StatusCode::NOT_FOUND);
    }
    assert_eq!(
        browser.post(&site, "/send", &[("csrf", "wrong"), ("article", "a1")]).await.status,
        StatusCode::FORBIDDEN
    );
    let rows: Vec<(String, String, String)> =
        sqlx::query_as("SELECT user_id, article_id, status FROM send_requests").fetch_all(pool).await.unwrap();
    assert_eq!(rows, [("john".to_owned(), "a1".to_owned(), "queued".to_owned())]);

    // The pipeline records the outcome; the card shows it and offers the button again.
    sqlx::query("UPDATE send_requests SET status = 'failed', error = 'smtp said no'").execute(pool).await.unwrap();
    let page = browser.get(&site, "/").await.text;
    assert!(
        page.contains("Could not be sent: smtp said no")
            && page.contains("<button class=\"kindle\">Send to Kindle</button>")
    );
    sqlx::query("UPDATE send_requests SET status = 'sent', requested_at = '2026-10-11T12:00:00+00:00'")
        .execute(pool)
        .await
        .unwrap();
    assert!(browser.get(&site, "/").await.text.contains("Sent to your Kindle on 11 October."));

    // A day's worth and no more.
    sqlx::query("UPDATE send_requests SET requested_at = ?").bind(db::now_iso()).execute(pool).await.unwrap();
    for _ in 1..db::SENDS_PER_DAY {
        sqlx::query(
            "INSERT INTO send_requests (user_id, article_id, requested_at, status) VALUES ('john', 'a1', ?, 'sent')",
        )
        .bind(db::now_iso())
        .execute(pool)
        .await
        .unwrap();
    }
    assert_eq!(browser.post(&site, "/send", &form).await.status, StatusCode::TOO_MANY_REQUESTS);
}

#[tokio::test]
async fn a_pick_shown_only_as_a_link_is_grayed_and_never_sent() {
    let site = site(true).await;
    let pool = &site.pool;
    let sent = "2026-10-10T10:00:00+00:00";
    article(pool, "a1", "https://blog.example/1", "Blog", "Post 1", "john", sent).await;
    article(pool, "p1", "https://paper.example/1", "Paper", "Behind the paywall", "john", sent).await;
    sqlx::query("UPDATE deliveries SET link_only = 1 WHERE article_id = 'p1'").execute(pool).await.unwrap();
    db::set_kindle_addresses(pool, "john", "john@kindle.com").await.unwrap();
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;

    let page = browser.get(&site, "/").await;
    let text = &page.text;
    // In the day's issue, grayed and marked, with a link to the site and no Send button; the
    // counts are of what was sent.
    assert!(text.contains("1 article in 1 issue.") && text.contains("1 article (and 1 shown as links only)"));
    let card = text.split("class=\"link-only\"").nth(1).unwrap().split("</article>").next().unwrap();
    assert!(card.contains("Behind the paywall") && card.contains("Link only"));
    assert!(card.contains("href=\"https://paper.example/1\"") && !card.contains("Send to Kindle"));
    // It can still be rated, but not sent by hand.
    let csrf = page.csrf();
    let form = [("csrf", csrf.as_str()), ("article", "p1"), ("back", "/")];
    assert_eq!(browser.post(&site, "/send", &form).await.status, StatusCode::CONFLICT);
    let queued: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM send_requests").fetch_one(pool).await.unwrap();
    assert_eq!(queued, 0);
}

#[tokio::test]
async fn a_thumbnail_is_served_only_to_a_reader_the_article_was_delivered_to() {
    let site = site(true).await;
    let pool = &site.pool;
    let sent = "2026-10-10T10:00:00+00:00";
    article(pool, "a1", "https://blog.example/1", "Blog", "With a thumbnail", "john", sent).await;
    article(pool, "a2", "https://blog.example/2", "Blog", "Without one", "john", sent).await;
    sqlx::query("UPDATE articles SET image_url = 'https://blog.example/lead.jpg'").execute(pool).await.unwrap();
    let thumbs = site.folder.path().join("thumbs");
    std::fs::create_dir_all(&thumbs).unwrap();
    std::fs::write(thumbs.join("a1.jpg"), b"\xff\xd8\xff jpeg bytes").unwrap();

    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    let page = browser.get(&site, "/").await;
    // Our own picture where there is one; the article's lead image otherwise.
    assert!(
        page.text.contains(r#"<img src="/thumb/a1""#)
            && page.text.contains(r#"<img src="https://blog.example/lead.jpg""#)
    );
    assert!(page.csp.contains("img-src 'self'"));
    let picture = browser.send(&site, Request::get("/thumb/a1").body(Body::empty()).unwrap()).await;
    assert!(picture.status == StatusCode::OK && picture.text.contains("jpeg bytes"));
    for missing in ["/thumb/a2", "/thumb/nope", "/thumb/..%2Fforenoon.sqlite3", "/thumb/a1.jpg"] {
        assert_eq!(browser.get(&site, missing).await.status, StatusCode::NOT_FOUND, "{missing}");
    }
    // Not to another reader, and not without signing in.
    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    assert_eq!(mary.get(&site, "/thumb/a1").await.status, StatusCode::NOT_FOUND);
    assert!(Browser::default().get(&site, "/thumb/a1").await.status.is_redirection());
}

#[tokio::test]
async fn a_broken_article_is_reported_to_the_admin_apart_from_its_rating() {
    let site = site(true).await;
    let pool = &site.pool;
    article(pool, "m1", "https://blog.example/m", "Blog", "Mary's article", "mary", "2026-10-10T10:00:00+00:00").await;
    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    let page = mary.get(&site, "/").await;
    let csrf = page.csrf();
    assert!(page.text.contains("Report a problem") && !page.text.contains("/problems"));
    mary.post(&site, "/rate", &[("csrf", &csrf), ("article", "m1"), ("rating", "2")]).await;
    assert!(!mary.get(&site, "/").await.text.contains("Broken"), "broken is no longer a reason for a rating");
    mary.post(&site, "/rate", &[("csrf", &csrf), ("article", "m1"), ("reason", "broken")]).await;

    let form = [("csrf", csrf.as_str()), ("article", "m1"), ("back", "/")];
    assert_eq!(mary.post(&site, "/report", &form).await.location, "/#a-m1");
    mary.post(&site, "/report", &form).await; // pressed twice: one report
    assert!(mary.get(&site, "/").await.text.contains("Reported as broken or incomplete."));
    assert_eq!(
        mary.post(&site, "/report", &[("csrf", &csrf), ("article", "nope")]).await.status,
        StatusCode::NOT_FOUND
    );
    assert_eq!(
        mary.post(&site, "/report", &[("csrf", "wrong"), ("article", "m1")]).await.status,
        StatusCode::FORBIDDEN
    );
    // The rating is untouched, and the extraction log has the report.
    assert_eq!(
        db::get_rating(pool, "mary", "m1").await.unwrap().unwrap(),
        db::Rating { rating: Some(2), finished: false, reason: String::new() }
    );
    let log: Vec<(String, String)> =
        sqlx::query_as("SELECT verdict, problem FROM extraction_reviews").fetch_all(pool).await.unwrap();
    assert_eq!(log, [("bad".to_owned(), "reader: broken or incomplete".to_owned())]);
    assert_eq!(mary.get(&site, "/problems").await.status, StatusCode::FORBIDDEN);

    let mut admin = Browser::default();
    admin.sign_in(&site, "john").await;
    assert!(admin.get(&site, "/").await.text.contains(r#"<a href="/problems">Problems (1)</a>"#));
    let text = admin.get(&site, "/problems").await.text;
    assert!(
        text.contains("Mary&#39;s article")
            && text.contains("by mary, on the web pages")
            && text.contains("Extractor:")
    );
}

#[tokio::test]
async fn the_archive_lists_the_readers_library_and_filters_it() {
    let site = site(true).await;
    let pool = &site.pool;
    let sent = "2026-10-10T10:00:00+00:00";
    article(pool, "a1", "https://blog.example/1", "Blog", "Compilers <b>today</b>", "john", sent).await;
    article(pool, "m1", "https://blog.example/m", "Blog", "Mary's own", "mary", sent).await;
    // Weighed for John and passed over: one the model read, one judged from its headline.
    let seen = "INSERT INTO articles (id, url, source, title, first_seen, word_count) VALUES (?, ?, ?, ?, ?, 2300)";
    let judged = "INSERT INTO assessments (user_id, article_id, assessed_at, score, reason, read, lasting)
                  VALUES (?, ?, '2026-10-11T09:00:00+00:00', ?, ?, ?, ?)";
    for (id, title, user, score, read, lasting) in [
        ("n1", "A near miss on linear types", "john", 6.5, 1, "years"),
        ("h1", "Headline only", "john", 2.0, 0, ""),
        ("m2", "Passed over for Mary", "mary", 6.0, 1, "days"),
    ] {
        let url = format!("https://found.example/{id}");
        sqlx::query(seen).bind(id).bind(url).bind("Hacker News").bind(title).bind(sent).execute(pool).await.unwrap();
        sqlx::query(judged)
            .bind(user)
            .bind(id)
            .bind(score)
            .bind("Deep, but long")
            .bind(read)
            .bind(lasting)
            .execute(pool)
            .await
            .unwrap();
    }
    let route = "INSERT INTO article_routes (article_id, route, via, first_seen) VALUES (?, ?, ?, 'x')";
    for (id, how, via) in
        [("a1", "feed", "Blog"), ("n1", "found", "Hacker News"), ("n1", "feed", "Blog"), ("h1", "found", "Lobsters")]
    {
        sqlx::query(route).bind(id).bind(how).bind(via).execute(pool).await.unwrap();
    }

    let mut browser = Browser::default();
    assert_eq!(browser.get(&site, "/archive").await.location, "/login");
    browser.sign_in(&site, "john").await;
    assert!(browser.get(&site, "/").await.text.contains(r#"<a href="/archive">Archive</a>"#));

    // What was delivered and what the model read, newest first; never another reader's.
    let page = browser.get(&site, "/archive").await;
    let text = &page.text;
    assert!(text.contains("Compilers &#60;b&#62;today&#60;/b&#62;") && !text.contains("<b>today</b>"));
    assert!(text.find("A near miss").unwrap() < text.find("Compilers").unwrap());
    assert!(!text.contains("Headline only") && !text.contains("Mary"));
    assert!(text.contains("Read by the model 11 October 2026, not delivered") && text.contains("Score 6.5"));
    assert!(text.contains("The model: Deep, but long") && text.contains("Worth reading for years"));
    // Every way it reached us is shown.
    assert!(text.contains("Found on Hacker News") && text.contains("From the feed Blog"));
    assert!(text.contains("Delivered 10 October 2026") && text.contains("2,300 words, 10 min"));

    let shows = |text: &str| {
        ["Compilers", "A near miss", "Headline only"].map(|title| text.contains(&format!(">{title}"))).to_vec()
    };
    let view = |query: &'static str| format!("/archive?{query}");
    for (query, expected) in [
        ("show=all", [true, true, true]),
        ("show=delivered", [true, false, false]),
        ("show=near", [false, true, false]),
        ("q=LINEAR", [false, true, false]),
        ("show=all&via=Lobsters", [false, false, true]),
        ("via=Blog", [true, true, false]),
        ("route=found", [false, true, false]),
        ("lasting=years", [false, true, false]),
        ("rating=rated", [false, false, false]),
        ("from=2026-10-11", [false, true, false]),
        ("to=2026-10-10", [true, false, false]),
        ("show=nonsense&via=Nobody&from=yesterday&sort=sideways&page=x", [true, true, false]),
    ] {
        assert_eq!(shows(&browser.get(&site, &view(query)).await.text), expected, "{query}");
    }
    let by_score = browser.get(&site, "/archive?show=all&sort=score").await.text;
    assert!(by_score.find("A near miss").unwrap() < by_score.find("Headline only").unwrap());
    assert!(by_score.contains("The model, from the headline: Deep, but long"));
    assert!(by_score.contains("Weighed from its headline"));

    // An article passed over can be rated and sent, and the reader lands back on this view.
    let csrf = page.csrf();
    let back = "/archive?show=near";
    let rate = [("csrf", csrf.as_str()), ("article", "n1"), ("rating", "5"), ("back", back)];
    assert_eq!(browser.post(&site, "/rate", &rate).await.location, "/archive?show=near#a-n1");
    assert!(browser.get(&site, "/archive?rating=high").await.text.contains("Rated 5 of 5"));
    db::set_kindle_addresses(pool, "john", "john@kindle.com").await.unwrap();
    assert!(browser.get(&site, back).await.text.contains("<button class=\"kindle\">Send to Kindle</button>"));
    let send = [("csrf", csrf.as_str()), ("article", "n1"), ("story", "1"), ("back", back)];
    assert_eq!(browser.post(&site, "/send", &send).await.location, "/archive?show=near#a-n1");
    assert!(browser.get(&site, back).await.text.contains("On its way to your Kindle"));
    let queued: Vec<(String, bool)> =
        sqlx::query_as("SELECT article_id, story FROM send_requests").fetch_all(pool).await.unwrap();
    assert_eq!(queued, [("n1".to_owned(), false)]); // never a story: it was not delivered as one
    // Not what was weighed for someone else.
    for other in ["m1", "m2"] {
        let rate = [("csrf", csrf.as_str()), ("article", other), ("rating", "5")];
        assert_eq!(browser.post(&site, "/rate", &rate).await.status, StatusCode::NOT_FOUND);
        let send = [("csrf", csrf.as_str()), ("article", other)];
        assert_eq!(browser.post(&site, "/send", &send).await.status, StatusCode::NOT_FOUND);
    }

    // More than a page: the next is one link away and keeps the filters.
    for n in 0..55 {
        let id = format!("x{n}");
        sqlx::query(seen)
            .bind(&id)
            .bind("https://x.example/")
            .bind("Blog")
            .bind("Filler")
            .bind(sent)
            .execute(pool)
            .await
            .unwrap();
        sqlx::query(judged).bind("john").bind(&id).bind(1.0).bind("").bind(1).bind("days").execute(pool).await.unwrap();
    }
    let first = browser.get(&site, "/archive?show=near").await.text;
    assert_eq!(first.matches(r#"<div class="shelf""#).count(), 50);
    assert!(first.contains(r#"<a href="/archive?show=near&#38;page=2">Older</a>"#) && !first.contains(">Newer<"));
    let second = browser.get(&site, "/archive?show=near&page=2").await.text;
    assert_eq!(second.matches(r#"<div class="shelf""#).count(), 6);
    assert!(second.contains(r#"<a href="/archive?show=near">Newer</a>"#) && !second.contains(">Older<"));
}

#[tokio::test]
async fn the_archive_waits_for_a_pipeline_that_keeps_what_it_weighs() {
    let site = site(false).await;
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    let page = browser.get(&site, "/archive").await;
    assert_eq!(page.status, StatusCode::OK);
    assert!(page.text.contains("has not started keeping what it weighs"));
}

#[tokio::test]
async fn a_link_added_by_hand_is_only_queued_and_then_shown_as_the_readers_own() {
    let site = site(true).await;
    let pool = &site.pool;
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    let page = browser.get(&site, "/archive").await;
    let csrf = page.csrf();
    assert!(page.text.contains("Add a link") && !page.text.contains("http-equiv"));
    let add = |link: &'static str, then: &'static str, rating: &'static str| {
        [("csrf", csrf.clone()), ("link", link.to_owned()), ("then", then.to_owned()), ("rating", rating.to_owned())]
    };
    let post = |form: &[(&'static str, String); 4]| -> Vec<(&'static str, String)> { form.to_vec() };
    macro_rules! send {
        ($form:expr) => {{
            let form = post(&$form);
            let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
            browser.post(&site, "/archive", &form).await
        }};
    }

    // Kept as read needs no Kindle; anything to be sent does. Only web addresses are taken.
    assert_eq!(send!(add("https://blog.example/read", "keep", "4")).location, "/archive?show=added");
    assert_eq!(send!(add("https://blog.example/x", "next", "")).status, StatusCode::BAD_REQUEST);
    db::set_kindle_addresses(pool, "john", "john@kindle.com").await.unwrap();
    assert_eq!(send!(add("https://blog.example/next", "next", "5")).location, "/archive?show=added");
    for bad in ["javascript:alert(1)", "file:///etc/passwd", "blog.example/no-scheme"] {
        assert_eq!(send!(add(bad, "next", "")).location, "/archive?show=added&error=link");
    }
    assert_eq!(send!(add("https://blog.example/x", "sideways", "")).status, StatusCode::BAD_REQUEST);
    let wrong = [("csrf", "wrong"), ("link", "https://blog.example/x"), ("then", "next")];
    assert_eq!(browser.post(&site, "/archive", &wrong).await.status, StatusCode::FORBIDDEN);
    let rows: Vec<(String, String, String, Option<i64>, String)> =
        sqlx::query_as("SELECT user_id, url, action, rating, status FROM add_requests ORDER BY id")
            .fetch_all(pool)
            .await
            .unwrap();
    let row = |url: &str, action: &str, rating| {
        ("john".to_owned(), url.to_owned(), action.to_owned(), rating, "queued".to_owned())
    };
    // A rating is taken only with something already read.
    assert_eq!(
        rows,
        [row("https://blog.example/read", "keep", Some(4)), row("https://blog.example/next", "next", None)]
    );

    // While it is fetched the page says so and reloads itself; a failure is shown.
    let waiting = browser.get(&site, "/archive?error=link").await.text;
    assert!(waiting.contains("https://blog.example/next: Being fetched") && waiting.contains("http-equiv"));
    assert!(waiting.contains("That is not a web address"));
    sqlx::query("UPDATE add_requests SET status = 'failed', error = 'HTTP 404 <b>' WHERE action = 'next'")
        .execute(pool)
        .await
        .unwrap();
    // What the pipeline does with the other: the article exists, tied to the request.
    sqlx::query(
        "INSERT INTO articles (id, url, source, title, first_seen, word_count)
         VALUES ('k1', 'https://blog.example/read', 'blog.example', 'Read elsewhere', 'x', 900)",
    )
    .execute(pool)
    .await
    .unwrap();
    sqlx::query("INSERT INTO article_routes (article_id, route, via, first_seen) VALUES ('k1', 'manual', '', 'x')")
        .execute(pool)
        .await
        .unwrap();
    sqlx::query(
        "UPDATE add_requests SET status = 'done', article_id = 'k1', requested_at = '2026-10-12T09:00:00+00:00'
         WHERE action = 'keep'",
    )
    .execute(pool)
    .await
    .unwrap();
    let page = browser.get(&site, "/archive").await;
    let text = page.text.clone();
    assert!(text.contains("Could not be added: HTTP 404 &#60;b&#62;") && !text.contains("http-equiv"));
    assert!(text.contains(">Read elsewhere<") && text.contains("Added by you 12 October 2026"));
    // A failure goes once the same link is added again, or when cleared; it stays on record.
    let failed: i64 =
        sqlx::query_scalar("SELECT id FROM add_requests WHERE status = 'failed'").fetch_one(pool).await.unwrap();
    sqlx::query("INSERT INTO add_requests (user_id, url, action, requested_at) VALUES ('john', 'https://blog.example/next', 'next', ?)")
        .bind(forenoon_web::db::now_iso())
        .execute(pool)
        .await
        .unwrap();
    let text = browser.get(&site, "/archive").await.text;
    assert!(!text.contains("Could not be added") && text.contains("https://blog.example/next: Being fetched"));
    sqlx::query("DELETE FROM add_requests WHERE id > ?").bind(failed).execute(pool).await.unwrap();
    let text = browser.get(&site, "/archive").await.text;
    assert!(text.contains("Clear the links that could not be added"));
    let clear = [("csrf", page.csrf()), ("link", String::new()), ("then", "next".to_owned())];
    let clear: Vec<(&str, &str)> = clear.iter().map(|(k, v)| (*k, v.as_str())).collect();
    assert_eq!(browser.post(&site, "/archive/added/clear", &clear).await.location, "/archive?show=added");
    let text = browser.get(&site, "/archive").await.text;
    assert!(!text.contains("Could not be added") && !text.contains("Clear the links"));
    let kept: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM add_requests WHERE status = 'failed'").fetch_one(pool).await.unwrap();
    assert_eq!(kept, 1);
    for (query, shown) in
        [("show=added", true), ("route=manual", true), ("show=near", false), ("show=delivered", false)]
    {
        let text = browser.get(&site, &format!("/archive?{query}")).await.text;
        assert_eq!(text.contains(">Read elsewhere<"), shown, "{query}");
    }
    // It is theirs to rate and send, and nobody else's.
    let rate = [("csrf", csrf.as_str()), ("article", "k1"), ("rating", "5"), ("back", "/archive")];
    assert_eq!(browser.post(&site, "/rate", &rate).await.location, "/archive#a-k1");
    let mut other = Browser::default();
    other.sign_in(&site, "mary").await;
    let page = other.get(&site, "/archive?show=all").await;
    assert!(!page.text.contains("Read elsewhere") && !page.text.contains("blog.example/next"));
    let csrf = page.csrf();
    let rate = [("csrf", csrf.as_str()), ("article", "k1"), ("rating", "1")];
    assert_eq!(other.post(&site, "/rate", &rate).await.status, StatusCode::NOT_FOUND);

    // A day's worth and no more.
    for _ in 0..db::ADDS_PER_DAY {
        db::queue_add(pool, "mary", "https://blog.example/y", "keep", None).await.unwrap();
    }
    let form = [("csrf", csrf.as_str()), ("link", "https://blog.example/z"), ("then", "keep")];
    assert_eq!(other.post(&site, "/archive", &form).await.location, "/archive?show=added&error=many");
}

#[tokio::test]
async fn the_notes_kept_on_a_reader_are_theirs_to_see_edit_and_roll_back() {
    let site = site(true).await;
    let pool = &site.pool;
    let mut browser = Browser::default();
    assert_eq!(browser.get(&site, "/learned").await.location, "/login");
    browser.sign_in(&site, "john").await;
    assert!(browser.get(&site, "/account").await.text.contains(r#"<a href="/learned">"#));
    let page = browser.get(&site, "/learned").await;
    let csrf = page.csrf();
    assert!(page.text.contains("Nothing has been learned yet") && !page.text.contains("Earlier versions"));

    // What the pipeline writes: the notes, when, and a version in the history.
    sqlx::query("UPDATE users SET learned = 'Finishes long <b>technical</b> pieces.', learned_at = '2026-10-12T11:00:00+00:00' WHERE id = 'john'")
        .execute(pool)
        .await
        .unwrap();
    let version = "INSERT INTO profile_history (user_id, notes, author, created_at) VALUES (?, ?, 'agent', ?)";
    for (user, notes) in [("john", "Finishes long <b>technical</b> pieces."), ("mary", "Mary's notes")] {
        sqlx::query(version).bind(user).bind(notes).bind("2026-10-12T11:00:00+00:00").execute(pool).await.unwrap();
    }
    let text = browser.get(&site, "/learned").await.text;
    assert!(text.contains("Last rewritten from your ratings on 12 October 2026"));
    assert!(text.contains("Finishes long &#60;b&#62;technical&#60;/b&#62; pieces.") && !text.contains("<b>technical"));
    assert!(text.contains("Rewritten from your ratings · in use") && !text.contains("Mary"));
    assert!(!text.contains("Restore this version"));

    // The reader's edit replaces it and is a version too; the pipeline's date is left alone.
    let edit = [("csrf", csrf.as_str()), ("notes", "  Skips market reports.\r\nLikes long pieces.  ")];
    assert_eq!(browser.post(&site, "/learned", &edit).await.location, "/learned?saved=notes");
    let now = db::learned(pool, "john").await.unwrap();
    assert_eq!(now.notes, "Skips market reports.\nLikes long pieces.");
    assert_eq!(now.learned_at.as_deref(), Some("2026-10-12T11:00:00+00:00"));
    let text = browser.get(&site, "/learned?saved=notes").await.text;
    assert!(text.contains("Edited by you · in use") && text.contains("Restore this version"));
    let long = "x".repeat(2001);
    let too_long = [("csrf", csrf.as_str()), ("notes", long.as_str())];
    assert_eq!(browser.post(&site, "/learned", &too_long).await.location, "/learned?saved=long");

    // Back to an earlier version of their own; never to someone else's, never without the token.
    let ids: Vec<(i64, String)> =
        sqlx::query_as("SELECT id, user_id FROM profile_history ORDER BY id").fetch_all(pool).await.unwrap();
    let (mine, marys) = (ids[0].0.to_string(), ids[1].0.to_string());
    let theirs = [("csrf", csrf.as_str()), ("version", marys.as_str())];
    assert_eq!(browser.post(&site, "/learned/restore", &theirs).await.status, StatusCode::NOT_FOUND);
    let forged = [("csrf", "wrong"), ("version", mine.as_str())];
    assert_eq!(browser.post(&site, "/learned/restore", &forged).await.status, StatusCode::FORBIDDEN);
    let back = [("csrf", csrf.as_str()), ("version", mine.as_str())];
    assert_eq!(browser.post(&site, "/learned/restore", &back).await.location, "/learned?saved=restored");
    assert_eq!(db::learned(pool, "john").await.unwrap().notes, "Finishes long <b>technical</b> pieces.");
    assert_eq!(db::learned(pool, "mary").await.unwrap().notes, "");
}
