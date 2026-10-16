//! The database: opening it, the account commands, and the newsstand's queries.
//!
//! One SQLite file is shared with the Python pipeline. This app owns `users` and the session
//! table. The pipeline owns everything else (see `pipeline-schema.sql`); of its tables this
//! app writes only `ratings` and `feedback_events`, with the same rules as `app/store.py`.

use std::{path::Path, str::FromStr, time::Duration};

use chrono::Utc;
use sqlx::{
    SqlitePool,
    sqlite::{SqliteConnectOptions, SqliteJournalMode, SqlitePoolOptions},
};

use crate::{
    hex,
    ratings::{self, Signal},
};

pub async fn open(path: &Path) -> Result<SqlitePool, sqlx::Error> {
    let options = SqliteConnectOptions::from_str("sqlite:")?
        .filename(path)
        .create_if_missing(true)
        .journal_mode(SqliteJournalMode::Wal)
        .busy_timeout(Duration::from_secs(30));
    let pool = SqlitePoolOptions::new().max_connections(4).connect_with(options).await?;
    sqlx::migrate!("./migrations").run(&pool).await?;
    Ok(pool)
}

/// The same form the pipeline writes, so timestamps from both sort together as text.
pub fn now_iso() -> String {
    Utc::now().format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
}

/// Whether the pipeline has created its tables yet; false on an install that has never run.
pub async fn pipeline_ready(pool: &SqlitePool) -> Result<bool, sqlx::Error> {
    let found = sqlx::query_scalar!("SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'deliveries'")
        .fetch_one(pool)
        .await?;
    Ok(found > 0)
}

pub struct Usage {
    pub tokens: i64,
    pub cost: f64,
    /// Calls to a model whose price the pipeline does not know: not in `cost`.
    pub unpriced: i64,
}

/// The model's tokens (cached ones too) and their cost since `start`, as the pipeline logged them.
pub async fn usage_since(pool: &SqlitePool, start: &str) -> Result<Usage, sqlx::Error> {
    sqlx::query_as!(
        Usage,
        r#"SELECT COALESCE(SUM(input_tokens + cache_write_tokens + cache_read_tokens + output_tokens), 0) AS "tokens!: i64",
           COALESCE(SUM(cost), 0.0) AS "cost!: f64",
           COUNT(*) FILTER (WHERE cost IS NULL AND status != 'error') AS "unpriced!: i64"
           FROM llm_calls WHERE created_at >= ?"#,
        start
    )
    .fetch_one(pool)
    .await
}

// accounts

pub async fn user_count(pool: &SqlitePool) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar!("SELECT COUNT(*) FROM users").fetch_one(pool).await
}

pub async fn add_user(
    pool: &SqlitePool,
    id: &str,
    email: &str,
    name: &str,
    password_hash: &str,
    role: &str,
    timezone: &str,
) -> Result<(), sqlx::Error> {
    let now = now_iso();
    sqlx::query!(
        "INSERT INTO users (id, email, name, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        id,
        email,
        name,
        password_hash,
        role,
        timezone,
        now
    )
    .execute(pool)
    .await?;
    Ok(())
}

/// An account for a reader an admin has invited: no password until they join through the
/// emailed link (see `join`). `invited_at` marks it until then.
pub async fn invite_user(
    pool: &SqlitePool,
    id: &str,
    email: &str,
    name: &str,
    role: &str,
    timezone: &str,
) -> Result<(), sqlx::Error> {
    let now = now_iso();
    sqlx::query!(
        "INSERT INTO users (id, email, name, password_hash, role, timezone, created_at, invited_at)
         VALUES (?, ?, ?, '', ?, ?, ?, ?)",
        id,
        email,
        name,
        role,
        timezone,
        now,
        now
    )
    .execute(pool)
    .await?;
    Ok(())
}

/// The invited reader has chosen a password: the account is theirs from now on, in one
/// transaction with its link used up and the household's shared sources followed. The address
/// counts as confirmed, since the link reached it. False when the account is not an open invite.
pub async fn join(
    pool: &SqlitePool,
    id: &str,
    name: &str,
    password_hash: &str,
    timezone: &str,
    kindle_addresses: &str,
) -> Result<bool, sqlx::Error> {
    let now = now_iso();
    let mut tx = pool.begin().await?;
    let done = sqlx::query!(
        "UPDATE users SET name = ?, password_hash = ?, timezone = ?, kindle_addresses = ?, email_verified_at = ?,
         invited_at = NULL WHERE id = ? AND invited_at IS NOT NULL",
        name,
        password_hash,
        timezone,
        kindle_addresses,
        now,
        id
    )
    .execute(&mut *tx)
    .await?;
    if done.rows_affected() == 0 {
        return Ok(false);
    }
    sqlx::query!("DELETE FROM email_tokens WHERE user_id = ? AND purpose = ?", id, INVITE).execute(&mut *tx).await?;
    follow_shared_with(&mut tx, id).await?;
    tx.commit().await?;
    Ok(true)
}

/// Take back an invitation: the account goes, with its link. False when it is not an open invite.
pub async fn revoke_invite(pool: &SqlitePool, id: &str) -> Result<bool, sqlx::Error> {
    let mut tx = pool.begin().await?;
    let done = sqlx::query!("DELETE FROM users WHERE id = ? AND invited_at IS NOT NULL", id).execute(&mut *tx).await?;
    if done.rows_affected() == 0 {
        return Ok(false);
    }
    sqlx::query!("DELETE FROM email_tokens WHERE user_id = ?", id).execute(&mut *tx).await?;
    sqlx::query!("DELETE FROM follows WHERE user_id = ?", id).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(true)
}

