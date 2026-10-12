//! The pages end to end, against a database holding the pipeline's tables and this app's.

use axum::{
    Router,
    body::Body,
    http::{Request, Response, StatusCode, header},
};
use forenoon_web::{Options, app, auth, db};
use http_body_util::BodyExt;
use sqlx::SqlitePool;
use tower::ServiceExt;

const PASSWORD: &str = "correct horse battery";

struct Site {
    router: Router,
    pool: SqlitePool,
    _folder: tempfile::TempDir,
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
        db::add_user(&pool, id, name, &hash, role, "America/New_York").await.unwrap();
    }
    let router = app(pool.clone(), Options { secure_cookies: false }).await.unwrap();
    Site { router, pool, _folder: folder }
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
        Reply { status, location, csp, text: String::from_utf8(body.to_vec()).unwrap() }
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
        self.post(site, "/login", &[("id", id), ("password", PASSWORD)]).await
    }
}

#[tokio::test]
async fn pages_need_a_session_and_sign_in_is_limited() {
    let site = site(true).await;
    let mut browser = Browser::default();

    // Anonymous: every page and every change leads to the sign-in page; only health is open.
    for path in ["/", "/account"] {
        let reply = browser.get(&site, path).await;
        assert_eq!(reply.status, StatusCode::TEMPORARY_REDIRECT, "{path}");
        assert!(reply.location.starts_with("/login"), "{}", reply.location);
    }
    for path in ["/rate", "/logout", "/account/password", "/account/profile"] {
        let reply = browser.post(&site, path, &[("csrf", "x"), ("article", "a1")]).await;
        assert!(reply.status.is_redirection() && reply.location.starts_with("/login"), "{path}");
    }
    assert_eq!(browser.get(&site, "/healthz").await.text, "ok");
    let page = browser.get(&site, "/login").await;
    assert!(page.status == StatusCode::OK && page.csp.contains("default-src 'none'") && !page.csp.contains("script"));
    assert!(browser.cookie.is_none(), "the sign-in page must not start a session");

    // Wrong password, unknown account, and a form posted from another site.
    let wrong = browser.post(&site, "/login", &[("id", "john"), ("password", "not the password")]).await;
    assert!(wrong.status == StatusCode::UNAUTHORIZED && wrong.text.contains("Wrong name or password."));
    let unknown = browser.post(&site, "/login", &[("id", "nobody"), ("password", PASSWORD)]).await;
    assert!(unknown.status == StatusCode::UNAUTHORIZED && unknown.text.contains("Wrong name or password."));
    let form = [("id", "john"), ("password", PASSWORD)];
    let foreign = browser.post_from(&site, "/login", &form, Some("https://evil.example")).await;
    assert_eq!(foreign.status, StatusCode::FORBIDDEN);
    assert!(browser.cookie.is_none());

    // The right password signs in; signing out ends the session.
    let reply = browser.post_from(&site, "/login", &form, Some("http://nas:8787")).await;
    assert!(reply.status == StatusCode::SEE_OTHER && reply.location == "/");
    let page = browser.get(&site, "/").await;
    assert!(page.status == StatusCode::OK && page.text.contains("Nothing has been delivered yet."));
    assert_eq!(browser.post(&site, "/logout", &[("csrf", "wrong")]).await.status, StatusCode::FORBIDDEN);
    assert_eq!(browser.post(&site, "/logout", &[("csrf", &page.csrf())]).await.location, "/login");
    assert!(browser.get(&site, "/").await.status.is_redirection());

    // Guessing is cut off, for the name even when the right password finally arrives.
    for _ in 0..10 {
        browser.post(&site, "/login", &[("id", "mary"), ("password", "a wrong guess")]).await;
    }
    let late = browser.post(&site, "/login", &[("id", "Mary"), ("password", PASSWORD)]).await;
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
    let mut other = Browser::default();
    other.sign_in(&site, "mary").await;
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
    let rating = |article: &'static str| db::get_rating(pool, "john", article);
    let stored = |rating, finished, reason: &str| Some(db::Rating { rating, finished, reason: reason.to_owned() });

    let base = [("csrf", csrf.as_str()), ("article", "a1"), ("back", "/?show=rated")];
    let with = |extra: &[(&'static str, &'static str)]| [&base[..], extra].concat();
    let done = browser.post(&site, "/rate", &with(&[("rating", "2")])).await;
    assert!(done.status == StatusCode::SEE_OTHER && done.location == "/?show=rated#a-a1", "{}", done.location);
    browser.post(&site, "/rate", &with(&[("finished", "1")])).await;
    browser.post(&site, "/rate", &with(&[("set", "reason"), ("reason", "thin")])).await;
    assert_eq!(rating("a1").await.unwrap(), stored(Some(2), true, "thin"));
    browser.post(&site, "/rate", &with(&[("set", "reason"), ("reason", "more-topic")])).await; // not for a low rating
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
    assert!(rated.contains("Too thin, nothing in it") && rated.contains(r#"<option value="thin" selected>"#));

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
    assert!(browser.post(&site, "/account/profile", &good).await.text.contains("Saved."));
    let user = auth::find_user(&site.pool, "john").await.unwrap().unwrap();
    assert_eq!((user.name.as_str(), user.timezone.as_str()), ("John R", "Europe/London"));
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
    assert!(browser.post(&site, "/account/password", &form).await.text.contains("Password changed."));

    assert_eq!(browser.get(&site, "/").await.status, StatusCode::OK); // this browser stays signed in
    assert!(second.get(&site, "/").await.status.is_redirection()); // the other does not
    let mut fresh = Browser::default();
    assert_eq!(fresh.sign_in(&site, "john").await.status, StatusCode::UNAUTHORIZED); // the old password is dead
    let reply = fresh.post(&site, "/login", &[("id", "john"), ("password", new)]).await;
    assert_eq!(reply.status, StatusCode::SEE_OTHER);
}
