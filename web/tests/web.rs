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
const FEEDBACK_SECRET: &[u8] = b"a secret of at least thirty-two bytes";

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
    site_with(with_pipeline, false).await
}

async fn site_with(with_pipeline: bool, secure_cookies: bool) -> Site {
    site_full(with_pipeline, secure_cookies, "", None).await
}

/// `demo`: the demo hosts, as `FORENOON_WEB_DEMO` is written; `landing`: the hostname the
/// landing page is served at, as `FORENOON_WEB_LANDING` names it.
async fn site_full(with_pipeline: bool, secure_cookies: bool, demo: &str, landing: Option<&str>) -> Site {
    let folder = tempfile::tempdir().unwrap();
    let pool = db::open(&folder.path().join("forenoon.sqlite3")).await.unwrap();
    if with_pipeline {
        sqlx::raw_sql(include_str!("../pipeline-schema.sql")).execute(&pool).await.unwrap();
    }
    for (id, name, role) in [("john", "John", "admin"), ("mary", "Mary", "reader")] {
        let hash = auth::hash_password(PASSWORD.to_owned()).await.unwrap();
        db::add_user(&pool, id, &format!("{id}@example.com"), name, &hash, role, "America/New_York").await.unwrap();
        db::follow_shared(&pool, id).await.unwrap();
    }
    let mail = Arc::new(Mutex::new(Vec::new()));
    let options = Options {
        secure_cookies,
        mailer: Mailer::Memory(mail.clone()),
        // Secure cookies come with an https address; plain ones with the LAN address.
        base_url: if secure_cookies { "https://forenoon.example.com/" } else { "http://nas:8787/" }.to_owned(),
        data_dir: folder.path().to_owned(),
        logins_dir: Some(folder.path().join("logins")),
        // Behind a tunnel that connects from loopback, as deploy.sh sets it up.
        proxy: if secure_cookies {
            forenoon_web::proxy::Proxy::new("CF-Connecting-IP", "127.0.0.1").unwrap()
        } else {
            forenoon_web::proxy::Proxy::default()
        },
        feedback_secret: Some(FEEDBACK_SECRET.to_vec()),
        sender: Some("issues@example.com".to_owned()),
        demo: forenoon_web::demo::parse(demo).unwrap(),
        landing: landing.map(str::to_owned),
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

/// Post the Accounts page's invite form.
async fn invite_post(site: &Site, browser: &mut Browser, form: &[(&str, String)]) -> Reply {
    let form: Vec<(&str, &str)> = form.iter().map(|(key, value)| (*key, value.as_str())).collect();
    browser.post(site, "/accounts", &form).await
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

/// The page's inline script, named the way a Content-Security-Policy names it: its SHA-256 in base64.
fn script_hash(page: &str) -> String {
    use sha2::{Digest, Sha256};
    let (_, rest) = page.split_once("<script>").expect("the theme script");
    let script = rest.split_once("</script>").unwrap().0;
    let digest = Sha256::digest(script.as_bytes());
    const TABLE: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::new();
    for chunk in digest.chunks(3) {
        let n = chunk.iter().fold(0u32, |n, b| (n << 8) | u32::from(*b)) << (8 * (3 - chunk.len()));
        for i in 0..4 {
            out.push(if i <= chunk.len() { TABLE[((n >> (18 - 6 * i)) & 63) as usize] as char } else { '=' });
        }
    }
    format!("sha256-{out}")
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
    assert!(page.status == StatusCode::OK && page.csp.contains("default-src 'none'"));
    // The only script is the light/dark switch, allowed by its hash alone.
    assert_eq!(page.text.matches("<script").count(), 1);
    assert!(page.csp.contains(&format!("script-src '{}';", script_hash(&page.text))), "{}", page.csp);
    assert!(page.text.contains(r#"<button class="theme" type="button" hidden"#));
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
    let account = browser.get(&site, "/account").await;
    assert!(account.text.contains(r#"action="/logout""#) && !page.text.contains(r#"action="/logout""#));
    assert_eq!(browser.post(&site, "/logout", &[("csrf", &account.csrf())]).await.location, "/login");
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
    // One script on the page: the theme switch, never the title's.
    assert!(text.contains("A &#60;script&#62;title&#60;/script&#62;") && text.matches("<script").count() == 1);
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
    // Reading settings, on the Reading page: the pipeline reads these.
    let reading = |interests: &'static str, daily: &'static str| {
        [
            ("csrf", csrf.as_str()),
            ("interests", interests),
            ("daily_stories", daily),
            ("format", "articles"),
            ("most_per_subject", "4"),
            ("show_reasons", "1"),
        ]
    };
    let reply = browser.post(&site, "/account/reading", &reading("Compilers.", "0")).await;
    assert!(
        reply.status == StatusCode::BAD_REQUEST && reply.text.contains("Stories a day must be a number from 1 to 50.")
    );
    assert!(reply.text.contains("Compilers.</textarea>") && reply.text.contains(r#"value="0""#), "shown as typed");
    // The rest of the form too, not the saved values: the reader loses nothing to one slip.
    assert!(reply.text.contains(r#"<option value="articles" selected>"#), "{}", reply.text);
    assert!(reply.text.contains(r#"name="most_per_subject" type="number" min="0" max="20" value="4""#));
    assert!(
        reply.text.contains(r#"name="show_reasons" value="1" checked"#)
            && reply.text.contains(r#"name="explore" value="1">"#)
    );
    let saved = browser.post(&site, "/account/reading", &reading("Compilers.\r\nNot watches. <b>", "16")).await;
    assert_eq!(saved.location, "/reading?saved=reading");
    let page = browser.get(&site, &saved.location).await.text;
    assert!(page.contains("Not watches. &#60;b&#62;</textarea>") && page.contains(r#"value="16""#), "{page}");
    assert!(page.contains(r#"<a href="/reading" aria-current="page">Reading</a>"#), "{page}");
    let user = auth::find_user(&site.pool, "john").await.unwrap().unwrap();
    assert_eq!((user.interests.as_str(), user.daily_stories), ("Compilers.\nNot watches. <b>", 16));
    // How the issue is made is the reader's own too; an unticked box is off.
    assert_eq!(
        (user.format.as_str(), user.most_per_subject, user.show_reasons, user.explore),
        ("articles", 4, true, false)
    );
    assert!(page.contains(r#"<option value="articles" selected>"#) && page.contains(r#"name="explore" value="1">"#));
    let mut odd = reading("Compilers.", "16").to_vec();
    odd[3] = ("format", "pdf");
    assert_eq!(browser.post(&site, "/account/reading", &odd).await.status, StatusCode::BAD_REQUEST);
    odd[3] = ("format", "issue");
    odd[4] = ("most_per_subject", "21");
    let reply = browser.post(&site, "/account/reading", &odd).await;
    assert!(
        reply.status == StatusCode::BAD_REQUEST
            && reply.text.contains("Most on one subject must be a number from 0 to 20.")
    );
    // Notes kept from ratings: on by default, the reader's to turn off.
    assert!(user.learn && page.contains(r#"name="learn" value="1" checked"#));
    assert_eq!(
        browser.post(&site, "/learned/switch", &[("csrf", csrf.as_str())]).await.location,
        "/reading?saved=learn"
    );
    assert!(!auth::find_user(&site.pool, "john").await.unwrap().unwrap().learn);
    assert!(browser.get(&site, "/reading").await.text.contains("Turned off: the notes below are not rewritten"));
    // Kindle addresses, on the account page: the reader's own, checked for shape.
    let kindle = [("csrf", csrf.as_str()), ("kindle_addresses", "a@kindle.com\nnot an address"), ("colour", "1")];
    let reply = browser.post(&site, "/account/kindle", &kindle).await;
    assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains("not an address is not an email address."));
    assert!(reply.text.contains("a@kindle.com\nnot an address</textarea>"), "shown as typed: {}", reply.text);
    assert!(reply.text.contains(r#"name="colour" value="1" checked"#), "and the tick with it");
    assert!(auth::find_user(&site.pool, "john").await.unwrap().unwrap().grayscale, "nothing saved");
    let kindle = [("csrf", csrf.as_str()), ("kindle_addresses", " a@kindle.com \n\nb@kindle.com")];
    assert_eq!(browser.post(&site, "/account/kindle", &kindle).await.location, "/account?saved=kindle");
    let user = auth::find_user(&site.pool, "john").await.unwrap().unwrap();
    assert_eq!(user.kindle_addresses, "a@kindle.com\nb@kindle.com");
    assert!(user.grayscale, "an e-ink screen unless the reader says it is in colour");
    let colour = [("csrf", csrf.as_str()), ("kindle_addresses", "a@kindle.com\nb@kindle.com"), ("colour", "1")];
    browser.post(&site, "/account/kindle", &colour).await;
    assert!(!auth::find_user(&site.pool, "john").await.unwrap().unwrap().grayscale);
    assert!(browser.get(&site, "/account").await.text.contains(r#"name="colour" value="1" checked"#));
    // The daily email is a tick of its own: off, the issue is read on the web pages only.
    assert!(auth::find_user(&site.pool, "mary").await.unwrap().unwrap().daily_to_kindle, "on for a new account");
    assert!(!auth::find_user(&site.pool, "john").await.unwrap().unwrap().daily_to_kindle, "the form above left it off");
    let daily = [("csrf", csrf.as_str()), ("kindle_addresses", "a@kindle.com\nb@kindle.com"), ("daily", "1")];
    browser.post(&site, "/account/kindle", &daily).await;
    assert!(auth::find_user(&site.pool, "john").await.unwrap().unwrap().daily_to_kindle);
    assert!(browser.get(&site, "/account").await.text.contains(r#"name="daily" value="1" checked"#));
    // Refused, the form comes back with the tick as sent, and nothing is saved.
    let slip = [("csrf", csrf.as_str()), ("kindle_addresses", "a@kindle.com\nnot an address")];
    let reply = browser.post(&site, "/account/kindle", &slip).await;
    assert!(reply.status == StatusCode::BAD_REQUEST && !reply.text.contains(r#"name="daily" value="1" checked"#));
    assert!(auth::find_user(&site.pool, "john").await.unwrap().unwrap().daily_to_kindle, "nothing saved");

    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    let page = mary.get(&site, "/account").await;
    assert!(page.text.contains(r#"name="kindle_addresses""#), "a reader sets their own Kindle addresses");
    let token = page.csrf();
    let form = [
        ("csrf", token.as_str()),
        ("interests", "Sailing."),
        ("daily_stories", "5"),
        ("format", "issue"),
        ("most_per_subject", "2"),
    ];
    assert_eq!(mary.post(&site, "/account/reading", &form).await.status, StatusCode::SEE_OTHER);
    let form = [("csrf", token.as_str()), ("kindle_addresses", "mary@kindle.com")];
    assert_eq!(mary.post(&site, "/account/kindle", &form).await.location, "/account?saved=kindle");
    let user = auth::find_user(&site.pool, "mary").await.unwrap().unwrap();
    assert_eq!(
        (user.interests.as_str(), user.daily_stories, user.kindle_addresses.as_str()),
        ("Sailing.", 5, "mary@kindle.com")
    );
    // Only their own row: the admin's addresses are untouched.
    assert_eq!(
        auth::find_user(&site.pool, "john").await.unwrap().unwrap().kindle_addresses,
        "a@kindle.com\nb@kindle.com"
    );

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
async fn an_admin_invites_a_reader_by_email_and_the_link_lets_them_join_once() {
    let site = site(false).await;
    let mut john = Browser::default();
    john.sign_in(&site, "john").await;
    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;

    // Admins only, from the nav; a reader is refused and does not see the page.
    assert_eq!(mary.get(&site, "/accounts").await.status, StatusCode::FORBIDDEN);
    assert!(!mary.get(&site, "/").await.text.contains(r#"href="/accounts""#));
    // Each reader's Kindle addresses, marked when their daily issue stays on the web pages.
    db::set_kindle(&site.pool, "john", "john@kindle.com", true, true).await.unwrap();
    db::set_kindle(&site.pool, "mary", "mary@kindle.com", true, false).await.unwrap();
    let page = john.get(&site, "/accounts").await;
    assert!(page.status == StatusCode::OK && page.text.contains("Mary") && page.text.contains("Added "));
    assert!(page.text.contains("Kindle: mary@kindle.com (daily issue on the web only)"), "{}", page.text);
    assert!(page.text.contains("Kindle: john@kindle.com ·"), "no mark while the daily email is on");
    let csrf = page.csrf();
    let invite = |email: &'static str, id: &'static str, role: &'static str| {
        vec![
            ("csrf", csrf.clone()),
            ("email", email.to_owned()),
            ("id", id.to_owned()),
            ("name", String::new()),
            ("role", role.to_owned()),
        ]
    };

    // Refused: not an address, an address in use, an id in use, and the wrong role.
    for (form, said) in [
        (invite("not an address", "", "reader"), "not an email address"),
        (invite("mary@example.com", "mary2", "reader"), "already uses that address"),
        (invite("jane@example.com", "mary", "reader"), "already an account mary"),
        (invite("jane@example.com", "", "owner"), "Choose a role"),
    ] {
        let reply = invite_post(&site, &mut john, &form).await;
        assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains(said), "{said}: {}", reply.text);
    }
    assert!(site.emails() == 0 && auth::find_user(&site.pool, "jane").await.unwrap().is_none());
    assert_eq!(
        invite_post(&site, &mut mary, &invite("jane@example.com", "", "reader")).await.status,
        StatusCode::FORBIDDEN
    );

    // Sent: the account exists at once, invited, with the id taken from the address.
    let sent = invite_post(&site, &mut john, &invite("Jane.Doe@Example.com", "", "reader")).await;
    assert!(
        sent.status == StatusCode::OK && sent.text.contains("Invitation sent to jane.doe@example.com"),
        "{}",
        sent.text
    );
    let mail = site.mail.lock().unwrap().last().cloned().unwrap();
    assert!(mail.to == "jane.doe@example.com" && mail.body.contains("John has invited you"));
    assert!(mail.body.contains("issues@example.com"), "the sender every Kindle must approve is in the email");
    let link = site.emailed_link();
    assert!(link.starts_with("/join/") && sent.text.contains(&link), "the admin is shown the link too");
    let jane = auth::find_user(&site.pool, "jane.doe").await.unwrap().unwrap();
    assert!(jane.is_invited() && jane.email.as_deref() == Some("jane.doe@example.com") && jane.role == "reader");
    assert_eq!(jane.timezone, "America/New_York", "the admin's zone, to start with");
    let listed = john.get(&site, "/accounts").await.text;
    assert!(listed.contains("jane.doe") && listed.contains("Invited; the link works until"));

    // Until she joins: no sign-in with any password, no reset email, no password change by hand.
    let mut jane_browser = Browser::default();
    let tried = jane_browser.post(&site, "/login", &[("email", "jane.doe@example.com"), ("password", "")]).await;
    assert_eq!(tried.status, StatusCode::UNAUTHORIZED);
    jane_browser.post(&site, "/forgot", &[("email", "jane.doe@example.com")]).await;
    assert_eq!(site.emails(), 1);

    // The link opens the join page, which names the address and the sender to approve.
    assert_eq!(jane_browser.get(&site, "/join/not-a-token").await.status, StatusCode::BAD_REQUEST);
    let form = jane_browser.get(&site, &link).await;
    assert!(form.status == StatusCode::OK && form.text.contains("<b>jane.doe@example.com</b>"), "{}", form.text);
    assert!(form.text.contains("<b>issues@example.com</b>") && form.text.contains("amazon.com/hz/mycd"));
    let join =
        |name: &'static str, password: &'static str, again: &'static str, zone: &'static str, kindle: &'static str| {
            [("name", name), ("password", password), ("again", again), ("timezone", zone), ("kindle_addresses", kindle)]
        };
    for (form, said) in [
        (join("Jane", "short", "short", "Europe/London", ""), "at least 10 characters"),
        (join("Jane", PASSWORD, "something else", "Europe/London", ""), "not the same"),
        (join("Jane", PASSWORD, PASSWORD, "Mars/Olympus", ""), "not a time zone"),
        (join("Jane", PASSWORD, PASSWORD, "Europe/London", "jane at kindle"), "not an email address"),
        (join("", PASSWORD, PASSWORD, "Europe/London", ""), "display name"),
    ] {
        let reply = jane_browser.post(&site, &link, &form).await;
        assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains(said), "{said}: {}", reply.text);
    }
    assert!(auth::find_user(&site.pool, "jane.doe").await.unwrap().unwrap().is_invited());
    let foreign = jane_browser
        .post_from(&site, &link, &join("Jane", PASSWORD, PASSWORD, "Europe/London", ""), Some("https://evil.example"))
        .await;
    assert_eq!(foreign.status, StatusCode::FORBIDDEN);

    // Joined: signed in at once, taken to the Reading page, and the account is hers.
    let done =
        jane_browser.post(&site, &link, &join("Jane", PASSWORD, PASSWORD, "Europe/London", "jane@kindle.com\n")).await;
    assert_eq!(done.location, "/reading?saved=joined", "{}", done.text);
    let reading = jane_browser.get(&site, &done.location).await;
    assert!(reading.status == StatusCode::OK && reading.text.contains("Welcome."), "{}", reading.text);
    let jane = auth::find_user(&site.pool, "jane.doe").await.unwrap().unwrap();
    assert!(!jane.is_invited() && jane.email_verified_at.is_some(), "the link reached the address: confirmed");
    assert!(jane.name == "Jane" && jane.timezone == "Europe/London" && jane.kindle_addresses == "jane@kindle.com");
    let follows: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM follows WHERE user_id = 'jane.doe'")
        .fetch_one(&site.pool)
        .await
        .unwrap();
    assert!(follows > 0, "she follows what the household shares: the built-in sources");
    // The link is used up; the password signs her in elsewhere.
    assert_eq!(jane_browser.get(&site, &link).await.status, StatusCode::BAD_REQUEST);
    let mut phone = Browser::default();
    let signed = phone.post(&site, "/login", &[("email", "jane.doe@example.com"), ("password", PASSWORD)]).await;
    assert_eq!(signed.status, StatusCode::SEE_OTHER);
    let listed = john.get(&site, "/accounts").await.text;
    assert!(listed.contains("Jane") && listed.contains("jane@kindle.com") && !listed.contains("Invited;"));
    // Joined accounts cannot be resent or revoked.
    let csrf = john.get(&site, "/accounts").await.csrf();
    assert_eq!(john.post(&site, "/accounts/jane.doe/resend", &[("csrf", &csrf)]).await.status, StatusCode::NOT_FOUND);
    assert_eq!(john.post(&site, "/accounts/jane.doe/revoke", &[("csrf", &csrf)]).await.status, StatusCode::NOT_FOUND);
    assert!(auth::find_user(&site.pool, "jane.doe").await.unwrap().is_some());

    // A second invitation: a fresh link replaces the old; taking it back removes the account.
    invite_post(&site, &mut john, &invite("bob@example.com", "bob", "admin")).await;
    let first = site.emailed_link();
    let csrf = john.get(&site, "/accounts").await.csrf();
    let again = john.post(&site, "/accounts/bob/resend", &[("csrf", &csrf)]).await;
    assert!(again.status == StatusCode::OK && again.text.contains("A new link was sent to bob@example.com"));
    let second = site.emailed_link();
    assert!(first != second && site.emails() == 3);
    assert_eq!(phone.get(&site, &first).await.status, StatusCode::BAD_REQUEST);
    assert_eq!(phone.get(&site, &second).await.status, StatusCode::OK);
    assert_eq!(mary.post(&site, "/accounts/bob/revoke", &[("csrf", &csrf)]).await.status, StatusCode::FORBIDDEN);
    let revoked = john.post(&site, "/accounts/bob/revoke", &[("csrf", &csrf)]).await;
    assert_eq!(revoked.location, "/accounts?saved=revoked");
    assert!(john.get(&site, &revoked.location).await.text.contains("Invitation taken back"));
    assert!(auth::find_user(&site.pool, "bob").await.unwrap().is_none());
    assert_eq!(phone.get(&site, &second).await.status, StatusCode::BAD_REQUEST);
    let tokens: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM email_tokens").fetch_one(&site.pool).await.unwrap();
    assert_eq!(tokens, 0);

    // The account page's Kindle hint names the sender too.
    let account = john.get(&site, "/account").await.text;
    assert!(account.contains("<b>issues@example.com</b>") && account.contains("amazon.com/hz/mycd"));
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
    db::set_kindle(pool, "john", "john@kindle.com", true, true).await.unwrap();
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

/// The sources page as the admin sees it, and this reader's view of the sources.
async fn sources_of(site: &Site, id: &str) -> Vec<forenoon_web::sources::Source> {
    let user = auth::find_user(&site.pool, id).await.unwrap().unwrap();
    forenoon_web::sources::list(&site.pool, &user).await.unwrap()
}

#[tokio::test]
async fn feeds_are_added_checked_filtered_and_changed() {
    let site = site(true).await;
    let mut admin = Browser::default();
    admin.sign_in(&site, "john").await;
    let page = admin.get(&site, "/sources").await;
    assert!(page.status == StatusCode::OK && page.text.contains("No feeds yet"));
    assert!(admin.get(&site, "/").await.text.contains(r#"<a href="/sources">Sources</a>"#));
    let csrf = page.csrf();

    // Every reader has the page; what they see there is their own (see the household test).
    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    assert!(mary.get(&site, "/").await.text.contains(r#"<a href="/sources">Sources</a>"#));
    assert_eq!(mary.get(&site, "/sources").await.status, StatusCode::OK);
    let token = mary.get(&site, "/account").await.csrf();

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
    let stored = feeds(sources_of(&site, "john").await);
    assert!(stored.iter().all(|source| source.check_state == "pending"));
    // The admin's feeds, private to him as added here: the reader's page does not have them.
    assert!(stored.iter().all(|source| source.added_by == "john" && source.followed && !source.shared));
    assert!(!mary.get(&site, "/sources").await.text.contains("blog.example"));
    assert!(feeds(sources_of(&site, "mary").await).is_empty());
    sqlx::query("UPDATE sources SET check_state = 'ok', check_note = 'Feed found at https://blog.example/rss.xml: 3 articles.' WHERE name != 'Other'")
        .execute(&site.pool).await.unwrap();
    sqlx::query("UPDATE sources SET check_state = 'failed', check_note = 'Not usable: no feed found at this address.' WHERE name = 'Other'")
        .execute(&site.pool)
        .await
        .unwrap();
    let text = admin.get(&site, "/sources").await.text;
    assert!(
        text.contains("Feed found at https://blog.example/rss.xml")
            && text.contains(r#"<p class="state failing">Not usable: no feed found at this address.</p>"#)
    );
    assert!(text.contains(r#"<a href="/sources" aria-current="page">Sources</a>"#), "this page is marked in the nav");
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
    // The failing feed is listed first, under its own label; the views and the search narrow the list.
    let (blog_row, other_row) = (format!(r#"id="s-{}""#, stored[0].id), format!(r#"id="s-{}""#, stored[1].id));
    assert!(text.contains("Needs attention") && text.find(&other_row) < text.find(&blog_row), "{text}");
    let failing = admin.get(&site, "/sources?show=failing").await.text;
    assert!(failing.contains(&other_row) && !failing.contains(&blog_row));
    assert!(
        failing.contains("<b>Failing (1)</b>") && failing.contains(r#"<a href="/sources?show=paused">Paused (0)</a>"#)
    );
    let searched = admin.get(&site, "/sources?q=BLOG.example").await.text;
    assert!(searched.contains(&blog_row) && !searched.contains(&other_row));
    assert!(searched.contains(r#"<a href="/sources?show=failing&#38;q=BLOG%2Eexample">Failing (0)</a>"#), "{searched}");
    assert!(admin.get(&site, "/sources?q=nothing").await.text.contains("No feeds match."));

    // Pause one, rename it, then remove the other. A reader cannot touch what is not theirs.
    let (blog, other) = (stored[0].id, stored[1].id);
    assert_eq!(mary.post(&site, &format!("/sources/{blog}"), &[("csrf", &token)]).await.status, StatusCode::NOT_FOUND);
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
        StatusCode::NOT_FOUND
    );
    let stored = feeds(sources_of(&site, "john").await);
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
    let stored = feeds(sources_of(&site, "john").await);
    assert!(stored.iter().any(|s| s.name == "Paper" && s.extract == "link"));
}

#[tokio::test]
async fn a_follow_made_for_a_reader_holds_nothing_until_they_choose_it() {
    let site = site(true).await;
    let pool = &site.pool;
    let hash = auth::hash_password(PASSWORD.to_owned()).await.unwrap();
    db::add_user(pool, "carl", "carl@example.com", "Carl", &hash, "reader", "UTC").await.unwrap();
    let (mut admin, mut mary, mut carl) = (Browser::default(), Browser::default(), Browser::default());
    admin.sign_in(&site, "john").await;
    mary.sign_in(&site, "mary").await;
    carl.sign_in(&site, "carl").await;
    let (admin_csrf, mary_csrf, carl_csrf) = (
        admin.get(&site, "/sources").await.csrf(),
        mary.get(&site, "/sources").await.csrf(),
        carl.get(&site, "/sources").await.csrf(),
    );
    let chosen = |user: &'static str, source: i64| async move {
        sqlx::query_scalar::<_, bool>("SELECT chosen FROM follows WHERE user_id = ? AND source_id = ?")
            .bind(user)
            .bind(source)
            .fetch_optional(pool)
            .await
            .unwrap()
    };
    let here = &site;
    let feed_of = |user: &'static str, name: &'static str| async move {
        sources_of(here, user).await.into_iter().find(|s| s.name == name)
    };

    // Carl turns off following shared feeds; Mary adds one shared. It is followed for the admin,
    // made for him, not chosen; Carl sees it as new, not followed.
    let off = [("csrf", carl_csrf.as_str())];
    assert_eq!(carl.post(&site, "/sources/follow-new", &off).await.location, "/sources?saved=follownew");
    assert!(!db::follows_new(pool, "carl").await.unwrap());
    let blog =
        [("csrf", mary_csrf.as_str()), ("name", "Blog"), ("address", "https://blog.example/rss"), ("shared", "1")];
    mary.post(&site, "/sources", &blog).await;
    let id = feed_of("mary", "Blog").await.unwrap().id;
    assert_eq!(
        (chosen("mary", id).await, chosen("john", id).await, chosen("carl", id).await),
        (Some(true), Some(false), None)
    );
    let text = carl.get(&site, "/sources").await.text;
    let row = text.split(&format!(r#"id="s-{id}""#)).nth(1).unwrap().split("</details>").next().unwrap();
    assert!(
        row.contains(">New</span>") && row.contains("+ Follow</button>") && !row.contains(r#"class="dot"#),
        "{row}"
    );
    assert!(!text.contains(r#"name="follow_new" value="1" checked"#));
    let text = admin.get(&site, "/sources").await.text;
    let row = text.split(&format!(r#"id="s-{id}""#)).nth(1).unwrap().split("</details>").next().unwrap();
    // A follow made for him: a ring, its tooltip saying so, and Keep it in the opened row.
    assert!(row.contains(r#" ring" role="img" title="Added for you: open it and keep it to make it yours. "#), "{row}");
    assert!(row.contains("Keep it</button>") && !row.contains(">New</span>") && !row.contains("+ Follow"), "{row}");

    // A follow made for the admin holds nothing: Mary still changes her feed.
    let rename =
        [("csrf", mary_csrf.as_str()), ("name", "Blog"), ("address", "https://blog.example/feed"), ("shared", "1")];
    assert_eq!(mary.post(&site, &format!("/sources/{id}"), &rename).await.location, "/sources?saved=saved");
    assert_eq!(feed_of("mary", "Blog").await.unwrap().url, "https://blog.example/feed");

    // Carl chooses it with the button before its name: now it is kept. Mary can no longer
    // change or remove it, and neither can the admin remove it, only pause it.
    let follow = [("csrf", carl_csrf.as_str()), ("follow", "1")];
    assert_eq!(carl.post(&site, &format!("/sources/{id}/follow"), &follow).await.location, "/sources?saved=followed");
    assert_eq!(chosen("carl", id).await, Some(true));
    let text = carl.get(&site, "/sources").await.text;
    let row = text.split(&format!(r#"id="s-{id}""#)).nth(1).unwrap().split("</details>").next().unwrap();
    assert!(
        row.contains(r#"role="img" title="Following. "#) && !row.contains(" ring\"") && !row.contains("Keep it"),
        "{row}"
    );
    let gone = mary.post(&site, &format!("/sources/{id}/delete"), &[("csrf", &mary_csrf)]).await;
    assert!(gone.status == StatusCode::FORBIDDEN && gone.text.contains("Another reader follows this feed"));
    assert!(
        gone.text.contains(r#"<p class="error">"#) && gone.text.contains("Add a feed"),
        "the page, not a bare error"
    );
    assert_eq!(mary.post(&site, &format!("/sources/{id}"), &rename).await.status, StatusCode::FORBIDDEN);
    let gone = admin.post(&site, &format!("/sources/{id}/delete"), &[("csrf", &admin_csrf)]).await;
    assert_eq!(gone.status, StatusCode::FORBIDDEN);
    let text = admin.get(&site, "/sources").await.text;
    let row = text.split(&format!(r#"id="s-{id}""#)).nth(1).unwrap().split("</details>").next().unwrap();
    assert!(!row.contains("Remove</button>") && row.contains("it can be paused, not removed"), "{row}");
    assert!(row.contains("Fetch it for the household (untick to pause)"));

    // Mary, having unfollowed her own kept feed, is still told why she cannot remove it.
    mary.post(&site, &format!("/sources/{id}/follow"), &[("csrf", mary_csrf.as_str()), ("follow", "0")]).await;
    let text = mary.get(&site, "/sources").await.text;
    let row = text.split(&format!(r#"id="s-{id}""#)).nth(1).unwrap().split("</details>").next().unwrap();
    assert!(!row.contains("Remove</button>") && row.contains("nobody removes it"), "{row}");
    mary.post(&site, &format!("/sources/{id}/follow"), &[("csrf", mary_csrf.as_str()), ("follow", "1")]).await;

    // Pinning a follow made for the admin chooses it; following again keeps its pin.
    let pin = [
        ("csrf", admin_csrf.as_str()),
        ("name", "Blog"),
        ("address", "https://blog.example/feed"),
        ("pinned", "1"),
        ("enabled", "1"),
        ("shared", "1"),
    ];
    admin.post(&site, &format!("/sources/{id}"), &pin).await;
    assert_eq!(chosen("john", id).await, Some(true));
    let again = [("csrf", admin_csrf.as_str()), ("follow", "1")];
    admin.post(&site, &format!("/sources/{id}/follow"), &again).await;
    assert!(feed_of("john", "Blog").await.unwrap().pinned);

    // Once nobody else has chosen it, its adder may remove it again.
    for (who, csrf) in [(&mut carl, &carl_csrf), (&mut admin, &admin_csrf)] {
        who.post(&site, &format!("/sources/{id}/follow"), &[("csrf", csrf.as_str()), ("follow", "0")]).await;
    }
    assert_eq!(
        mary.post(&site, &format!("/sources/{id}/delete"), &[("csrf", &mary_csrf)]).await.location,
        "/sources?saved=removed"
    );

    // A new account's follows are made for it, not chosen.
    db::add_user(pool, "dana", "dana@example.com", "Dana", &hash, "reader", "UTC").await.unwrap();
    db::follow_shared(pool, "dana").await.unwrap();
    let made: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM follows WHERE user_id = 'dana' AND chosen = 0")
        .fetch_one(pool)
        .await
        .unwrap();
    let all: i64 =
        sqlx::query_scalar("SELECT COUNT(*) FROM follows WHERE user_id = 'dana'").fetch_one(pool).await.unwrap();
    assert!(made > 0 && made == all);
}

#[tokio::test]
async fn a_readers_feed_is_private_until_shared_and_the_household_shares_sessions() {
    let site = site(true).await;
    let hash = auth::hash_password(PASSWORD.to_owned()).await.unwrap();
    db::add_user(&site.pool, "carl", "carl@example.com", "Carl", &hash, "reader", "UTC").await.unwrap();
    db::follow_shared(&site.pool, "carl").await.unwrap();
    let (mut admin, mut mary, mut carl) = (Browser::default(), Browser::default(), Browser::default());
    admin.sign_in(&site, "john").await;
    mary.sign_in(&site, "mary").await;
    carl.sign_in(&site, "carl").await;
    let (admin_csrf, mary_csrf, carl_csrf) = (
        admin.get(&site, "/sources").await.csrf(),
        mary.get(&site, "/sources").await.csrf(),
        carl.get(&site, "/sources").await.csrf(),
    );
    // A new account follows what is shared: the built-in sources, so far.
    let text = carl.get(&site, "/sources").await.text;
    assert!(text.contains("Following 2 of 2 sources, 0 failing.") && !text.contains("Not followed"), "{text}");

    // Mary adds a feed. It is hers: Carl sees nothing of it, and cannot follow or add to it.
    let form = [("csrf", mary_csrf.as_str()), ("name", "Mine"), ("address", "https://www.paper.example/rss")];
    let added = mary.post(&site, "/sources", &form).await.location;
    let mine = sources_of(&site, "mary").await.into_iter().find(|s| s.name == "Mine").unwrap();
    assert!(added.starts_with("/sources?saved=added") && mine.followed && mine.added_by == "mary" && !mine.shared);
    let text = mary.get(&site, "/sources").await.text;
    assert!(
        text.contains("Following 3 of 3 sources") && text.contains(r#"until they share it">Private</span>"#),
        "{text}"
    );
    assert!(!text.contains("Private: "), "the adder is not told their own name");
    let text = carl.get(&site, "/sources").await.text;
    assert!(!text.contains("Mine") && !text.contains("paper.example"), "{text}");
    assert!(sources_of(&site, "carl").await.iter().all(|s| s.id != mine.id));
    let follow = [("csrf", carl_csrf.as_str()), ("follow", "1")];
    assert_eq!(carl.post(&site, &format!("/sources/{}/follow", mine.id), &follow).await.status, StatusCode::NOT_FOUND);
    assert_eq!(carl.post(&site, &format!("/sources/{}", mine.id), &follow).await.status, StatusCode::NOT_FOUND);
    assert_eq!(
        carl.post(&site, &format!("/sources/{}/delete", mine.id), &[("csrf", &carl_csrf)]).await.status,
        StatusCode::NOT_FOUND
    );
    // Carl adding the same address gets a feed of his own: Mary's private one is not matched,
    // so he learns nothing of it, and it stays hers to change.
    let same = [("csrf", carl_csrf.as_str()), ("name", "Paper"), ("address", "https://paper.example/rss/")];
    let location = carl.post(&site, "/sources", &same).await.location;
    assert!(location.starts_with("/sources?saved=added&new=") && !location.contains(&format!("new={}", mine.id)));
    let count = || async {
        sqlx::query_scalar::<_, i64>("SELECT COUNT(*) FROM sources WHERE kind = 'rss'")
            .fetch_one(&site.pool)
            .await
            .unwrap()
    };
    assert_eq!(count().await, 2);
    let paper = sources_of(&site, "carl").await.into_iter().find(|s| s.name == "Paper").unwrap();
    assert!(paper.added_by == "carl" && paper.followed && paper.followers == 1 && !paper.shared);
    assert!(sources_of(&site, "carl").await.iter().all(|s| s.id != mine.id));
    assert!(sources_of(&site, "mary").await.iter().all(|s| s.id != paper.id));
    assert_eq!(sources_of(&site, "mary").await.into_iter().find(|s| s.id == mine.id).unwrap().followers, 1);
    assert_eq!(
        carl.post(&site, &format!("/sources/{}/delete", paper.id), &[("csrf", &carl_csrf)]).await.location,
        "/sources?saved=removed"
    );
    assert_eq!(count().await, 1);

    // The admin sees Mary's, with whose it is, and shares it with the household.
    let text = admin.get(&site, "/sources").await.text;
    assert!(
        text.contains(r#"until they share it">Private: Mary</span>"#) && text.contains("Not followed by you"),
        "{text}"
    );
    assert!(text.contains("3 sources, 0 paused, 0 failing."));
    let share = [
        ("csrf", admin_csrf.as_str()),
        ("name", "Mine"),
        ("address", "https://www.paper.example/rss"),
        ("enabled", "1"),
        ("shared", "1"),
    ];
    assert_eq!(admin.post(&site, &format!("/sources/{}", mine.id), &share).await.location, "/sources?saved=saved");
    // Shared, it is followed for every reader, as it would be for an account made now.
    let text = carl.get(&site, "/sources").await.text;
    assert!(
        text.contains("Following 3 of 3 sources")
            && text.contains(r#"any reader can follow it">Shared</span>"#)
            && !text.contains("Shared with the household, not followed"),
        "{text}"
    );
    assert_eq!(sources_of(&site, "mary").await.into_iter().find(|s| s.id == mine.id).unwrap().followers, 3);
    // Carl unfollows it, and a later edit of the shared feed leaves that alone.
    let unfollow = [("csrf", carl_csrf.as_str()), ("follow", "0")];
    carl.post(&site, &format!("/sources/{}/follow", mine.id), &unfollow).await;
    assert_eq!(admin.post(&site, &format!("/sources/{}", mine.id), &share).await.location, "/sources?saved=saved");
    let text = carl.get(&site, "/sources").await.text;
    assert!(text.contains("Shared with the household, not followed") && text.contains("Following 2 of 3 sources"));
    // Adding its address again, however dressed, follows it instead.
    assert_eq!(
        carl.post(&site, "/sources", &same).await.location,
        format!("/sources?saved=followed&new={0}#s-{0}", mine.id)
    );
    assert_eq!(count().await, 1);
    let mine_for_carl = sources_of(&site, "carl").await.into_iter().find(|s| s.id == mine.id).unwrap();
    assert!(mine_for_carl.followed && mine_for_carl.followers == 3 && mine_for_carl.name == "Mine");
    assert!(carl.get(&site, "/sources").await.text.contains("Following 3 of 3 sources"));
    // A feed added shared is followed for everyone straight away; one added private is not.
    let wire =
        [("csrf", admin_csrf.as_str()), ("name", "Wire"), ("address", "https://wire.example/rss"), ("shared", "1")];
    admin.post(&site, "/sources", &wire).await;
    let wire = sources_of(&site, "carl").await.into_iter().find(|s| s.name == "Wire").unwrap();
    assert!(wire.followed && !wire.pinned && wire.followers == 3);
    let quiet = [("csrf", admin_csrf.as_str()), ("name", "Quiet"), ("address", "https://quiet.example/rss")];
    admin.post(&site, "/sources", &quiet).await;
    assert!(sources_of(&site, "carl").await.iter().all(|s| s.name != "Quiet"));
    for name in ["Wire", "Quiet"] {
        let id = sources_of(&site, "john").await.into_iter().find(|s| s.name == name).unwrap().id;
        admin.post(&site, &format!("/sources/{id}/delete"), &[("csrf", &admin_csrf)]).await;
    }
    assert_eq!(count().await, 1);

    // With two followers it is no longer Mary's alone to change or remove: she pins it for
    // herself, and still says whether it is shared.
    let edit = [
        ("csrf", mary_csrf.as_str()),
        ("name", "Renamed"),
        ("address", "https://elsewhere.example/rss"),
        ("pinned", "1"),
    ];
    let pin = [("csrf", mary_csrf.as_str()), ("pinned", "1")];
    assert_eq!(mary.post(&site, &format!("/sources/{}", mine.id), &pin).await.location, "/sources?saved=saved");
    let mine_now = sources_of(&site, "mary").await.into_iter().find(|s| s.id == mine.id).unwrap();
    assert!(mine_now.name == "Mine" && mine_now.url == "https://www.paper.example/rss" && mine_now.pinned);
    assert!(!mine_now.shared, "the form came back without the shared tick, so it is hers alone again");
    let for_carl = sources_of(&site, "carl").await.into_iter().find(|s| s.id == mine.id).unwrap();
    assert!(for_carl.followed && !for_carl.pinned, "a follower keeps following, and pins are each reader's own");
    let reshare = [("csrf", mary_csrf.as_str()), ("pinned", "1"), ("shared", "1")];
    assert_eq!(mary.post(&site, &format!("/sources/{}", mine.id), &reshare).await.location, "/sources?saved=saved");
    assert!(sources_of(&site, "mary").await.into_iter().find(|s| s.id == mine.id).unwrap().shared);
    assert_eq!(
        mary.post(&site, &format!("/sources/{}/delete", mine.id), &[("csrf", &mary_csrf)]).await.status,
        StatusCode::FORBIDDEN
    );
    let text = mary.get(&site, "/sources").await.text;
    let row = text.split(&format!(r#"id="s-{}""#, mine.id)).nth(1).unwrap().split("</details>").next().unwrap();
    assert!(
        row.contains("only an admin can change its address") && row.contains("so is whether it is shared"),
        "{row}"
    );
    assert!(!row.contains(r#"name="address""#) && row.contains(r#"name="pinned""#) && row.contains(r#"name="shared""#));
    // Carl, a follower who did not add it, pins and nothing more.
    let text = carl.get(&site, "/sources").await.text;
    let row = text.split(&format!(r#"id="s-{}""#, mine.id)).nth(1).unwrap().split("</details>").next().unwrap();
    assert!(
        row.contains(r#"name="pinned""#) && !row.contains(r#"name="shared""#) && !row.contains(r#"name="address""#)
    );
    assert!(row.contains("Only the reader who added this feed") && !row.contains("Others follow"), "{row}");
    // A form loaded while the feed was hers alone, sent after someone followed, is refused
    // rather than half-applied.
    let stale = mary.post(&site, &format!("/sources/{}", mine.id), &edit).await;
    assert!(stale.status == StatusCode::FORBIDDEN && stale.text.contains("Another reader chose to follow this feed"));
    assert!(
        stale.text.contains(r#"<p class="error">"#) && stale.text.contains("Add a feed"),
        "the page, not a bare error"
    );

    // A session is the household's: Mary, with a feed on the site, saves it; Carl, following
    // that feed, sees it is there but cannot remove it; an admin can. Nobody without a feed on
    // the site sees it or may save one.
    let path = "/sources/sessions";
    let cookie = [("csrf", mary_csrf.as_str()), ("site", "paper.example"), ("cookie", "id=s3cret")];
    assert_eq!(mary.post(&site, path, &cookie).await.location, "/sources?saved=session");
    let text = carl.get(&site, "/sources").await.text;
    assert!(text.contains("<b class=\"wide\">paper.example</b>"), "{text}");
    assert!(text.contains("fetched with the household's paper.example session"), "{text}");
    assert!(!text.contains("Remove</button>"), "nothing on Carl's page is his to remove");
    let remove = |csrf: &str| [("csrf", csrf.to_owned()), ("site", "paper.example".to_owned())];
    let form = remove(&carl_csrf);
    let form: Vec<(&str, &str)> = form.iter().map(|(k, v)| (*k, v.as_str())).collect();
    assert_eq!(carl.post(&site, "/sources/sessions/delete", &form).await.status, StatusCode::FORBIDDEN);
    assert!(site.folder.path().join("logins/site-paper.example.json").exists());
    let elsewhere = [("csrf", carl_csrf.as_str()), ("site", "nytimes.com"), ("cookie", "id=1")];
    let reply = carl.post(&site, path, &elsewhere).await;
    assert!(
        reply.status == StatusCode::BAD_REQUEST && reply.text.contains("Add or follow a feed on nytimes.com first")
    );
    carl.post(&site, &format!("/sources/{}/follow", mine.id), &[("csrf", &carl_csrf), ("follow", "0")]).await;
    let text = carl.get(&site, "/sources").await.text;
    assert!(!text.contains("<b class=\"wide\">paper.example</b>"), "no feed on the site: no session shown");
    assert!(!text.contains("paper.example session"), "nor the tag on the feed he does not follow");
    // Without a feed on the site, a removal attempt is answered as if there were no session,
    // whether or not there is one: nothing to probe.
    for probe in ["paper.example", "nytimes.com"] {
        let form = [("csrf", carl_csrf.as_str()), ("site", probe)];
        assert_eq!(carl.post(&site, "/sources/sessions/delete", &form).await.location, "/sources?saved=nosession");
    }
    assert!(site.folder.path().join("logins/site-paper.example.json").exists());
    let form = remove(&admin_csrf);
    let form: Vec<(&str, &str)> = form.iter().map(|(k, v)| (*k, v.as_str())).collect();
    assert_eq!(admin.post(&site, "/sources/sessions/delete", &form).await.location, "/sources?saved=nosession");
    assert!(!site.folder.path().join("logins/site-paper.example.json").exists());

    // Alone again on the feed she added (sharing had it followed for the admin too), Mary may
    // remove it; its follows go with it.
    admin.post(&site, &format!("/sources/{}/follow", mine.id), &[("csrf", &admin_csrf), ("follow", "0")]).await;
    assert_eq!(
        mary.post(&site, &format!("/sources/{}/delete", mine.id), &[("csrf", &mary_csrf)]).await.location,
        "/sources?saved=removed"
    );
    let follows: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM follows WHERE source_id = ?")
        .bind(mine.id)
        .fetch_one(&site.pool)
        .await
        .unwrap();
    assert_eq!(follows, 0);

    // Built-in sources are followed the same way; switching one off is for the whole household.
    let hn = sources_of(&site, "carl").await.into_iter().find(|s| s.builtin == "hackernews").unwrap();
    assert_eq!(
        carl.post(&site, &format!("/sources/{}/follow", hn.id), &[("csrf", &carl_csrf), ("follow", "0")])
            .await
            .location,
        "/sources?saved=unfollowed"
    );
    assert!(!sources_of(&site, "carl").await.into_iter().find(|s| s.id == hn.id).unwrap().followed);
    assert!(sources_of(&site, "mary").await.into_iter().find(|s| s.id == hn.id).unwrap().followed);
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
    let feed = sources_of(&site, "john").await.into_iter().find(|s| s.name == "Paper").unwrap();
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
    assert!(
        text.contains("Its pages are fetched with the household's paper.example session.") && !text.contains("s3cret")
    );
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

    // Not for a reader with no feed on the site; removing a feed leaves the site's session;
    // removing the session takes it.
    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    let token = mary.get(&site, "/account").await.csrf();
    let form = [("csrf", token.as_str()), ("site", "evil.example"), ("cookie", "id=mine")];
    assert_eq!(mary.post(&site, path, &form).await.status, StatusCode::BAD_REQUEST);
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
    assert!(page.text.contains(r#"<span class="name">Hacker News</span>"#) && page.text.contains("Lobsters"));
    let builtin = |all: Vec<forenoon_web::sources::Source>, key: &str| {
        all.into_iter().find(|source| source.builtin == key).expect("a built-in source")
    };
    let hn = builtin(sources_of(&site, "john").await, "hackernews");
    assert!(hn.kind == "builtin" && hn.enabled && hn.daily_max == 0); // many publishers: no limit
    assert!(hn.shared && hn.followed, "built-in sources are the household's, followed from the start");

    let path = format!("/sources/{}/toggle", hn.id);
    let reply = admin.post(&site, &path, &[("csrf", &csrf), ("enabled", "1"), ("daily_max", "x")]).await;
    assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains("Places a day: give a whole number"));
    let off = [("csrf", csrf.as_str()), ("enabled", "0"), ("daily_max", "4")];
    assert_eq!(admin.post(&site, &path, &off).await.location, "/sources?saved=saved");
    let hn = builtin(sources_of(&site, "john").await, "hackernews");
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
    let hn = builtin(sources_of(&site, "john").await, "hackernews");
    assert_eq!((hn.name.as_str(), hn.url.as_str(), hn.enabled), ("Hacker News", "", false));

    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    let token = mary.get(&site, "/account").await.csrf();
    assert_eq!(mary.post(&site, &path, &[("csrf", &token), ("enabled", "1")]).await.status, StatusCode::FORBIDDEN);
    assert_eq!(admin.post(&site, &path, &[("csrf", "wrong"), ("enabled", "1")]).await.status, StatusCode::FORBIDDEN);
    assert!(!builtin(sources_of(&site, "john").await, "hackernews").enabled);
}

#[tokio::test]
async fn settings_are_saved_by_admins_and_only_where_they_differ_from_the_default() {
    let site = site(true).await;
    let mut admin = Browser::default();
    admin.sign_in(&site, "john").await;
    let page = admin.get(&site, "/settings").await;
    let csrf = page.csrf();
    assert!(page.text.contains(r#"<input name="delivery_time" type="time" value="06:00" required>"#));
    assert!(page.text.contains(r#"name="alerts.enabled" value="1" checked"#) && !page.text.contains("changed"));
    assert!(!page.text.contains(r#"name="llm.enabled""#), "the model is required, not switched on");
    assert!(admin.get(&site, "/").await.text.contains(r#"<a href="/settings">Settings</a>"#));
    // What the model used, priced by the pipeline when it logged each call; a call to a model
    // with no known price is said apart. Nothing about prices is a setting.
    assert!(
        page.text.contains("Today: 0 of 600,000 tokens, about $0.0000.") && !page.text.contains("Price per million")
    );
    let now = forenoon_web::db::now_iso();
    for (model, cost) in [("anthropic/claude-haiku-5-5", Some(0.0125)), ("anthropic/claude-new-9", None)] {
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
    // A server you run costs nothing and is outside the cap; a call that never got through is not "unpriced".
    for (model, tokens, status, cost) in [("lan/qwen", 9000, "ok", Some(0.0)), ("lan/qwen", 0, "unreachable", None)] {
        sqlx::query(
            "INSERT INTO llm_calls (created_at, purpose, model, input_tokens, output_tokens, status, cost)
             VALUES (?, 'rank', ?, ?, 0, ?, ?)",
        )
        .bind(&now)
        .bind(model)
        .bind(tokens)
        .bind(status)
        .bind(cost)
        .execute(&site.pool)
        .await
        .unwrap();
    }
    let text = admin.get(&site, "/settings").await.text;
    assert!(text.contains("Today: 103,000 of 600,000 tokens") && text.contains("1 calls this month were to a model"));
    assert!(text.contains("Also 9,000 tokens today and 9,000 this month on servers you run"));
    sqlx::query("DELETE FROM llm_calls WHERE model = 'lan/qwen'").execute(&site.pool).await.unwrap();
    // The model settings are typed, suggesting the shipped models; only the others can be left empty.
    assert!(text.contains(r#"<input name="llm.model" list="shipped-models" maxlength="200" value="anthropic/claude-haiku-5-5" autocomplete="off" spellcheck="false" required>"#));
    assert!(text.contains(r#"name="llm.choose_model" list="shipped-models" maxlength="200" value="" autocomplete="off" spellcheck="false" placeholder="The model above">"#));
    assert_eq!(text.matches(r#"<option value="anthropic/claude-sonnet-5-5">Claude Sonnet 5.5</option>"#).count(), 1);
    // Any model is shown as it was saved, so a save keeps it.
    sqlx::query(
        "INSERT INTO settings (key, value, updated_at) VALUES ('llm.model', 'lan/unsloth/Qwen3.8-27B-GGUF', ?)",
    )
    .bind(&now)
    .execute(&site.pool)
    .await
    .unwrap();
    let text = admin.get(&site, "/settings").await.text;
    assert!(
        text.contains(r#"name="llm.model" list="shipped-models" maxlength="200" value="lan/unsloth/Qwen3.8-27B-GGUF""#)
    );
    sqlx::query("DELETE FROM settings").execute(&site.pool).await.unwrap();

    // Every field as the page shows it, with a few changed.
    let defaults = [
        ("max_age_days", "3"),
        ("llm.model", "anthropic/claude-haiku-5-5"),
        ("llm.daily_token_cap", "600000"),
        ("llm.read_candidates", "40"),
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
        send(form(&[("delivery_time", "07:30"), ("llm.read_candidates", "20"), ("alerts.to", "Ops@Example.com")]));
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
            ("llm.read_candidates", "20")
        ]
    );
    let text = admin.get(&site, "/settings?saved=1").await.text;
    assert!(
        text.contains("Saved.") && text.contains(r#"value="07:30""#) && text.matches("changed</span>").count() == 4
    );

    // Back to the defaults: the rows go away.
    let back = form(&[("delivery_time", "06:00"), ("llm.read_candidates", "40"), ("alerts.on_empty", "1")]);
    let back: Vec<(&str, &str)> = back.iter().map(|(key, value)| (*key, value.as_str())).collect();
    admin.post(&site, "/settings", &back).await;
    let left: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM settings").fetch_one(&site.pool).await.unwrap();
    assert_eq!(left, 0);

    // One wrong value saves nothing and keeps what was typed.
    let wrong = form(&[("delivery_time", "25:00"), ("llm.read_candidates", "9")]);
    let wrong: Vec<(&str, &str)> = wrong.iter().map(|(key, value)| (*key, value.as_str())).collect();
    let reply = admin.post(&site, "/settings", &wrong).await;
    assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains("Deliver at: give a time such as 06:00."));
    assert!(reply.text.contains(r#"name="llm.read_candidates" type="number" step="1" min="0" max="200" value="9""#));
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
async fn the_test_button_saves_and_asks_the_pipeline_to_test_the_models() {
    let site = site(true).await;
    let mut admin = Browser::default();
    admin.sign_in(&site, "john").await;
    let page = admin.get(&site, "/settings").await;
    let csrf = page.csrf();
    assert!(page.text.contains(r#"<button formaction="/settings/test">Save and test</button>"#));
    let save = page.text.find(r#"<button class="primary">Save</button>"#).unwrap();
    assert!(save < page.text.find("/settings/test").unwrap(), "Enter in a field saves: Save comes first");
    assert!(page.text.contains("<code>FORENOON_LLM_LAN_URL</code>"), "where a connection is set is said");

    // Every field as the page shows it, with the models given.
    let full = |models: &[(&'static str, &'static str)]| -> Vec<(&'static str, String)> {
        let mut form = vec![("csrf", csrf.clone())];
        for (key, value) in [
            ("delivery_time", "06:00"),
            ("max_age_days", "3"),
            ("llm.daily_token_cap", "600000"),
            ("llm.read_candidates", "40"),
            ("alerts.enabled", "1"),
            ("alerts.on_empty", "1"),
            ("alerts.source_failures", "3"),
            ("keep_pages_days", "-1"),
            ("keep_issues_days", "-1"),
        ]
        .iter()
        .chain(models)
        {
            form.push((key, (*value).to_owned()));
        }
        form
    };
    // A model must name its connection; nothing is saved or asked for when one does not.
    let bad = full(&[("llm.model", "qwen")]);
    let form: Vec<(&str, &str)> = bad.iter().map(|(k, v)| (*k, v.as_str())).collect();
    let reply = admin.post(&site, "/settings/test", &form).await;
    assert!(reply.status == StatusCode::BAD_REQUEST && reply.text.contains("Model: name a model with its connection"));
    let asked: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM model_tests").fetch_one(&site.pool).await.unwrap();
    assert_eq!(asked, 0);

    // The form is saved, then the test asked for.
    let good = full(&[("llm.model", "lan/qwen3.8-27b"), ("llm.fallback_model", "anthropic/claude-haiku-5-5")]);
    let form: Vec<(&str, &str)> = good.iter().map(|(k, v)| (*k, v.as_str())).collect();
    assert_eq!(admin.get(&site, "/settings/test").await.location, "/settings#model-test", "none asked for yet");
    assert_eq!(admin.post(&site, "/settings/test", &form).await.location, "/settings/test");
    let model: String =
        sqlx::query_scalar("SELECT value FROM settings WHERE key = 'llm.model'").fetch_one(&site.pool).await.unwrap();
    assert_eq!(model, "lan/qwen3.8-27b");
    let (requested, answered): (String, String) =
        sqlx::query_as("SELECT requested_at, answered FROM model_tests").fetch_one(&site.pool).await.unwrap();
    assert!(!requested.is_empty() && answered.is_empty());
    // The results page waits for the pipeline, refreshing itself: no reloading by hand.
    let refresh = r#"<meta http-equiv="refresh" content="3">"#;
    let page = admin.get(&site, "/settings/test").await.text;
    assert!(page.contains(refresh) && page.contains("Waiting for the pipeline to begin,"));
    assert!(admin.get(&site, "/settings").await.text.contains(r#"<a href="/settings/test">A test is under way.</a>"#));
    // Not begun in a minute: it stops refreshing and asks whether the pipeline is running.
    sqlx::query("UPDATE model_tests SET requested_at = '2026-10-17T12:00:00+00:00'").execute(&site.pool).await.unwrap();
    let page = admin.get(&site, "/settings/test").await.text;
    assert!(!page.contains(refresh) && page.contains("check that it is running"));
    assert!(admin.get(&site, "/settings").await.text.contains("The last test was not begun: see why."));

    // Under way: each model's line as it comes, still refreshing.
    let now = forenoon_web::db::now_iso();
    sqlx::query(
        "UPDATE model_tests SET started = requested_at, tested_at = ?,
         result = 'lan/qwen3.8-27b (ranking and choosing): OK: answered in 1.1 s'",
    )
    .bind(&now)
    .execute(&site.pool)
    .await
    .unwrap();
    let page = admin.get(&site, "/settings/test").await.text;
    assert!(page.contains(refresh) && page.contains("Under way"));
    assert!(page.contains("<li>lan/qwen3.8-27b (ranking and choosing): OK: answered in 1.1 s</li>"));
    // Silent for five minutes: given up on.
    sqlx::query("UPDATE model_tests SET tested_at = '2026-10-17T12:00:05+00:00'").execute(&site.pool).await.unwrap();
    let page = admin.get(&site, "/settings/test").await.text;
    assert!(!page.contains(refresh) && page.contains("it may have stopped"));

    // The pipeline's answer is shown, a line per model, here and on Settings.
    sqlx::query(
        "UPDATE model_tests SET answered = requested_at, tested_at = '2026-10-17T12:00:05+00:00',
         result = 'lan/qwen3.8-27b (ranking and choosing): OK: answered in 1.1 s
anthropic/claude-haiku-5-5 (fallback): Failed (could not reach it): no key'",
    )
    .execute(&site.pool)
    .await
    .unwrap();
    let page = admin.get(&site, "/settings/test").await.text;
    assert!(!page.contains(refresh) && page.contains("<h2>Tested</h2>"));
    assert!(page.contains("<li>anthropic/claude-haiku-5-5 (fallback): Failed (could not reach it): no key</li>"));
    let text = admin.get(&site, "/settings").await.text;
    assert!(text.contains("Last tested 2026-10-17 12:00 UTC"));
    assert!(text.contains("<li>lan/qwen3.8-27b (ranking and choosing): OK: answered in 1.1 s</li>"));
    assert!(text.contains("<li>anthropic/claude-haiku-5-5 (fallback): Failed (could not reach it): no key</li>"));

    // Asking again replaces the last.
    admin.post(&site, "/settings/test", &form).await;
    let rows: Vec<(String, String)> =
        sqlx::query_as("SELECT requested_at, answered FROM model_tests").fetch_all(&site.pool).await.unwrap();
    assert!(rows.len() == 1 && rows[0].0 != rows[0].1);

    // Only admins, only from this site's page.
    let mut mary = Browser::default();
    mary.sign_in(&site, "mary").await;
    let token = mary.get(&site, "/account").await.csrf();
    assert_eq!(mary.post(&site, "/settings/test", &[("csrf", token.as_str())]).await.status, StatusCode::FORBIDDEN);
    assert_eq!(mary.get(&site, "/settings/test").await.status, StatusCode::FORBIDDEN);
    assert_eq!(admin.post(&site, "/settings/test", &[("csrf", "wrong")]).await.status, StatusCode::FORBIDDEN);
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

    db::set_kindle(pool, "john", "john@kindle.com", true, true).await.unwrap();
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
    db::set_kindle(pool, "john", "john@kindle.com", true, true).await.unwrap();
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
    let reply = browser.post(&site, "/send", &form).await;
    assert!(reply.status == StatusCode::CONFLICT && reply.text.contains("It goes with your next issue once it can."));
    // With the daily email off, the next issue is read here, so the refusal says that instead.
    db::set_kindle(pool, "john", "john@kindle.com", true, false).await.unwrap();
    let reply = browser.post(&site, "/send", &form).await;
    assert!(reply.status == StatusCode::CONFLICT && reply.text.contains("It is in your next issue here once it can."));
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
    db::set_kindle(pool, "john", "john@kindle.com", true, true).await.unwrap();
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
    // The count is of every page, and a later page says which of them it shows.
    assert!(first.contains("<p>56 articles match.</p>"));
    assert!(second.contains("<p>56 articles match, showing 51–56.</p>"));
}

#[tokio::test]
async fn the_archive_says_how_many_articles_match() {
    let site = site(true).await;
    let pool = &site.pool;
    let sent = "2026-10-10T10:00:00+00:00";
    article(pool, "a1", "https://blog.example/1", "Blog", "Compilers today", "john", sent).await;
    article(pool, "a2", "https://blog.example/2", "Blog", "More compilers", "john", sent).await;
    article(pool, "a3", "https://blog.example/3", "Blog", "Sailing", "john", sent).await;
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;

    assert!(browser.get(&site, "/archive?q=compilers").await.text.contains("<p>2 articles match.</p>"));
    assert!(browser.get(&site, "/archive?q=sailing").await.text.contains("<p>1 article matches.</p>"));
    let none = browser.get(&site, "/archive?q=watches").await.text;
    // Only the existing empty text: no count line.
    assert!(none.contains(r#"<p class="empty">No articles match.</p>"#));
    assert_eq!(none.matches("articles match").count(), 1);
    assert!(!none.contains("article matches"));
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
    db::set_kindle(pool, "john", "john@kindle.com", true, true).await.unwrap();
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
    assert_eq!(browser.get(&site, "/reading").await.location, "/login");
    browser.sign_in(&site, "john").await;
    assert_eq!(browser.get(&site, "/learned").await.location, "/reading");
    let page = browser.get(&site, "/reading").await;
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
    let text = browser.get(&site, "/reading").await.text;
    assert!(text.contains("Last rewritten from your ratings on 12 October 2026"));
    assert!(text.contains("Finishes long &#60;b&#62;technical&#60;/b&#62; pieces.") && !text.contains("<b>technical"));
    assert!(text.contains("Rewritten from your ratings · in use") && !text.contains("Mary"));
    assert!(!text.contains("Restore this version"));

    // The reader's edit replaces it and is a version too; the pipeline's date is left alone.
    let edit = [("csrf", csrf.as_str()), ("notes", "  Skips market reports.\r\nLikes long pieces.  ")];
    assert_eq!(browser.post(&site, "/learned", &edit).await.location, "/reading?saved=notes");
    let now = db::learned(pool, "john").await.unwrap();
    assert_eq!(now.notes, "Skips market reports.\nLikes long pieces.");
    assert_eq!(now.learned_at.as_deref(), Some("2026-10-12T11:00:00+00:00"));
    let text = browser.get(&site, "/reading?saved=notes").await.text;
    assert!(text.contains("Edited by you · in use") && text.contains("Restore this version"));
    // Shown to read; the form to correct them is tucked away, not the page's main action.
    assert!(text.contains("<p class=\"notes\">Skips market reports.\nLikes long pieces.</p>"));
    let tucked = text.find("<summary>Correct these notes</summary>").unwrap();
    assert!(text.find("name=\"notes\"").unwrap() > tucked);
    let long = "x".repeat(2001);
    let too_long = [("csrf", csrf.as_str()), ("notes", long.as_str())];
    assert_eq!(browser.post(&site, "/learned", &too_long).await.location, "/reading?saved=long");

    // Back to an earlier version of their own; never to someone else's, never without the token.
    let ids: Vec<(i64, String)> =
        sqlx::query_as("SELECT id, user_id FROM profile_history ORDER BY id").fetch_all(pool).await.unwrap();
    let (mine, marys) = (ids[0].0.to_string(), ids[1].0.to_string());
    let theirs = [("csrf", csrf.as_str()), ("version", marys.as_str())];
    assert_eq!(browser.post(&site, "/learned/restore", &theirs).await.status, StatusCode::NOT_FOUND);
    let forged = [("csrf", "wrong"), ("version", mine.as_str())];
    assert_eq!(browser.post(&site, "/learned/restore", &forged).await.status, StatusCode::FORBIDDEN);
    let back = [("csrf", csrf.as_str()), ("version", mine.as_str())];
    assert_eq!(browser.post(&site, "/learned/restore", &back).await.location, "/reading?saved=restored");
    assert_eq!(db::learned(pool, "john").await.unwrap().notes, "Finishes long <b>technical</b> pieces.");
    assert_eq!(db::learned(pool, "mary").await.unwrap().notes, "");
}

#[tokio::test]
async fn an_article_in_the_archive_can_be_fetched_again() {
    let site = site(true).await;
    let pool = &site.pool;
    let sent = "2026-10-10T10:00:00+00:00";
    article(pool, "a1", "https://blog.example/1", "Blog", "Compilers today", "john", sent).await;
    article(pool, "m1", "https://blog.example/m", "Blog", "Mary's own", "mary", sent).await;
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;

    let page = browser.get(&site, "/archive").await;
    assert!(page.text.contains(r#"<form class="send apart" method="post" action="/archive/refresh">"#));
    assert!(page.text.contains("Fetch again") && !page.text.contains(r#"http-equiv="refresh""#));
    let csrf = page.csrf();
    let ask = [("csrf", csrf.as_str()), ("article", "a1"), ("back", "/archive?q=compilers")];
    assert_eq!(browser.post(&site, "/archive/refresh", &ask).await.location, "/archive?q=compilers#a-a1");
    // Asked twice while it waits: still one request.
    browser.post(&site, "/archive/refresh", &ask).await;
    let queued: Vec<(String, String, String)> =
        sqlx::query_as("SELECT user_id, article_id, status FROM refresh_requests").fetch_all(pool).await.unwrap();
    assert_eq!(queued, [("john".to_owned(), "a1".to_owned(), "queued".to_owned())]);

    // While it waits the button is gone and the page reloads itself.
    let waiting = browser.get(&site, "/archive").await.text;
    assert!(waiting.contains("Being fetched again") && !waiting.contains(">Fetch again<"));
    assert!(waiting.contains(r#"http-equiv="refresh""#));
    sqlx::query("UPDATE refresh_requests SET status = 'failed', error = 'HTTP 403', done_at = ?")
        .bind(sent)
        .execute(pool)
        .await
        .unwrap();
    let failed = browser.get(&site, "/archive").await.text;
    assert!(failed.contains("Could not be fetched again: HTTP 403") && failed.contains(">Fetch again<"));

    // Not another reader's article, and not from elsewhere.
    let other = [("csrf", csrf.as_str()), ("article", "m1")];
    assert_eq!(browser.post(&site, "/archive/refresh", &other).await.status, StatusCode::NOT_FOUND);
    let forged = [("csrf", "forged"), ("article", "a1")];
    assert_eq!(browser.post(&site, "/archive/refresh", &forged).await.status, StatusCode::FORBIDDEN);
}

#[tokio::test]
async fn a_rating_link_in_a_book_records_the_tap_without_a_sign_in() {
    use forenoon_web::feedback::{make, verify};
    let site = site(true).await;
    let pool = &site.pool;
    article(pool, "a1", "https://blog.example/1", "Blog", "A post", "john", "2026-10-10T10:00:00+00:00").await;
    let mut kindle = Browser::default();
    let later = 4_000_000_000;
    let link = |signal: &str| format!("/f/{}", make(FEEDBACK_SECRET, "john", "a1", signal, later));

    let page = kindle.get(&site, &link("r2")).await;
    assert!(page.status == StatusCode::OK && page.text.contains("Rated 2 of 5, not finished."), "{}", page.text);
    assert!(page.text.contains("<p class=\"small\">A post</p>"));
    assert!(kindle.cookie.is_none() && page.set_cookie.is_empty(), "no session for a tap");
    // The reasons for a low rating are offered; the rating counts without one.
    assert!(page.text.contains("Why? (optional)") && page.text.contains("Too thin, nothing in it"));
    assert!(!page.text.contains("More on this topic") && !page.text.contains("Tap all that apply"));
    // Every other choice is signed for the same reader, article and expiry.
    let other = page.text.split("href=\"/f/").nth(1).unwrap().split('"').next().unwrap();
    let claim = verify(FEEDBACK_SECRET, other, 0).unwrap();
    assert!(claim.user == "john" && claim.article == "a1" && claim.expires == later);
    assert_eq!(page.text.matches("href=\"/f/").count(), 5 + 10 + 1, "reasons, both rows and the report");

    let why = kindle.get(&site, &link("why-thin")).await.text;
    assert!(why.contains("Rated 2 of 5, not finished. Too thin, nothing in it."), "{why}");
    let high = kindle.get(&site, &link("f5")).await.text;
    assert!(high.contains("Rated 5 of 5, finished.") && high.contains("Tap all that apply."));
    assert!(high.contains("More from this source") && !high.contains("Too thin"));
    kindle.get(&site, &link("why-depth")).await;
    assert!(kindle.get(&site, &link("why-more-topic")).await.text.contains("More on this topic. Liked the depth."));
    // A fault in the delivery is reported apart from the rating, which it leaves as it was.
    let reported = kindle.get(&site, &link("problem")).await.text;
    assert!(reported.contains("Reported. Thank you.") && !reported.contains("Change it"));
    // Opened again (the Kindle's browser retries on its own), it is still one report.
    assert!(kindle.get(&site, &link("problem")).await.text.contains("Reported. Thank you."));

    let rating = db::get_rating(pool, "john", "a1").await.unwrap().unwrap();
    assert_eq!((rating.rating, rating.finished, rating.reason.as_str()), (Some(5), true, "more-topic,depth"));
    let events: Vec<(String, String)> =
        sqlx::query_as("SELECT signal, source FROM feedback_events ORDER BY id").fetch_all(pool).await.unwrap();
    let signals: Vec<&str> = events.iter().map(|(signal, _)| signal.as_str()).collect();
    assert_eq!(signals, ["r2", "why-thin", "f5", "why-depth", "why-more-topic", "problem"]);
    assert!(events.iter().all(|(_, source)| source == "kindle-footer"));
    let review: String = sqlx::query_scalar("SELECT problem FROM extraction_reviews").fetch_one(pool).await.unwrap();
    assert_eq!(review, "reader: broken or incomplete");

    // Expired, forged and malformed links change nothing.
    let expired = kindle.get(&site, &format!("/f/{}", make(FEEDBACK_SECRET, "john", "a1", "r1", 1000))).await;
    assert!(expired.status == StatusCode::BAD_REQUEST && expired.text.contains("This link has expired."));
    let forged = kindle
        .get(&site, &format!("/f/{}", make(b"not the install's secret, but long", "john", "a1", "r1", later)))
        .await;
    assert!(forged.status == StatusCode::BAD_REQUEST && forged.text.contains("Link not valid."));
    assert_eq!(kindle.get(&site, "/f/garbage").await.status, StatusCode::BAD_REQUEST);
    assert_eq!(db::get_rating(pool, "john", "a1").await.unwrap().unwrap().rating, Some(5));

    // Taps are limited per client.
    let mut codes = Vec::new();
    for _ in 0..30 {
        codes.push(kindle.get(&site, "/f/garbage").await.status);
    }
    assert_eq!(codes.last(), Some(&StatusCode::TOO_MANY_REQUESTS));
}

#[tokio::test]
async fn over_https_the_cookie_is_host_only_and_browsers_are_told_to_stay() {
    let site = site_with(true, true).await;
    let form = format!("email=john%40example.com&password={}", PASSWORD.replace(' ', "+"));
    let request = Request::post("/login")
        .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
        .header(header::HOST, "forenoon.example.com")
        .body(Body::from(form))
        .unwrap();
    let response = site.router.clone().oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::SEE_OTHER);
    let cookie = response.headers()[header::SET_COOKIE].to_str().unwrap();
    assert!(cookie.starts_with("__Host-forenoon_session=") && cookie.contains("Secure") && cookie.contains("Path=/"));
    assert!(!cookie.contains("Domain"), "{cookie}");
    assert_eq!(response.headers()[header::STRICT_TRANSPORT_SECURITY], "max-age=31536000");
    let plain = site_with(true, false).await;
    let response = plain.router.clone().oneshot(Request::get("/login").body(Body::empty()).unwrap()).await.unwrap();
    assert!(!response.headers().contains_key(header::STRICT_TRANSPORT_SECURITY));
}

#[tokio::test]
async fn with_secure_cookies_another_address_is_sent_to_the_https_one() {
    let site = site_with(true, true).await;
    let get = |host: &str, path: &str| {
        let request = Request::get(path).header(header::HOST, host).body(Body::empty()).unwrap();
        site.router.clone().oneshot(request)
    };
    // The LAN address, where a sign-in could never hold: the same page at the https address.
    let response = get("192.168.1.5:8787", "/archive?q=rust").await.unwrap();
    assert_eq!(response.status(), StatusCode::TEMPORARY_REDIRECT);
    assert_eq!(response.headers()[header::LOCATION], "https://forenoon.example.com/archive?q=rust");
    let form = format!("email=john%40example.com&password={}", PASSWORD.replace(' ', "+"));
    let post = Request::post("/login")
        .header(header::HOST, "nas:8787")
        .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
        .body(Body::from(form))
        .unwrap();
    let response = site.router.clone().oneshot(post).await.unwrap();
    assert_eq!(response.status(), StatusCode::TEMPORARY_REDIRECT);
    assert!(!response.headers().contains_key(header::SET_COOKIE), "no sign-in where it cannot hold");
    // Addressed to the https host (through the tunnel, any letter case): served as usual.
    assert_eq!(get("Forenoon.Example.com", "/login").await.unwrap().status(), StatusCode::OK);
    // From a trusted proxy, whatever host it names (nginx names its upstream): served, never
    // sent round again. The same request from the LAN is sent on.
    let from = |peer: [u8; 4]| {
        let mut request = Request::get("/login").header(header::HOST, "127.0.0.1:8081").body(Body::empty()).unwrap();
        request.extensions_mut().insert(axum::extract::ConnectInfo(std::net::SocketAddr::from((peer, 50000))));
        site.router.clone().oneshot(request)
    };
    assert_eq!(from([127, 0, 0, 1]).await.unwrap().status(), StatusCode::OK);
    assert_eq!(from([192, 168, 1, 5]).await.unwrap().status(), StatusCode::TEMPORARY_REDIRECT);
    // The health check answers wherever it is asked, as the deploy script asks it inside the container.
    assert_eq!(get("localhost", "/healthz").await.unwrap().status(), StatusCode::OK);
    // Plain cookies: the LAN address is the address, and nothing is redirected.
    let plain = site_with(true, false).await;
    let request = Request::get("/login").header(header::HOST, "192.168.1.5:8787").body(Body::empty()).unwrap();
    assert_eq!(plain.router.clone().oneshot(request).await.unwrap().status(), StatusCode::OK);
}

#[tokio::test]
async fn a_rating_link_before_the_pipeline_has_run_is_not_an_error() {
    let site = site(false).await;
    let link = format!("/f/{}", forenoon_web::feedback::make(FEEDBACK_SECRET, "john", "a1", "f4", 4_000_000_000));
    assert_eq!(Browser::default().get(&site, &link).await.status, StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn pages_are_compressed_and_the_form_token_never_reads_the_same_twice() {
    let site = site(true).await;
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    let first = browser.get(&site, "/account").await;
    let second = browser.get(&site, "/account").await;
    // Masked afresh on every page, so compression cannot reveal it; each one still works.
    assert_ne!(first.csrf(), second.csrf());
    let profile =
        |token: &str| [("csrf", token.to_owned()), ("name", "John".to_owned()), ("timezone", "UTC".to_owned())];
    for token in [first.csrf(), second.csrf()] {
        let form = profile(&token);
        let form: Vec<(&str, &str)> = form.iter().map(|(k, v)| (*k, v.as_str())).collect();
        let reply = browser.post(&site, "/account/profile", &form).await;
        assert_eq!(reply.status, StatusCode::SEE_OTHER, "{}", reply.text);
    }
    // Half a token (the mask alone), a token of the wrong length and junk are refused.
    let half = first.csrf()[..first.csrf().len() / 2].to_owned();
    for token in [half.as_str(), "", "zz", &"ab".repeat(64)] {
        let form = profile(token);
        let form: Vec<(&str, &str)> = form.iter().map(|(k, v)| (*k, v.as_str())).collect();
        assert_eq!(browser.post(&site, "/account/profile", &form).await.status, StatusCode::FORBIDDEN, "{token}");
    }

    let request = Request::get("/login").header(header::ACCEPT_ENCODING, "gzip").body(Body::empty()).unwrap();
    let response = site.router.clone().oneshot(request).await.unwrap();
    assert_eq!(response.headers()[header::CONTENT_ENCODING], "gzip");
    assert_eq!(response.headers()["cross-origin-opener-policy"], "same-origin");
    assert_eq!(response.headers()["cross-origin-resource-policy"], "same-origin");
    let csp = response.headers()[header::CONTENT_SECURITY_POLICY].to_str().unwrap();
    assert!(csp.ends_with("frame-ancestors 'none'"), "{csp}");
}

#[tokio::test]
async fn an_address_that_is_not_a_page_says_so_signed_in_or_not() {
    let site = site(true).await;
    let mut browser = Browser::default();
    for _ in 0..2 {
        let reply = browser.get(&site, "/no-such-page").await;
        assert_eq!(reply.status, StatusCode::NOT_FOUND);
        assert!(reply.text.contains("There is no page at this address.") && reply.text.contains(r#"<a href="/">"#));
        assert!(reply.csp.contains("default-src 'none'"));
        browser.sign_in(&site, "john").await;
    }
}

#[tokio::test]
async fn icons_and_the_manifest_need_no_session() {
    let site = site(false).await;
    for (path, media_type) in [
        ("/favicon.ico", "image/x-icon"),
        ("/icon.svg", "image/svg+xml"),
        ("/apple-touch-icon.png", "image/png"),
        ("/icon-192.png", "image/png"),
        ("/icon-512.png", "image/png"),
        ("/icon-maskable-512.png", "image/png"),
        ("/og.png", "image/png"),
        // The address the pages name the card by, versioned: the same answer.
        (forenoon_web::icons::SHARE_CARD.as_str(), "image/png"),
        ("/manifest.webmanifest", "application/manifest+json"),
    ] {
        let response = site.router.clone().oneshot(Request::get(path).body(Body::empty()).unwrap()).await.unwrap();
        let header = |name| response.headers().get(name).unwrap().to_str().unwrap().to_owned();
        assert_eq!(response.status(), StatusCode::OK, "{path}");
        assert_eq!(header(header::CONTENT_TYPE), media_type, "{path}");
        assert_eq!(header(header::CACHE_CONTROL), "public, max-age=86400", "{path}");
        // The share card may be shown on another site's page; nothing else may.
        let policy = if path.starts_with("/og.png") { "cross-origin" } else { "same-origin" };
        assert_eq!(header(header::HeaderName::from_static("cross-origin-resource-policy")), policy, "{path}");
    }
    // Every icon the manifest names is served, and the page lets the browser load the manifest.
    let manifest = Browser::default().get(&site, "/manifest.webmanifest").await.text;
    let manifest: serde_json::Value = serde_json::from_str(&manifest).unwrap();
    for icon in manifest["icons"].as_array().unwrap() {
        let reply = Browser::default().get(&site, icon["src"].as_str().unwrap()).await;
        assert_eq!(reply.status, StatusCode::OK, "{icon}");
    }
    let page = Browser::default().get(&site, "/login").await;
    assert!(page.csp.contains("manifest-src 'self'"), "{}", page.csp);
    for link in [
        r#"href="/favicon.ico""#,
        r#"href="/icon.svg""#,
        r#"href="/apple-touch-icon.png""#,
        r#"href="/manifest.webmanifest""#,
    ] {
        assert!(page.text.contains(link), "{link}");
    }
}

#[tokio::test]
async fn a_shared_link_previews_with_the_sites_card() {
    let site = site(false).await;
    // A chat app fetching a link's preview has no session, so it lands on the sign-in page.
    let home = Browser::default().get(&site, "/").await;
    assert_eq!(home.location, "/login");
    let page = Browser::default().get(&site, "/login").await;
    for tag in [
        r#"<meta property="og:title" content="Forenoon: the AI scrolls, you read">"#,
        r#"<meta property="og:url" content="http://nas:8787/login">"#,
        r#"<meta name="twitter:card" content="summary_large_image">"#,
    ] {
        assert!(page.text.contains(tag), "{tag}");
    }
    // The card by its versioned address, which serves the same picture as the plain one.
    let (_, card) = page.text.split_once(r#"<meta property="og:image" content="http://nas:8787"#).unwrap();
    let card = card.split('"').next().unwrap();
    assert!(card.starts_with("/og.png?v="), "{card}");
    let versioned = Browser::default().get(&site, card).await;
    assert_eq!(versioned.status, StatusCode::OK);
    assert_eq!(versioned.text, Browser::default().get(&site, "/og.png").await.text);
}

#[tokio::test]
async fn a_demo_host_shows_one_persona_to_anyone_read_only_and_without_a_session() {
    const DEMO: &str =
        "john.forenoon.test=john,mary.forenoon.test=mary,nobody.forenoon.test=nobody,port.forenoon.test:8081=mary";
    let site = site_full(true, false, DEMO, None).await;
    let pool = &site.pool;
    let sent = "2026-10-10T10:00:00+00:00";
    article(pool, "a1", "https://blog.example/1", "Blog", "Johns article", "john", sent).await;
    article(pool, "a2", "https://blog.example/2", "Blog", "Marys article", "mary", sent).await;
    sqlx::query("UPDATE users SET interests = 'Compilers and old maps' WHERE id = 'john'").execute(pool).await.unwrap();
    // A feed of Mary's own, shared with nobody: an admin sees it on the Sources page.
    sqlx::query(
        "INSERT INTO sources (name, kind, url, created_at, added_by, shared) VALUES ('Marys private feed', 'rss', 'https://m.example/feed', '2026-10-01T00:00:00+00:00', 'mary', 0)",
    )
    .execute(pool)
    .await
    .unwrap();
    let thumbs = site.folder.path().join("thumbs");
    std::fs::create_dir_all(&thumbs).unwrap();
    std::fs::write(thumbs.join("a1.jpg"), b"\xff\xd8\xff jpeg bytes").unwrap();
    // Anyone, with no cookie.
    let get = async |host: &str, path: &str| {
        let request = Request::get(path).header(header::HOST, host).body(Body::empty()).unwrap();
        let response = site.router.clone().oneshot(request).await.unwrap();
        let text = |name: header::HeaderName| {
            response.headers().get(name).map(|value| value.to_str().unwrap().to_owned()).unwrap_or_default()
        };
        let (status, cookie, cache) = (response.status(), text(header::SET_COOKIE), text(header::CACHE_CONTROL));
        let body = response.into_body().collect().await.unwrap().to_bytes();
        (status, cookie, cache, String::from_utf8_lossy(&body).into_owned())
    };

    // John's newsstand, to anyone: no sign-in, no session started, and a page a cache may keep.
    let (status, cookie, cache, page) = get("john.forenoon.test", "/").await;
    assert_eq!(status, StatusCode::OK);
    assert!(cookie.is_empty(), "no session for a visitor: {cookie}");
    assert_eq!(cache, "public, max-age=300");
    assert!(page.contains("Johns article") && !page.contains("Marys article"));
    // The personas to switch between, the persona's interests, and the buttons shown but inert.
    assert!(page.contains(r#"<span class="me">Reading as</span>"#), "{page}");
    assert!(page.contains(r#"href="//mary.forenoon.test/""#) && page.contains(r#"aria-current="true">John</a>"#));
    assert!(page.contains("Compilers and old maps") && page.contains("A demo, read only"));
    assert!(!page.contains(r#"method="post""#) && !page.contains(r#"name="csrf""#), "{page}");
    assert!(page.contains(r#"<button type="button" class="inert">1</button>"#));
    assert!(page.contains(r#"class="kindle inert""#) && page.contains(r#"<img src="/thumb/a1""#));
    assert!(!page.contains("/account") && !page.contains("/settings") && !page.contains("Sign out"));
    // Mary's host, however the browser spells it, shows hers; so does one configured with its port.
    let (status, _, _, page) = get("Mary.Forenoon.test:8081", "/").await;
    assert!(status == StatusCode::OK && page.contains("Marys article") && !page.contains("Johns article"));
    assert!(get("port.forenoon.test:8081", "/").await.3.contains("Marys article"));
    // A host that is not quite one of them is the install's own: a sign-in is needed.
    assert!(get("john.forenoon.test.", "/").await.0.is_redirection());

    // The Archive's filters are links, so they work; the Sources page is the list alone, and
    // though John is an admin, the persona is a reader: Mary's private feed is not on it.
    let (status, cookie, _, page) = get("john.forenoon.test", "/archive?q=Johns").await;
    assert!(status == StatusCode::OK && page.contains("Johns article") && !page.contains(r#"method="post""#));
    assert!(!page.contains("Add a link") && !page.contains("Fetch again") && cookie.is_empty());
    let (status, cookie, _, page) = get("john.forenoon.test", "/sources").await;
    assert!(status == StatusCode::OK && page.contains("Hacker News") && cookie.is_empty(), "{page}");
    assert!(!page.contains("Marys private feed"), "{page}");
    assert!(
        !page.contains(r#"method="post""#) && !page.contains("Subscriber sessions") && !page.contains("Add a feed")
    );
    assert!(page.contains(r#"<div class="row"#) && !page.contains(r#"<details class="row"#), "{page}");
    assert!(!page.contains(r#"<details class="add"#) && !page.contains("Open a feed to change it"));

    // A picture, on the host whose persona it was delivered to; kept by a cache too.
    let (status, cookie, cache, body) = get("john.forenoon.test", "/thumb/a1").await;
    assert!(
        status == StatusCode::OK && body.contains("jpeg bytes") && cache.starts_with("public") && cookie.is_empty()
    );
    let head = Request::head("/").header(header::HOST, "john.forenoon.test").body(Body::empty()).unwrap();
    assert_eq!(site.router.clone().oneshot(head).await.unwrap().status(), StatusCode::OK);
    assert_eq!(get("mary.forenoon.test", "/thumb/a1").await.0, StatusCode::NOT_FOUND);
    // Icons and the health check answer there as anywhere.
    assert_eq!(get("john.forenoon.test", "/icon.svg").await.0, StatusCode::OK);
    assert_eq!(get("john.forenoon.test", "/healthz").await.0, StatusCode::OK);

    // Nothing else exists on a demo host, the sign-in page included; John is an admin, and the
    // demo shows none of it.
    for path in ["/login", "/forgot", "/account", "/settings", "/accounts", "/problems", "/reading", "/learned", "/f/x"]
    {
        assert_eq!(get("john.forenoon.test", path).await.0, StatusCode::NOT_FOUND, "{path}");
    }
    // That answer is dressed like every other.
    let request = Request::get("/login").header(header::HOST, "john.forenoon.test").body(Body::empty()).unwrap();
    let response = site.router.clone().oneshot(request).await.unwrap();
    assert!(response.headers().contains_key(header::CONTENT_SECURITY_POLICY));
    let post = Request::post("/rate")
        .header(header::HOST, "john.forenoon.test")
        .header(header::CONTENT_TYPE, "application/x-www-form-urlencoded")
        .body(Body::from("csrf=x&article=a1&rating=5"))
        .unwrap();
    assert_eq!(site.router.clone().oneshot(post).await.unwrap().status(), StatusCode::NOT_FOUND);
    let rated: i64 = sqlx::query_scalar("SELECT COUNT(*) FROM feedback_events").fetch_one(pool).await.unwrap();
    assert_eq!(rated, 0);
    // A host whose reader is not an account yet has nothing to show, and is not an error.
    assert_eq!(get("nobody.forenoon.test", "/").await.0, StatusCode::NOT_FOUND);

    // The install's own address is as it was: a sign-in is needed, and the pages post.
    assert!(Browser::default().get(&site, "/").await.status.is_redirection());
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    let page = browser.get(&site, "/").await;
    assert!(
        page.text.contains(r#"method="post""#) && page.text.contains("/account") && !page.text.contains("Reading as")
    );
    assert!(browser.get(&site, "/sources").await.text.contains("Marys private feed"));
    // John's own cookie on a demo host changes nothing: the persona is shown, no cookie is set.
    let mut request = Request::get("/").header(header::HOST, "mary.forenoon.test").body(Body::empty()).unwrap();
    request.headers_mut().insert(header::COOKIE, browser.cookie.clone().unwrap().parse().unwrap());
    let response = site.router.clone().oneshot(request).await.unwrap();
    assert!(!response.headers().contains_key(header::SET_COOKIE));
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let text = String::from_utf8_lossy(&body);
    assert!(text.contains("Marys article") && !text.contains("Johns article") && !text.contains(r#"method="post""#));

    // Over https, a demo host is an address of its own, not sent to the public one.
    let secure = site_full(true, true, DEMO, None).await;
    let request = Request::get("/").header(header::HOST, "john.forenoon.test").body(Body::empty()).unwrap();
    assert_eq!(secure.router.clone().oneshot(request).await.unwrap().status(), StatusCode::OK);
}

#[tokio::test]
async fn a_demo_hosts_wordmark_leads_back_to_the_landing_page() {
    const DEMO: &str = "john.forenoon.test=john";
    let page = async |site: &Site, host: &str| {
        let request = Request::get("/").header(header::HOST, host).body(Body::empty()).unwrap();
        let response = site.router.clone().oneshot(request).await.unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        String::from_utf8_lossy(&body).into_owned()
    };
    // A visitor came from the landing page: the wordmark goes back there, scheme-relative.
    let site = site_full(true, false, DEMO, Some("forenoon.test")).await;
    assert!(page(&site, "john.forenoon.test").await.contains(r#"<a href="//forenoon.test/">Forenoon</a>"#));
    // On the install's own pages it is the newsstand, as ever.
    let mut browser = Browser::default();
    browser.sign_in(&site, "john").await;
    assert!(browser.get(&site, "/archive").await.text.contains(r#"<a href="/">Forenoon</a>"#));
    // A demo with no landing page has nowhere else to go.
    let site = site_full(true, false, DEMO, None).await;
    assert!(page(&site, "john.forenoon.test").await.contains(r#"<a href="/">Forenoon</a>"#));
}

#[tokio::test]
async fn demo_personas_and_their_feeds_are_made_on_start_and_only_once() {
    // Mary is a reader made by hand, named as a persona too; she has stopped following Lobsters.
    const DEMO: &str = "mara.forenoon.test=mara,theo.forenoon.test=theo,june.forenoon.test=june,m.forenoon.test=mary,x.forenoon.test=nobody";
    let site = site_full(true, false, DEMO, None).await;
    let pool = &site.pool;
    let count = async |sql: &str| sqlx::query_scalar::<_, i64>(sql).fetch_one(pool).await.unwrap();
    sqlx::query(
        "DELETE FROM follows WHERE user_id = 'mary' AND source_id = (SELECT id FROM sources WHERE name = 'Lobsters')",
    )
    .execute(pool)
    .await
    .unwrap();
    let marys = count("SELECT COUNT(*) FROM follows WHERE user_id = 'mary'").await;
    let personas = forenoon_web::demo::parse(DEMO).unwrap();
    let made = forenoon_web::demo::seed(pool, &personas, "America/New_York").await.unwrap();
    // Three readers, the feeds, and a word about the host whose reader the file does not know.
    assert_eq!(made.len(), 5, "{made:?}");
    assert_eq!(
        count("SELECT COUNT(*) FROM sources WHERE kind = 'rss' AND shared = 1 AND check_state = 'pending'").await,
        24
    );
    let shared = count("SELECT COUNT(*) FROM sources WHERE shared = 1").await;
    assert_eq!(count("SELECT COUNT(*) FROM follows WHERE user_id = 'june'").await, shared);
    // Mary follows the new feeds and is otherwise left as she was; John, no persona, gets nothing.
    assert_eq!(count("SELECT COUNT(*) FROM follows WHERE user_id = 'mary'").await, marys + 24);
    assert_eq!(count("SELECT COUNT(*) FROM follows WHERE user_id = 'john'").await, 2);
    let theo = auth::find_user(pool, "theo").await.unwrap().unwrap();
    assert!(theo.interests.contains("housing") && theo.kindle_addresses.is_empty() && theo.role == "reader");
    assert_eq!(theo.timezone, "America/New_York");
    // Again: nothing to make, and an unfollow and a rewrite made since stand.
    sqlx::query(
        "DELETE FROM follows WHERE user_id = 'june' AND source_id = (SELECT id FROM sources WHERE name = 'BBC News')",
    )
    .execute(pool)
    .await
    .unwrap();
    sqlx::query("UPDATE users SET interests = 'Only trains.' WHERE id = 'theo'").execute(pool).await.unwrap();
    let again = forenoon_web::demo::seed(pool, &personas, "UTC").await.unwrap();
    assert_eq!(again.len(), 1, "{again:?}");
    assert_eq!(count("SELECT COUNT(*) FROM follows WHERE user_id = 'june'").await, shared - 1);
    assert_eq!(auth::find_user(pool, "theo").await.unwrap().unwrap().interests, "Only trains.");
    // The persona's Sources page lists them on its host.
    let request = Request::get("/sources").header(header::HOST, "mara.forenoon.test").body(Body::empty()).unwrap();
    let response = site.router.clone().oneshot(request).await.unwrap();
    let body = response.into_body().collect().await.unwrap().to_bytes();
    assert!(String::from_utf8_lossy(&body).contains("Quanta Magazine"));
}

#[tokio::test]
async fn the_landing_page_is_served_at_its_own_host_and_nothing_else_is() {
    let site = site_full(true, false, "", Some("forenoon.test")).await;
    let get = async |host: &str, path: &str| {
        let request = Request::get(path).header(header::HOST, host).body(Body::empty()).unwrap();
        let response = site.router.clone().oneshot(request).await.unwrap();
        let text = |name: &str| {
            response.headers().get(name).map(|value| value.to_str().unwrap().to_owned()).unwrap_or_default()
        };
        let headers = (text("content-type"), text("cache-control"), text("x-robots-tag"), text("set-cookie"));
        let has_csp = response.headers().contains_key(header::CONTENT_SECURITY_POLICY);
        let status = response.status();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        (status, headers, has_csp, String::from_utf8_lossy(&body).into_owned())
    };
    // The page, to anyone, meant to be found and kept by a cache; dressed like every other answer.
    let (status, (kind, cache, robots, cookie), csp, page) = get("Forenoon.Test:443", "/").await;
    assert_eq!(status, StatusCode::OK);
    assert!(kind.starts_with("text/html") && cache == "public, max-age=300" && robots.is_empty() && cookie.is_empty());
    assert!(csp && page.contains("A morning paper edited for one reader"), "{page}");
    // What a shared link's preview needs, X's card type and the picture's size among it.
    for tag in [
        r#"<meta property="og:site_name" content="Forenoon">"#,
        r#"<meta property="og:image:width" content="1200">"#,
        r#"<meta property="og:image:height" content="630">"#,
        r#"<meta property="og:image:alt" content="#,
        r#"<meta name="twitter:card" content="summary_large_image">"#,
    ] {
        assert!(page.contains(tag), "{tag}");
    }
    // Its pictures, and the icons it names.
    let (status, (kind, cache, _, _), _, _) = get("forenoon.test", "/images/kindle-cover.webp").await;
    assert!(status == StatusCode::OK && kind == "image/webp" && cache.starts_with("public"));
    assert_eq!(get("forenoon.test", "/favicon.ico").await.0, StatusCode::OK);
    assert_eq!(get("forenoon.test", &forenoon_web::icons::SHARE_CARD).await.0, StatusCode::OK);
    assert_eq!(get("forenoon.test", "/healthz").await.0, StatusCode::OK);
    // Nothing else on that host, the sign-in page included.
    for path in ["/login", "/archive", "/images/nope.webp", "/thumb/a1", "/f/x"] {
        let (status, _, csp, _) = get("forenoon.test", path).await;
        assert!(status == StatusCode::NOT_FOUND && csp, "{path}");
    }
    let post = Request::post("/").header(header::HOST, "forenoon.test").body(Body::empty()).unwrap();
    assert_eq!(site.router.clone().oneshot(post).await.unwrap().status(), StatusCode::NOT_FOUND);
    // The install's own address is as it was.
    assert!(Browser::default().get(&site, "/").await.status.is_redirection());
    // Over https, the landing host is an address of its own, not sent to the public one.
    let secure = site_full(true, true, "", Some("forenoon.test")).await;
    let request = Request::get("/").header(header::HOST, "forenoon.test").body(Body::empty()).unwrap();
    assert_eq!(secure.router.clone().oneshot(request).await.unwrap().status(), StatusCode::OK);
}

#[tokio::test]
async fn the_landing_page_and_the_demo_hosts_pages_are_meant_to_be_found() {
    let site =
        site_full(true, false, "john.forenoon.test=john,nobody.forenoon.test=nobody", Some("forenoon.test")).await;
    let thumbs = site.folder.path().join("thumbs");
    std::fs::create_dir_all(&thumbs).unwrap();
    article(&site.pool, "a1", "https://blog.example/1", "Blog", "Johns article", "john", "2026-10-10T10:00:00+00:00")
        .await;
    std::fs::write(thumbs.join("a1.jpg"), b"\xff\xd8\xff jpeg bytes").unwrap();
    let get = async |host: &str, path: &str| {
        let request = Request::get(path).header(header::HOST, host).body(Body::empty()).unwrap();
        let response = site.router.clone().oneshot(request).await.unwrap();
        let text = |name: &str| {
            response.headers().get(name).map(|value| value.to_str().unwrap().to_owned()).unwrap_or_default()
        };
        let headers = (text("content-type"), text("x-robots-tag"));
        let status = response.status();
        let body = response.into_body().collect().await.unwrap().to_bytes();
        (status, headers, String::from_utf8_lossy(&body).into_owned())
    };

    // The sitemap, on the landing host only, lists the page and the demo hosts' pages.
    let (status, (kind, robots), sitemap) = get("forenoon.test", "/sitemap.xml").await;
    assert!(status == StatusCode::OK && kind == "application/xml" && robots.is_empty(), "{sitemap}");
    for page in ["https://forenoon.test/", "https://john.forenoon.test/", "https://john.forenoon.test/sources"] {
        assert!(sitemap.contains(&format!("<loc>{page}</loc>")), "{page}");
    }
    assert_eq!(get("john.forenoon.test", "/sitemap.xml").await.0, StatusCode::NOT_FOUND);
    // Every one of those hosts names it from its robots.txt; the install's own host has none.
    for host in ["forenoon.test", "John.Forenoon.test:443"] {
        let (status, (kind, _), text) = get(host, "/robots.txt").await;
        assert!(status == StatusCode::OK && kind.starts_with("text/plain"), "{host}");
        assert!(text.contains("Sitemap: https://forenoon.test/sitemap.xml") && text.contains("Disallow: /thumb/"));
    }
    assert_eq!(get("nas:8787", "/robots.txt").await.0, StatusCode::NOT_FOUND);

    // A persona's pages may be indexed; its thumbnails, a page that is not there, and the
    // install's own pages may not.
    for path in ["/", "/archive", "/sources"] {
        let (status, (_, robots), _) = get("john.forenoon.test", path).await;
        assert!(status == StatusCode::OK && robots.is_empty(), "{path}");
    }
    let (status, (_, robots), _) = get("john.forenoon.test", "/thumb/a1").await;
    assert!(status == StatusCode::OK && robots == "noindex");
    assert_eq!(get("nobody.forenoon.test", "/").await.1.1, "noindex");
    assert_eq!(get("john.forenoon.test", "/login").await.1.1, "noindex");
    assert_eq!(get("nas:8787", "/login").await.1.1, "noindex");
}