/// Follow every source shared with the household, as a new account does so that its first
/// issue has something in it. Made for the reader, not chosen: it holds no feed in place.
pub async fn follow_shared(pool: &SqlitePool, user: &str) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    follow_shared_with(&mut tx, user).await?;
    tx.commit().await
}

async fn follow_shared_with(tx: &mut sqlx::Transaction<'_, sqlx::Sqlite>, user: &str) -> Result<(), sqlx::Error> {
    let now = now_iso();
    sqlx::query!(
        "INSERT OR IGNORE INTO follows (user_id, source_id, pinned, created_at, chosen)
         SELECT ?, id, 0, ?, 0 FROM sources WHERE shared = 1",
        user,
        now
    )
    .execute(&mut **tx)
    .await?;
    Ok(())
}

/// Whether feeds are followed for this reader when they are shared with the household.
pub async fn follows_new(pool: &SqlitePool, user: &str) -> Result<bool, sqlx::Error> {
    let on = sqlx::query_scalar!(r#"SELECT follow_new AS "on: bool" FROM users WHERE id = ?"#, user)
        .fetch_optional(pool)
        .await?;
    Ok(on.unwrap_or(true))
}

pub async fn set_follows_new(pool: &SqlitePool, user: &str, on: bool) -> Result<(), sqlx::Error> {
    sqlx::query!("UPDATE users SET follow_new = ? WHERE id = ?", on, user).execute(pool).await?;
    Ok(())
}

/// True when the account exists. Changing the hash signs out every session made with the old one.
pub async fn set_password(pool: &SqlitePool, id: &str, password_hash: &str) -> Result<bool, sqlx::Error> {
    let done = sqlx::query!("UPDATE users SET password_hash = ? WHERE id = ?", password_hash, id).execute(pool).await?;
    Ok(done.rows_affected() > 0)
}

pub async fn set_profile(pool: &SqlitePool, id: &str, name: &str, timezone: &str) -> Result<(), sqlx::Error> {
    sqlx::query!("UPDATE users SET name = ?, timezone = ? WHERE id = ?", name, timezone, id).execute(pool).await?;
    Ok(())
}

/// The settings the pipeline reads for this reader.
pub async fn set_reading(pool: &SqlitePool, id: &str, interests: &str, daily_stories: i64) -> Result<(), sqlx::Error> {
    sqlx::query!("UPDATE users SET interests = ?, daily_stories = ? WHERE id = ?", interests, daily_stories, id)
        .execute(pool)
        .await?;
    Ok(())
}

/// Where the reader's books are sent; `addresses` is one per line.
pub async fn set_kindle_addresses(pool: &SqlitePool, id: &str, addresses: &str) -> Result<(), sqlx::Error> {
    sqlx::query!("UPDATE users SET kindle_addresses = ? WHERE id = ?", addresses, id).execute(pool).await?;
    Ok(())
}

pub struct UserLine {
    pub id: String,
    pub name: String,
    pub role: String,
    pub timezone: String,
    pub created_at: String,
    pub kindle_addresses: String,
    pub email: Option<String>,
    pub email_verified_at: Option<String>,
    pub invited_at: Option<String>,
    /// When the invitation's link stops working; `None` for an account in use, or an invite
    /// whose link has been cleared out.
    pub invite_expires_at: Option<String>,
}

pub async fn list_users(pool: &SqlitePool) -> Result<Vec<UserLine>, sqlx::Error> {
    sqlx::query_as!(
        UserLine,
        r#"SELECT users.id AS "id!", name, role, timezone, users.created_at, kindle_addresses, users.email, email_verified_at,
           invited_at, invite.expires_at AS "invite_expires_at?"
           FROM users LEFT JOIN email_tokens invite ON invite.user_id = users.id AND invite.purpose = 'invite'
           ORDER BY users.created_at, users.id"#
    )
    .fetch_all(pool)
    .await
}

// email addresses and the links sent to them

pub const VERIFY: &str = "verify";
pub const RESET: &str = "reset";
pub const INVITE: &str = "invite";

fn token_hash(token: &str) -> String {
    use sha2::{Digest, Sha256};
    hex::encode(&Sha256::digest(token.as_bytes()))
}

fn iso_in(seconds: i64) -> String {
    (Utc::now() + chrono::Duration::seconds(seconds)).format("%Y-%m-%dT%H:%M:%S+00:00").to_string()
}

/// Whether another account already signs in with this address.
pub async fn email_taken(pool: &SqlitePool, email: &str, except: &str) -> Result<bool, sqlx::Error> {
    let found = sqlx::query_scalar!("SELECT COUNT(*) FROM users WHERE email = ? AND id <> ?", email, except)
        .fetch_one(pool)
        .await?;
    Ok(found > 0)
}

/// Ask for a change of address; it takes effect when the link sent to it is opened. An
/// account with no address yet gets it at once, unconfirmed.
pub async fn request_email(pool: &SqlitePool, id: &str, email: &str) -> Result<(), sqlx::Error> {
    sqlx::query!(
        "UPDATE users SET pending_email = CASE WHEN email IS NULL THEN NULL ELSE ?1 END,
         email = COALESCE(email, ?1) WHERE id = ?2",
        email,
        id
    )
    .execute(pool)
    .await?;
    Ok(())
}

pub async fn cancel_email_change(pool: &SqlitePool, id: &str) -> Result<(), sqlx::Error> {
    sqlx::query!("UPDATE users SET pending_email = NULL WHERE id = ?", id).execute(pool).await?;
    sqlx::query!("DELETE FROM email_tokens WHERE user_id = ? AND purpose = 'verify'", id).execute(pool).await?;
    Ok(())
}

/// A new link token for this account; any earlier one for the same purpose stops working.
pub async fn new_token(
    pool: &SqlitePool,
    token: &str,
    user: &str,
    purpose: &str,
    email: &str,
    valid_seconds: i64,
) -> Result<(), sqlx::Error> {
    let (hash, now, expires) = (token_hash(token), now_iso(), iso_in(valid_seconds));
    let mut tx = pool.begin().await?;
    sqlx::query!("DELETE FROM email_tokens WHERE (user_id = ? AND purpose = ?) OR expires_at < ?", user, purpose, now)
        .execute(&mut *tx)
        .await?;
    sqlx::query!(
        "INSERT INTO email_tokens (token_hash, user_id, purpose, email, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        hash,
        user,
        purpose,
        email,
        expires,
        now
    )
    .execute(&mut *tx)
    .await?;
    tx.commit().await
}

pub struct TokenFor {
    pub user_id: String,
    pub email: String,
}

/// Who a link token belongs to, when it exists for this purpose and has not expired.
pub async fn read_token(pool: &SqlitePool, token: &str, purpose: &str) -> Result<Option<TokenFor>, sqlx::Error> {
    let (hash, now) = (token_hash(token), now_iso());
    sqlx::query_as!(
        TokenFor,
        "SELECT user_id, email FROM email_tokens WHERE token_hash = ? AND purpose = ? AND expires_at >= ?",
        hash,
        purpose,
        now
    )
    .fetch_optional(pool)
    .await
}

pub async fn drop_tokens(pool: &SqlitePool, user: &str, purpose: &str) -> Result<(), sqlx::Error> {
    sqlx::query!("DELETE FROM email_tokens WHERE user_id = ? AND purpose = ?", user, purpose).execute(pool).await?;
    Ok(())
}

/// The link sent to `email` was opened: it becomes (or stays) the account's confirmed
/// address. False when the account no longer wants that address or another account has
/// taken it since.
pub async fn confirm_email(pool: &SqlitePool, id: &str, email: &str) -> Result<bool, sqlx::Error> {
    let now = now_iso();
    let done = sqlx::query!(
        "UPDATE users SET email = ?1, pending_email = NULL, email_verified_at = ?2
         WHERE id = ?3 AND (pending_email = ?1 OR (pending_email IS NULL AND email = ?1))
         AND NOT EXISTS (SELECT 1 FROM users other WHERE other.email = ?1 AND other.id <> ?3)",
        email,
        now,
        id
    )
    .execute(pool)
    .await?;
    Ok(done.rows_affected() > 0)
}

// what the pipeline has learned about a reader (its notes; see learned.rs)

pub struct Learned {
    pub notes: String,
    /// When the pipeline last rewrote them.
    pub learned_at: Option<String>,
}

pub async fn learned(pool: &SqlitePool, user: &str) -> Result<Learned, sqlx::Error> {
    sqlx::query_as!(Learned, "SELECT learned AS notes, learned_at FROM users WHERE id = ?", user).fetch_one(pool).await
}

/// The reader's own version of the notes. `learned_at` is left alone: it says when the
/// pipeline last looked at the ratings, which an edit does not change.
pub async fn set_learned(pool: &SqlitePool, user: &str, notes: &str) -> Result<(), sqlx::Error> {
    let now = now_iso();
    let mut tx = pool.begin().await?;
    sqlx::query!("UPDATE users SET learned = ? WHERE id = ?", notes, user).execute(&mut *tx).await?;
    sqlx::query!(
        "INSERT INTO profile_history (user_id, notes, author, created_at) VALUES (?, ?, 'reader', ?)",
        user,
        notes,
        now
    )
    .execute(&mut *tx)
    .await?;
    tx.commit().await
}

pub struct ProfileVersion {
    pub id: i64,
    pub notes: String,
    pub author: String,
    pub created_at: String,
}

/// The reader's notes as they have been, newest first.
pub async fn profile_history(pool: &SqlitePool, user: &str, limit: i64) -> Result<Vec<ProfileVersion>, sqlx::Error> {
    sqlx::query_as!(
        ProfileVersion,
        r#"SELECT id AS "id!", notes, author, created_at FROM profile_history
           WHERE user_id = ? ORDER BY id DESC LIMIT ?"#,
        user,
        limit
    )
    .fetch_all(pool)
    .await
}

/// One earlier version, only if it is this reader's.
pub async fn profile_version(pool: &SqlitePool, user: &str, id: i64) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar!("SELECT notes FROM profile_history WHERE user_id = ? AND id = ?", user, id)
        .fetch_optional(pool)
        .await
}

