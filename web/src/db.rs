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

use crate::ratings::{self, Signal};

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

// accounts

pub async fn user_count(pool: &SqlitePool) -> Result<i64, sqlx::Error> {
    sqlx::query_scalar!("SELECT COUNT(*) FROM users").fetch_one(pool).await
}

pub async fn add_user(
    pool: &SqlitePool,
    id: &str,
    name: &str,
    password_hash: &str,
    role: &str,
    timezone: &str,
) -> Result<(), sqlx::Error> {
    let now = now_iso();
    sqlx::query!(
        "INSERT INTO users (id, name, password_hash, role, timezone, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        id,
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

/// True when the account exists. Changing the hash signs out every session made with the old one.
pub async fn set_password(pool: &SqlitePool, id: &str, password_hash: &str) -> Result<bool, sqlx::Error> {
    let done = sqlx::query!("UPDATE users SET password_hash = ? WHERE id = ?", password_hash, id).execute(pool).await?;
    Ok(done.rows_affected() > 0)
}

pub async fn set_profile(pool: &SqlitePool, id: &str, name: &str, timezone: &str) -> Result<(), sqlx::Error> {
    sqlx::query!("UPDATE users SET name = ?, timezone = ? WHERE id = ?", name, timezone, id).execute(pool).await?;
    Ok(())
}

pub struct UserLine {
    pub id: String,
    pub name: String,
    pub role: String,
    pub timezone: String,
    pub created_at: String,
}

pub async fn list_users(pool: &SqlitePool) -> Result<Vec<UserLine>, sqlx::Error> {
    sqlx::query_as!(UserLine, r#"SELECT id AS "id!", name, role, timezone, created_at FROM users ORDER BY created_at"#)
        .fetch_all(pool)
        .await
}

// the newsstand

pub struct Totals {
    pub articles: i64,
    pub issues: i64,
    pub rated: i64,
}

/// Articles delivered to this reader, books sent, and articles rated.
pub async fn totals(pool: &SqlitePool, user: &str) -> Result<Totals, sqlx::Error> {
    sqlx::query_as!(
        Totals,
        r#"SELECT COUNT(*) AS "articles!: i64", COUNT(DISTINCT d.delivered_at) AS "issues!: i64",
           COUNT(r.rating) AS "rated!: i64"
           FROM deliveries d LEFT JOIN ratings r ON r.user_id = d.user_id AND r.article_id = d.article_id
           WHERE d.user_id = ?"#,
        user
    )
    .fetch_one(pool)
    .await
}

/// Delivery timestamps, newest first: one per book sent.
pub async fn issue_times(
    pool: &SqlitePool,
    user: &str,
    before: Option<&str>,
    limit: i64,
) -> Result<Vec<String>, sqlx::Error> {
    sqlx::query_scalar!(
        r#"SELECT DISTINCT delivered_at FROM deliveries
           WHERE user_id = ?1 AND (?2 IS NULL OR delivered_at < ?2)
           ORDER BY delivered_at DESC LIMIT ?3"#,
        user,
        before,
        limit
    )
    .fetch_all(pool)
    .await
}

pub struct Delivered {
    pub delivered_at: String,
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
}

/// This reader's delivered articles between two delivery timestamps, with why each was picked
/// and its rating.
pub async fn delivered(
    pool: &SqlitePool,
    user: &str,
    oldest: &str,
    newest: &str,
) -> Result<Vec<Delivered>, sqlx::Error> {
    sqlx::query_as!(
        Delivered,
        r#"SELECT d.delivered_at, a.id AS "id!", a.url, a.source, a.title, a.author, a.word_count,
           a.image_url, a.description, s.policy AS "policy?", s.score AS "score?: f64",
           s.reason AS "picked?", s.rank AS "rank?: i64", s.explore AS "explore?: bool",
           r.rating AS "rating?: i64", r.finished AS "finished?: bool",
           COALESCE(r.reason, '') AS "why!: String"
           FROM deliveries d JOIN articles a ON a.id = d.article_id
           LEFT JOIN selections s ON s.user_id = d.user_id AND s.article_id = d.article_id
           LEFT JOIN ratings r ON r.user_id = d.user_id AND r.article_id = d.article_id
           WHERE d.user_id = ?1 AND d.delivered_at BETWEEN ?2 AND ?3
           ORDER BY d.delivered_at DESC, s.policy = 'pinned' DESC, s.rank IS NULL, s.rank, a.title"#,
        user,
        oldest,
        newest
    )
    .fetch_all(pool)
    .await
}

pub async fn is_delivered(pool: &SqlitePool, user: &str, article: &str) -> Result<bool, sqlx::Error> {
    let found =
        sqlx::query_scalar!("SELECT COUNT(*) FROM deliveries WHERE user_id = ? AND article_id = ?", user, article)
            .fetch_one(pool)
            .await?;
    Ok(found > 0)
}

// ratings

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Rating {
    /// 1 to 5; `None` when only "finished" was recorded.
    pub rating: Option<i64>,
    pub finished: bool,
    /// A key of `ratings::REASONS`, or "" when the reader gave none.
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
///
/// A reason sets only the reason. A new rating drops a stored reason that no longer fits it
/// (a "too thin" on an article now rated 5).
pub async fn record_feedback(pool: &SqlitePool, user: &str, article: &str, signal: &Signal) -> Result<(), sqlx::Error> {
    let now = now_iso();
    let text = signal.to_string();
    let (rating, finished) = signal.meaning();
    let mut tx = pool.begin().await?;
    sqlx::query!(
        "INSERT INTO feedback_events (user_id, article_id, signal, source, created_at) VALUES (?, ?, ?, 'web', ?)",
        user,
        article,
        text,
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
    if let Signal::Why(reason) = signal {
        sqlx::query!("UPDATE ratings SET reason = ? WHERE user_id = ? AND article_id = ?", reason, user, article)
            .execute(&mut *tx)
            .await?;
    }
    let stored = sqlx::query!(
        r#"SELECT rating AS "rating?: i64", reason FROM ratings WHERE user_id = ? AND article_id = ?"#,
        user,
        article
    )
    .fetch_one(&mut *tx)
    .await?;
    if !stored.reason.is_empty() && !ratings::fits(&stored.reason, stored.rating) {
        sqlx::query!("UPDATE ratings SET reason = '' WHERE user_id = ? AND article_id = ?", user, article)
            .execute(&mut *tx)
            .await?;
    }
    tx.commit().await
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