// the newsstand

pub struct Totals {
    pub articles: i64,
    pub issues: i64,
    pub rated: i64,
}

/// Articles delivered to this reader, books sent, and articles rated. Picks shown only as
/// links were not sent, so they count only when rated.
pub async fn totals(pool: &SqlitePool, user: &str) -> Result<Totals, sqlx::Error> {
    sqlx::query_as!(
        Totals,
        r#"SELECT COUNT(*) FILTER (WHERE d.link_only = 0) AS "articles!: i64",
           COUNT(DISTINCT d.delivered_at) FILTER (WHERE d.link_only = 0) AS "issues!: i64",
           COUNT(r.rating) AS "rated!: i64"
           FROM deliveries d LEFT JOIN ratings r ON r.user_id = d.user_id AND r.article_id = d.article_id
           WHERE d.user_id = ?"#,
        user
    )
    .fetch_one(pool)
    .await
}

/// Delivery timestamps, newest first: one per book sent that has an article matching `show`
/// ("" for all, "rated" or "unrated").
pub async fn issue_times(
    pool: &SqlitePool,
    user: &str,
    before: Option<&str>,
    show: &str,
    limit: i64,
) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar!(
        r#"SELECT DISTINCT d.delivered_at AS "delivered_at!" FROM deliveries d
           LEFT JOIN ratings r ON r.user_id = d.user_id AND r.article_id = d.article_id
           WHERE d.user_id = ?1 AND (?2 IS NULL OR d.delivered_at < ?2)
           AND (?3 = '' OR (?3 = 'rated') = (r.rating IS NOT NULL))
           ORDER BY d.delivered_at DESC LIMIT ?4"#,
        user,
        before,
        show,
        limit
    )
    .fetch_all(pool)
    .await
}

pub struct Delivered {
    pub delivered_at: String,
    /// The lead article's id when this article was delivered as part of a story, else "".
    pub story: String,
    pub id: String,
    pub url: String,
    pub source: String,
    pub title: String,
    pub author: String,
    pub word_count: i64,
    pub image_url: String,
    pub description: String,
    pub policy: Option<String>,
    pub score: Option<f64>,
    pub picked: Option<String>,
    pub rank: Option<i64>,
    pub explore: Option<bool>,
    pub rating: Option<i64>,
    pub finished: Option<bool>,
    pub why: String,
    /// Picked, but its page could not be had: shown as a link and not sent.
    pub link_only: bool,
}

/// This reader's delivered articles between two delivery timestamps that match `show`, with
/// why each was picked and its rating.
pub async fn delivered(
    pool: &SqlitePool,
    user: &str,
    oldest: &str,
    newest: &str,
    show: &str,
) -> Result<Vec<Delivered>, sqlx::Error> {
    sqlx::query_as!(
        Delivered,
        r#"SELECT d.delivered_at, d.story, a.id AS "id!", a.url, a.source, a.title, a.author, a.word_count,
           a.image_url, a.description, s.policy AS "policy?", s.score AS "score?: f64",
           s.reason AS "picked?", s.rank AS "rank?: i64", s.explore AS "explore?: bool",
           r.rating AS "rating?: i64", r.finished AS "finished?: bool",
           COALESCE(r.reason, '') AS "why!: String", d.link_only AS "link_only: bool"
           FROM deliveries d JOIN articles a ON a.id = d.article_id
           LEFT JOIN selections s ON s.user_id = d.user_id AND s.article_id = d.article_id
           LEFT JOIN ratings r ON r.user_id = d.user_id AND r.article_id = d.article_id
           WHERE d.user_id = ?1 AND d.delivered_at BETWEEN ?2 AND ?3
           AND (?4 = '' OR EXISTS (
               -- A story is shown whole when any of its articles matches, never in pieces.
               SELECT 1 FROM deliveries d2
               LEFT JOIN ratings r2 ON r2.user_id = d2.user_id AND r2.article_id = d2.article_id
               WHERE d2.user_id = d.user_id AND d2.delivered_at = d.delivered_at
               AND (d2.article_id = d.article_id OR (d.story != '' AND d2.story = d.story))
               AND (?4 = 'rated') = (r2.rating IS NOT NULL)))
           ORDER BY d.delivered_at DESC, s.policy = 'pinned' DESC, s.rank IS NULL, s.rank, a.title"#,
        user,
        oldest,
        newest,
        show
    )
    .fetch_all(pool)
    .await
}

/// Whether this article was picked for the reader but shown only as a link.
pub async fn is_link_only(pool: &SqlitePool, user: &str, article: &str) -> Result<bool, sqlx::Error> {
    let found = sqlx::query_scalar!(
        "SELECT COUNT(*) FROM deliveries WHERE user_id = ? AND article_id = ? AND link_only = 1",
        user,
        article
    )
    .fetch_one(pool)
    .await?;
    Ok(found > 0)
}

pub async fn is_delivered(pool: &SqlitePool, user: &str, article: &str) -> Result<bool, sqlx::Error> {
    let found =
        sqlx::query_scalar!("SELECT COUNT(*) FROM deliveries WHERE user_id = ? AND article_id = ?", user, article)
            .fetch_one(pool)
            .await?;
    Ok(found > 0)
}

// the archive: the reader's library, which is what was delivered to them and what the model
// weighed for them and passed over

/// Whether the pipeline keeps its judgements and routes yet; false until it has been upgraded.
pub async fn library_ready(pool: &SqlitePool) -> Result<bool, sqlx::Error> {
    let found = sqlx::query_scalar!(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name IN ('assessments', 'article_routes')"
    )
    .fetch_one(pool)
    .await?;
    Ok(found == 2)
}

/// Whether this article was delivered to the reader, added by them or weighed for them.
pub async fn in_library(pool: &SqlitePool, user: &str, article: &str) -> Result<bool, sqlx::Error> {
    if is_delivered(pool, user, article).await? {
        return Ok(true);
    }
    let added = sqlx::query_scalar!(
        "SELECT COUNT(*) FROM add_requests WHERE user_id = ? AND article_id = ? AND status = 'done'",
        user,
        article
    )
    .fetch_one(pool)
    .await?;
    if added > 0 {
        return Ok(true);
    }
    if !library_ready(pool).await? {
        return Ok(false);
    }
    let found =
        sqlx::query_scalar!("SELECT COUNT(*) FROM assessments WHERE user_id = ? AND article_id = ?", user, article)
            .fetch_one(pool)
            .await?;
    Ok(found > 0)
}

/// The feeds, recipes and front pages that have brought articles in, by name.
pub async fn brought_by(pool: &SqlitePool) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar!("SELECT DISTINCT via FROM article_routes WHERE via != '' ORDER BY via COLLATE NOCASE")
        .fetch_all(pool)
        .await
}

/// What to show from a reader's library. An empty string means "any" for every filter.
pub struct Shelf<'a> {
    pub user: &'a str,
    /// "delivered", "near" (read by the model, not delivered), "added" (by hand), "all", or ""
    /// for everything but what was only weighed from a headline.
    pub show: &'a str,
    /// Words the title must contain.
    pub title: &'a str,
    /// The feed, recipe or front page that brought it.
    pub via: &'a str,
    /// How it arrived: feed, recipe, found, linked.
    pub route: &'a str,
    /// rated, unrated, high (4 or 5), low (1 or 2), finished.
    pub rating: &'a str,
    /// years, or months (months or longer).
    pub lasting: &'a str,
    /// Dates, as 2026-10-13, compared with the day it was delivered or weighed (UTC).
    pub from: &'a str,
    pub to: &'a str,
    /// score, rating, length, or "" for newest first.
    pub sort: &'a str,
    pub limit: i64,
    pub offset: i64,
}

pub struct Shelved {
    pub id: String,
    pub url: String,
    pub source: String,
    pub title: String,
    pub author: String,
    pub word_count: i64,
    pub description: String,
    pub delivered_at: Option<String>,
    /// When the reader added it by hand, and what they asked for: next, now or keep.
    pub added_at: Option<String>,
    pub added_for: Option<String>,
    /// When it was delivered, or else added or weighed.
    pub at: String,
    pub score: Option<f64>,
    /// The model's reason, from reading it when it did.
    pub said: String,
    /// Whether the model read the article, not only its headline.
    pub read: bool,
    pub lasting: String,
    pub policy: Option<String>,
    pub rating: Option<i64>,
    pub finished: Option<bool>,
    pub why: String,
    /// One "route:via" per line.
    pub routes: String,
}

/// A query over the articles a shelf selects: `$select`, then the shared FROM and WHERE, then
/// `$rest`. The filters are ?1 to ?9, taken from the shelf. Both `shelved` and `shelved_count`
/// go through here so their filters cannot drift; a macro because sqlx's macros take only
/// string literals (joined with `+`), not constants.
macro_rules! on_shelf {
    ($query:ident!($($before:tt)*), $shelf:expr, $select:tt, $rest:tt $(, $more:expr)*) => {
        sqlx::$query!(
            $($before)*
            $select + r#"
           FROM articles a
           LEFT JOIN deliveries d ON d.article_id = a.id AND d.user_id = ?1
           LEFT JOIN assessments x ON x.article_id = a.id AND x.user_id = ?1
           LEFT JOIN selections s ON s.article_id = a.id AND s.user_id = ?1
           LEFT JOIN ratings r ON r.article_id = a.id AND r.user_id = ?1
           WHERE (d.article_id IS NOT NULL OR x.article_id IS NOT NULL OR EXISTS (SELECT 1 FROM add_requests m WHERE m.user_id = ?1 AND m.article_id = a.id AND m.status = 'done'))
           AND CASE ?2 WHEN 'all' THEN 1 WHEN 'delivered' THEN d.article_id IS NOT NULL
               WHEN 'near' THEN d.article_id IS NULL AND COALESCE(x.read, 0)
               WHEN 'added' THEN EXISTS (SELECT 1 FROM add_requests m WHERE m.user_id = ?1 AND m.article_id = a.id AND m.status = 'done')
               ELSE d.article_id IS NOT NULL OR COALESCE(x.read, 0) OR EXISTS (SELECT 1 FROM add_requests m WHERE m.user_id = ?1 AND m.article_id = a.id AND m.status = 'done') END
           AND (?3 = '' OR instr(lower(a.title), lower(?3)) > 0)
           AND (?4 = '' OR EXISTS (SELECT 1 FROM article_routes t WHERE t.article_id = a.id AND t.via = ?4))
           AND (?5 = '' OR EXISTS (SELECT 1 FROM article_routes t WHERE t.article_id = a.id AND t.route = ?5))
           AND CASE ?6 WHEN 'rated' THEN r.rating IS NOT NULL WHEN 'unrated' THEN r.rating IS NULL
               WHEN 'high' THEN COALESCE(r.rating >= 4, 0) WHEN 'low' THEN COALESCE(r.rating <= 2, 0)
               WHEN 'finished' THEN COALESCE(r.finished, 0) ELSE 1 END
           AND CASE ?7 WHEN 'years' THEN COALESCE(x.lasting = 'years', 0)
               WHEN 'months' THEN COALESCE(x.lasting IN ('months', 'years'), 0) ELSE 1 END
           AND (?8 = '' OR substr(COALESCE(d.delivered_at, (SELECT MAX(m.requested_at) FROM add_requests m WHERE m.user_id = ?1 AND m.article_id = a.id AND m.status = 'done'), x.assessed_at, a.first_seen), 1, 10) >= ?8)
           AND (?9 = '' OR substr(COALESCE(d.delivered_at, (SELECT MAX(m.requested_at) FROM add_requests m WHERE m.user_id = ?1 AND m.article_id = a.id AND m.status = 'done'), x.assessed_at, a.first_seen), 1, 10) <= ?9)
"# + $rest,
            $shelf.user,
            $shelf.show,
            $shelf.title,
            $shelf.via,
            $shelf.route,
            $shelf.rating,
            $shelf.lasting,
            $shelf.from,
            $shelf.to
            $(, $more)*
        )
    };
}

pub async fn shelved(pool: &SqlitePool, shelf: &Shelf<'_>) -> Result<Vec<Shelved>, sqlx::Error> {
    on_shelf!(
        query_as!(Shelved,),
        shelf,
        r#"SELECT a.id AS "id!", a.url, a.source, a.title, a.author, a.word_count, a.description,
           d.delivered_at AS "delivered_at?",
           (SELECT m.requested_at FROM add_requests m WHERE m.user_id = ?1 AND m.article_id = a.id
                     AND m.status = 'done' ORDER BY m.id DESC LIMIT 1) AS "added_at?: String",
           (SELECT m.action FROM add_requests m WHERE m.user_id = ?1 AND m.article_id = a.id
                     AND m.status = 'done' ORDER BY m.id DESC LIMIT 1) AS "added_for?: String",
           COALESCE(d.delivered_at, (SELECT MAX(m.requested_at) FROM add_requests m WHERE m.user_id = ?1 AND m.article_id = a.id AND m.status = 'done'), x.assessed_at, a.first_seen) AS "at!: String",
           COALESCE(s.score, x.score) AS "score?: f64",
           COALESCE(NULLIF(s.reason, ''), x.reason, '') AS "said!: String",
           COALESCE(x.read, 0) AS "read!: bool", COALESCE(x.lasting, '') AS "lasting!: String",
           s.policy AS "policy?", r.rating AS "rating?: i64", r.finished AS "finished?: bool",
           COALESCE(r.reason, '') AS "why!: String",
           COALESCE((SELECT group_concat(t.route || ':' || t.via, char(10)) FROM article_routes t
                     WHERE t.article_id = a.id), '') AS "routes!: String""#,
        r#"
           ORDER BY CASE ?10 WHEN 'score' THEN -COALESCE(s.score, x.score, -1)
               WHEN 'rating' THEN -COALESCE(r.rating, 0) WHEN 'length' THEN -a.word_count ELSE 0 END,
               COALESCE(d.delivered_at, (SELECT MAX(m.requested_at) FROM add_requests m WHERE m.user_id = ?1 AND m.article_id = a.id AND m.status = 'done'), x.assessed_at, a.first_seen) DESC, a.id
           LIMIT ?11 OFFSET ?12"#,
        shelf.sort,
        shelf.limit,
        shelf.offset
    )
    .fetch_all(pool)
    .await
}

/// How many articles the shelf's filters match, over all its pages.
pub async fn shelved_count(pool: &SqlitePool, shelf: &Shelf<'_>) -> Result<i64, sqlx::Error> {
    on_shelf!(query_scalar!(), shelf, r#"SELECT COUNT(*) AS "count!: i64""#, "").fetch_one(pool).await
}

// links added by hand; the pipeline's scheduler fetches them and records the outcome

pub const ADDS_PER_DAY: i64 = 30;

/// `action` is next (the reader's next issue), now (sent as its own book) or keep (already
/// read, with their rating when they gave one). False when the day's limit is reached.
pub async fn queue_add(
    pool: &SqlitePool,
    user: &str,
    url: &str,
    action: &str,
    rating: Option<i64>,
) -> Result<bool, sqlx::Error> {
    let (now, day_ago) = (now_iso(), iso_in(-24 * 3600));
    let mut tx = pool.begin().await?;
    let recent =
        sqlx::query_scalar!("SELECT COUNT(*) FROM add_requests WHERE user_id = ? AND requested_at > ?", user, day_ago)
            .fetch_one(&mut *tx)
            .await?;
    if recent >= ADDS_PER_DAY {
        return Ok(false);
    }
    sqlx::query!(
        "INSERT INTO add_requests (user_id, url, action, rating, requested_at) VALUES (?, ?, ?, ?, ?)",
        user,
        url,
        action,
        rating,
        now
    )
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(true)
}

pub struct AddState {
    pub url: String,
    pub status: String,
    pub error: String,
}

/// The reader's links from the last day that are still being fetched or could not be, newest first.
/// A failure is left out once it was cleared, or once the same link was added again.
pub async fn unfinished_adds(pool: &SqlitePool, user: &str) -> Result<Vec<AddState>, sqlx::Error> {
    let day_ago = iso_in(-24 * 3600);
    sqlx::query_as!(
        AddState,
        "SELECT url, status, error FROM add_requests r
         WHERE user_id = ?1 AND requested_at > ?2 AND status != 'done'
         AND NOT (status = 'failed' AND (dismissed = 1 OR EXISTS (
             SELECT 1 FROM add_requests later WHERE later.user_id = r.user_id AND later.url = r.url AND later.id > r.id)))
         ORDER BY id DESC LIMIT 20",
        user,
        day_ago
    )
    .fetch_all(pool)
    .await
}

/// Stop showing the reader's links that could not be added. They stay on record.
pub async fn dismiss_failed_adds(pool: &SqlitePool, user: &str) -> Result<(), sqlx::Error> {
    sqlx::query!("UPDATE add_requests SET dismissed = 1 WHERE user_id = ? AND status = 'failed'", user)
        .execute(pool)
        .await?;
    Ok(())
}

// send requests ("Send to Kindle"); the pipeline's scheduler sends them and records the outcome

pub const SENDS_PER_DAY: i64 = 10;

pub struct SendState {
    pub article_id: String,
    /// The whole story the article belongs to was asked for, as one book.
    pub story: bool,
    pub status: String,
    pub error: String,
    pub requested_at: String,
}

/// The reader's requests, oldest first, so a later one for the same article replaces an earlier.
pub async fn send_states(pool: &SqlitePool, user: &str) -> Result<Vec<SendState>, sqlx::Error> {
    sqlx::query_as!(
        SendState,
        r#"SELECT article_id, story AS "story: bool", status, error, requested_at
           FROM send_requests WHERE user_id = ? ORDER BY id"#,
        user
    )
    .fetch_all(pool)
    .await
}

pub enum Queued {
    Added,
    /// Already waiting to be sent.
    Waiting,
    /// The reader has asked for as many as one day allows.
    TooMany,
}

/// `story` asks for every article delivered with this one, as one book; it counts once.
pub async fn queue_send(pool: &SqlitePool, user: &str, article: &str, story: bool) -> Result<Queued, sqlx::Error> {
    let (now, day_ago) = (now_iso(), iso_in(-24 * 3600));
    let mut tx = pool.begin().await?;
    let waiting = sqlx::query_scalar!(
        "SELECT COUNT(*) FROM send_requests WHERE user_id = ? AND article_id = ? AND story = ? AND status = 'queued'",
        user,
        article,
        story
    )
    .fetch_one(&mut *tx)
    .await?;
    if waiting > 0 {
        return Ok(Queued::Waiting);
    }
    let recent =
        sqlx::query_scalar!("SELECT COUNT(*) FROM send_requests WHERE user_id = ? AND requested_at > ?", user, day_ago)
            .fetch_one(&mut *tx)
            .await?;
    if recent >= SENDS_PER_DAY {
        return Ok(Queued::TooMany);
    }
    sqlx::query!(
        "INSERT INTO send_requests (user_id, article_id, story, requested_at) VALUES (?, ?, ?, ?)",
        user,
        article,
        story,
        now
    )
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Queued::Added)
}

// "Fetch again"; the pipeline's scheduler fetches and extracts the article again

pub const REFRESHES_PER_DAY: i64 = 30;

pub struct RefreshState {
    pub article_id: String,
    pub status: String,
    pub error: String,
    pub done_at: Option<String>,
}

/// Ask for an article in the reader's library to be fetched and extracted again.
pub async fn queue_refresh(pool: &SqlitePool, user: &str, article: &str) -> Result<Queued, sqlx::Error> {
    let (now, day_ago) = (now_iso(), iso_in(-24 * 3600));
    let mut tx = pool.begin().await?;
    let waiting = sqlx::query_scalar!(
        "SELECT COUNT(*) FROM refresh_requests WHERE article_id = ? AND status = 'queued'",
        article
    )
    .fetch_one(&mut *tx)
    .await?;
    if waiting > 0 {
        return Ok(Queued::Waiting);
    }
    let recent = sqlx::query_scalar!(
        "SELECT COUNT(*) FROM refresh_requests WHERE user_id = ? AND requested_at > ?",
        user,
        day_ago
    )
    .fetch_one(&mut *tx)
    .await?;
    if recent >= REFRESHES_PER_DAY {
        return Ok(Queued::TooMany);
    }
    sqlx::query!(
        "INSERT INTO refresh_requests (user_id, article_id, requested_at) VALUES (?, ?, ?)",
        user,
        article,
        now
    )
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Queued::Added)
}

/// The reader's requests of the last day, oldest first, so a later one replaces an earlier.
pub async fn refresh_states(pool: &SqlitePool, user: &str) -> Result<Vec<RefreshState>, sqlx::Error> {
    let day_ago = iso_in(-24 * 3600);
    sqlx::query_as!(
        RefreshState,
        "SELECT article_id, status, error, done_at FROM refresh_requests
         WHERE user_id = ? AND requested_at > ? ORDER BY id",
        user,
        day_ago
    )
    .fetch_all(pool)
    .await
}

// problem reports ("this article came out broken"): for the admin, apart from any rating

/// Log the report and note it in the pipeline's extraction log, as the Kindle link does.
/// `source` is where it came from: `web`, or `kindle-footer` for a link in a book.
pub async fn report_problem(pool: &SqlitePool, user: &str, article: &str, source: &str) -> Result<(), sqlx::Error> {
    let now = now_iso();
    let mut tx = pool.begin().await?;
    sqlx::query!(
        "INSERT INTO feedback_events (user_id, article_id, signal, source, created_at) VALUES (?, ?, 'problem', ?, ?)",
        user,
        article,
        source,
        now
    )
    .execute(&mut *tx)
    .await?;
    sqlx::query!(
        "INSERT INTO extraction_reviews (article_id, checked_at, verdict, problem, model)
         VALUES (?, ?, 'bad', 'reader: broken or incomplete', 'reader')",
        article,
        now
    )
    .execute(&mut *tx)
    .await?;
    tx.commit().await
}

/// An article's title, for the page a rating link answers with.
pub async fn article_title(pool: &SqlitePool, article: &str) -> Result<Option<String>, sqlx::Error> {
    sqlx::query_scalar!("SELECT title FROM articles WHERE id = ?", article).fetch_optional(pool).await
}

/// The articles this reader has reported.
pub async fn reported(pool: &SqlitePool, user: &str) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar!(
        "SELECT DISTINCT article_id FROM feedback_events WHERE user_id = ? AND signal = 'problem'",
        user
    )
    .fetch_all(pool)
    .await
}

pub struct Problem {
    pub reported_at: String,
    pub user_id: String,
    pub via: String,
    pub title: String,
    pub url: String,
    pub source: String,
    pub words: i64,
    pub extraction: String,
}

/// Every report, newest first, with what the extractor itself made of the article.
pub async fn problems(pool: &SqlitePool, limit: i64) -> Result<Vec<Problem>, sqlx::Error> {
    sqlx::query_as!(
        Problem,
        r#"SELECT f.created_at AS reported_at, f.user_id, f.source AS via,
           COALESCE(a.title, f.article_id) AS "title!: String", COALESCE(a.url, '') AS "url!: String",
           COALESCE(a.source, '') AS "source!: String", COALESCE(a.word_count, 0) AS "words!: i64",
           COALESCE(a.status || CASE WHEN a.reason = '' THEN '' ELSE ': ' || a.reason END, '') AS "extraction!: String"
           FROM feedback_events f LEFT JOIN articles a ON a.id = f.article_id
           WHERE f.signal = 'problem' ORDER BY f.id DESC LIMIT ?"#,
        limit
    )
    .fetch_all(pool)
    .await
}

pub async fn problem_count(pool: &SqlitePool) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar!("SELECT COUNT(*) FROM feedback_events WHERE signal = 'problem'").fetch_one(pool).await
}

// ratings

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rating {
    /// 1 to 5; `None` when only "finished" was recorded.
    pub rating: Option<i64>,
    pub finished: bool,
    /// Keys of the reasons the reader gave, comma-separated; "" when none.
    pub reason: String,
}

pub async fn get_rating(pool: &SqlitePool, user: &str, article: &str) -> Result<Option<Rating>, sqlx::Error> {
    sqlx::query_as!(
        Rating,
        r#"SELECT rating AS "rating?: i64", finished AS "finished!: bool", reason
           FROM ratings WHERE user_id = ? AND article_id = ?"#,
        user,
        article
    )
    .fetch_optional(pool)
    .await
}

/// Log the signal and apply it: the latest one decides the stored rating and finished flag.
/// Returns the rating as it now stands.
///
/// A reason sets only the reason. A new rating drops a stored reason that no longer fits it
/// (a "too thin" on an article now rated 5). `source` is as for `report_problem`.
pub async fn record_feedback(
    pool: &SqlitePool,
    user: &str,
    article: &str,
    signal: &Signal,
    source: &str,
) -> Result<Rating, sqlx::Error> {
    let now = now_iso();
    let text = signal.to_string();
    let (rating, finished) = signal.meaning();
    let mut tx = pool.begin().await?;
    sqlx::query!(
        "INSERT INTO feedback_events (user_id, article_id, signal, source, created_at) VALUES (?, ?, ?, ?, ?)",
        user,
        article,
        text,
        source,
        now
    )
    .execute(&mut *tx)
    .await?;
    sqlx::query!(
        r#"INSERT INTO ratings (user_id, article_id, rating, finished, updated_at)
           VALUES (?1, ?2, ?3, COALESCE(?4, 0), ?5)
           ON CONFLICT(user_id, article_id) DO UPDATE SET
           rating = COALESCE(excluded.rating, rating),
           finished = COALESCE(?4, finished), updated_at = excluded.updated_at"#,
        user,
        article,
        rating,
        finished,
        now
    )
    .execute(&mut *tx)
    .await?;
    let stored = sqlx::query!(
        r#"SELECT rating AS "rating?: i64", finished AS "finished!: bool", reason
           FROM ratings WHERE user_id = ? AND article_id = ?"#,
        user,
        article
    )
    .fetch_one(&mut *tx)
    .await?;
    // A reason adds, replaces or takes back; any signal drops reasons that no longer fit.
    let pressed = if let Signal::Why(reason) = signal { reason.as_str() } else { "" };
    let fitting = ratings::with_reason(&stored.reason, "", stored.rating);
    let kept = ratings::with_reason(&fitting, pressed, stored.rating);
    if kept != stored.reason {
        sqlx::query!("UPDATE ratings SET reason = ? WHERE user_id = ? AND article_id = ?", kept, user, article)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await?;
    Ok(Rating { rating: stored.rating, finished: stored.finished, reason: kept })
}

/// Forget an article's rating, finished flag and reason; the event log keeps the history.
pub async fn clear_rating(pool: &SqlitePool, user: &str, article: &str) -> Result<(), sqlx::Error> {
    let now = now_iso();
    let mut tx = pool.begin().await?;
    sqlx::query!(
        "INSERT INTO feedback_events (user_id, article_id, signal, source, created_at) VALUES (?, ?, 'clear', 'web', ?)",
        user,
        article,
        now
    )
    .execute(&mut *tx)
    .await?;
    sqlx::query!("DELETE FROM ratings WHERE user_id = ? AND article_id = ?", user, article).execute(&mut *tx).await?;
    tx.commit().await
}
