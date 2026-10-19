//! The settings page: how the pipeline behaves, for the whole install. Admins only.
//!
//! `settings.json` describes every setting offered and its default; a Python test holds it to
//! the pipeline's own defaults. A model setting is typed, named with its connection, and
//! suggests the models in `models.json`, the list shipped with the app that the pipeline also
//! reads for prices. Only a value that differs from the default is stored, so a default that
//! changes in a later version reaches an install that never touched it.
//!
//! The Test button asks the pipeline, which holds the connections and their keys, to send one
//! small request to each model named (see `app/model_test.py`); its answer is shown here.

use std::{collections::HashMap, sync::LazyLock};

use askama::Template;
use axum::{
    Form,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Redirect},
};
use serde::Deserialize;
use tower_sessions::Session;

use chrono::{Datelike, NaiveDate, Utc};
use chrono_tz::Tz;
use std::str::FromStr;

use crate::{
    auth::{AuthSession, User},
    db::{self, now_iso},
    pages::{AppState, NOT_FROM_HERE, Nav, Page, allowed, csrf_token, html, reader, refused, thousands},
};

const MAX_TEXT: usize = 200;
const ADMINS_ONLY: &str = "Settings are managed by an admin.";

#[derive(Deserialize)]
struct Spec {
    section: String,
    key: String,
    /// "time", "choice", "int", "float", "bool", "text", "email" or "model".
    kind: String,
    default: serde_json::Value,
    label: String,
    #[serde(default)]
    hint: String,
    min: Option<f64>,
    max: Option<f64>,
    #[serde(default)]
    choices: Vec<(String, String)>,
    /// For a model setting: what leaving it empty means ("the model above"), if it may be empty.
    #[serde(default)]
    same: String,
}

#[derive(Deserialize)]
struct Model {
    id: String,
    label: String,
}

#[derive(Deserialize)]
struct Models {
    models: Vec<Model>,
}

static SPECS: LazyLock<Vec<Spec>> =
    LazyLock::new(|| serde_json::from_str(include_str!("../settings.json")).expect("settings.json is valid"));

/// The shipped models, suggested in the model settings: (id, label).
static SHIPPED: LazyLock<Vec<(String, String)>> = LazyLock::new(|| {
    let models: Models = serde_json::from_str(include_str!("../models.json")).expect("models.json is valid");
    models.models.into_iter().map(|model| (model.id, model.label)).collect()
});

impl Spec {
    /// The default as it would be stored: "1" or "0" for yes or no, otherwise as written.
    fn default_text(&self) -> String {
        match &self.default {
            serde_json::Value::Bool(on) => if *on { "1" } else { "0" }.to_owned(),
            serde_json::Value::String(text) => text.clone(),
            other => other.to_string(),
        }
    }

    /// What a form sent for this setting, as it should be stored; or what is wrong with it.
    fn check(&self, sent: Option<&String>) -> Result<String, String> {
        let text = sent.map(|text| text.trim()).unwrap_or("");
        let bad = |why: &str| Err(format!("{}: {why}.", self.label));
        match self.kind.as_str() {
            // A box that is not ticked sends nothing.
            "bool" => Ok(if sent.is_some() { "1" } else { "0" }.to_owned()),
            "time" => {
                let ok = text.len() == 5
                    && text.split_once(':').is_some_and(|(hour, minute)| {
                        hour.len() == 2
                            && hour.parse::<u8>().is_ok_and(|hour| hour < 24)
                            && minute.parse::<u8>().is_ok_and(|minute| minute < 60)
                    });
                if ok { Ok(text.to_owned()) } else { bad("give a time such as 06:00") }
            }
            "choice" => match self.choices.iter().any(|(value, _)| value == text) {
                true => Ok(text.to_owned()),
                false => bad("choose one of the options"),
            },
            "model" if text.is_empty() && !self.same.is_empty() => Ok(String::new()),
            "model" => match model_name(text) {
                true => Ok(text.to_owned()),
                false => bad(
                    "name a model with its connection in lower case, as in anthropic/claude-haiku-5-5 or lan/qwen3.8-27b",
                ),
            },
            "int" | "float" => {
                let number =
                    if self.kind == "int" { text.parse::<i64>().ok().map(|n| n as f64) } else { text.parse().ok() };
                let (min, max) = (self.min.unwrap_or(0.0), self.max.unwrap_or(f64::MAX));
                match number {
                    Some(number) if number.is_finite() && number >= min && number <= max => Ok(text.to_owned()),
                    _ => bad(&format!("give a number from {min} to {max}")),
                }
            }
            "email" if text.is_empty() => Ok(String::new()),
            "email" => match crate::auth::normal_email(text) {
                Some(address) => Ok(address),
                None => bad("not an email address"),
            },
            _ => {
                let plain = !text.is_empty() && text.len() <= MAX_TEXT && !text.chars().any(char::is_control);
                if plain { Ok(text.to_owned()) } else { bad("it cannot be empty") }
            }
        }
    }
}

/// A model named with its connection, as in anthropic/claude-haiku-5-5. The connection is in
/// lower case, as the pipeline names it: FORENOON_LLM_LAN_URL is `lan`.
fn model_name(text: &str) -> bool {
    text.len() <= MAX_TEXT
        && !text.chars().any(|c| c.is_control() || c.is_whitespace())
        && text.split_once('/').is_some_and(|(connection, model)| {
            !connection.is_empty()
                && connection.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
                && !model.is_empty()
        })
}

struct Field {
    key: &'static str,
    kind: &'static str,
    label: &'static str,
    hint: &'static str,
    /// For a model setting: what leaving it empty means.
    same: &'static str,
    value: String,
    on: bool,
    changed: bool,
    choices: Vec<(String, String, bool)>,
    min: Option<f64>,
    max: Option<f64>,
}

/// "Model use" as an anchor: model-use.
fn slug(title: &str) -> String {
    title.to_lowercase().split(|c: char| !c.is_alphanumeric()).filter(|w| !w.is_empty()).collect::<Vec<_>>().join("-")
}

struct Section {
    /// The heading's anchor, for the links that jump to each section.
    id: String,
    title: &'static str,
    fields: Vec<Field>,
}

#[derive(Template)]
#[template(path = "settings.html")]
struct SettingsPage {
    nav: Option<Nav>,
    csrf: String,
    sections: Vec<Section>,
    error: Option<String>,
    saved: bool,
    /// What the model has used today and this month, and what it cost.
    usage: Vec<String>,
    /// Suggested in the model settings.
    models: &'static [(String, String)],
    test: Option<TestView>,
}

/// Seconds a test may wait for the pipeline to begin before the page stops refreshing and
/// asks whether it is running: it looks every few seconds, but not during the morning run, and
/// one asked for again begins when the request under way for the last returns (two minutes
/// at most).
const TEST_PICKUP: i64 = 150;
/// Seconds without word from a test under way before the page gives up on it (each model has
/// two minutes at most).
const TEST_SILENCE: i64 = 300;

/// The last test of the models: asked for, under way, or answered.
struct TestView {
    /// "waiting", "late" (not begun in TEST_PICKUP), "running", "silent" or "done".
    state: &'static str,
    tested_at: String,
    lines: Vec<String>,
}

impl TestView {
    fn refresh(&self) -> bool {
        matches!(self.state, "waiting" | "running")
    }
}

fn seconds_since(stamp: &str) -> i64 {
    chrono::DateTime::parse_from_rfc3339(stamp)
        .map(|at| (Utc::now() - at.with_timezone(&Utc)).num_seconds())
        .unwrap_or(0)
}

async fn test_view(state: &AppState) -> Result<Option<TestView>, sqlx::Error> {
    let row = sqlx::query!("SELECT requested_at, answered, started, tested_at, result FROM model_tests WHERE id = 1")
        .fetch_optional(&state.pool)
        .await?;
    Ok(row.map(|row| {
        let state = if row.answered == row.requested_at {
            "done"
        } else if row.started == row.requested_at {
            if seconds_since(&row.tested_at) > TEST_SILENCE { "silent" } else { "running" }
        } else if seconds_since(&row.requested_at) > TEST_PICKUP {
            "late"
        } else {
            "waiting"
        };
        TestView {
            state,
            tested_at: row.tested_at.get(..16).unwrap_or(&row.tested_at).replace('T', " "),
            lines: row.result.lines().map(str::to_owned).collect(),
        }
    }))
}

#[derive(Template)]
#[template(path = "model_test.html")]
struct TestPage {
    nav: Option<Nav>,
    test: TestView,
}

/// The test's progress and result, refreshing itself while the pipeline works on it.
pub async fn test_page(State(state): State<AppState>, auth: AuthSession) -> Page {
    let user = reader(&auth)?;
    if !user.is_admin() {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    let Some(test) = test_view(&state).await? else {
        return Ok(Redirect::to("/settings#model-test").into_response());
    };
    html(StatusCode::OK, TestPage { nav: Nav::new(&state, &user, "settings").await?, test })
}

/// Today's and this month's use of the model, in the install's time zone (the pipeline's day).
async fn usage(state: &AppState, stored: &HashMap<String, String>) -> Result<Vec<String>, sqlx::Error> {
    if !db::pipeline_ready(&state.pool).await? {
        return Ok(Vec::new());
    }
    let zone = std::env::var("TZ").ok().and_then(|zone| Tz::from_str(&zone).ok()).unwrap_or(Tz::UTC);
    let today = Utc::now().with_timezone(&zone).date_naive();
    let start = |day: NaiveDate| {
        day.and_hms_opt(0, 0, 0)
            .and_then(|midnight| midnight.and_local_timezone(zone).earliest())
            .map(|at| at.with_timezone(&Utc).format("%Y-%m-%dT%H:%M:%S+00:00").to_string())
            .unwrap_or_default()
    };
    let cap = stored.get("llm.daily_token_cap").cloned().unwrap_or_else(|| {
        SPECS.iter().find(|spec| spec.key == "llm.daily_token_cap").map(Spec::default_text).unwrap_or_default()
    });
    let check_cap = stored.get("llm.check_daily_tokens").cloned().unwrap_or_else(|| {
        SPECS.iter().find(|spec| spec.key == "llm.check_daily_tokens").map(Spec::default_text).unwrap_or_default()
    });
    let day = db::usage_since(&state.pool, &start(today)).await?;
    let month = db::usage_since(&state.pool, &start(today.with_day(1).unwrap_or(today))).await?;
    let mut lines = vec![
        format!(
            "Today: {} of {} tokens, about ${:.4}.",
            thousands(day.tokens),
            thousands(cap.parse().unwrap_or(0)),
            day.cost
        ),
        format!("This month: {} tokens, about ${:.2}.", thousands(month.tokens + month.check_tokens), month.cost),
    ];
    if month.check_tokens > 0 {
        lines.insert(
            1,
            format!(
                "Of today's, the book check's own: {} of {} tokens.",
                thousands(day.check_tokens),
                thousands(check_cap.parse().unwrap_or(0))
            ),
        );
    }
    if month.free_tokens > 0 {
        lines.push(format!(
            "Also {} tokens today and {} this month on servers you run, which cost nothing and are not capped.",
            thousands(day.free_tokens),
            thousands(month.free_tokens)
        ));
    }
    if month.unpriced > 0 {
        lines.push(format!(
            "{} calls this month were to a model with no known price and are not in the cost.",
            month.unpriced
        ));
    }
    Ok(lines)
}

async fn stored(state: &AppState) -> Result<HashMap<String, String>, sqlx::Error> {
    let rows = sqlx::query!("SELECT key, value FROM settings").fetch_all(&state.pool).await?;
    Ok(rows.into_iter().map(|row| (row.key, row.value)).collect())
}

/// `shown` is what to put in the fields: what is stored, or a form that was refused.
fn sections(shown: &HashMap<String, String>, stored: &HashMap<String, String>) -> Vec<Section> {
    let mut sections: Vec<Section> = Vec::new();
    for spec in SPECS.iter() {
        let default = spec.default_text();
        let value = shown.get(&spec.key).cloned().unwrap_or_else(|| default.clone());
        let choices: Vec<(String, String, bool)> =
            spec.choices.iter().map(|(key, label)| (key.clone(), label.clone(), *key == value)).collect();
        let field = Field {
            key: &spec.key,
            kind: &spec.kind,
            label: &spec.label,
            hint: &spec.hint,
            same: &spec.same,
            on: value == "1",
            changed: stored.get(&spec.key).is_some_and(|saved| *saved != default),
            choices,
            min: spec.min,
            max: spec.max,
            value,
        };
        match sections.last_mut() {
            Some(section) if section.title == spec.section => section.fields.push(field),
            _ => sections.push(Section { id: slug(&spec.section), title: &spec.section, fields: vec![field] }),
        }
    }
    sections
}

#[derive(Deserialize)]
pub struct PageQuery {
    saved: Option<String>,
}

/// The page, with `typed` in the fields when a form was refused (else what is stored) and the
/// problem said; a 400 then.
async fn render(
    state: &AppState,
    user: &User,
    session: &Session,
    typed: Option<HashMap<String, String>>,
    error: Option<String>,
    saved: bool,
) -> Page {
    let stored = stored(state).await?;
    let page = SettingsPage {
        nav: Nav::new(state, user, "settings").await?,
        csrf: csrf_token(session).await?,
        sections: sections(typed.as_ref().unwrap_or(&stored), &stored),
        saved,
        usage: usage(state, &stored).await?,
        models: &SHIPPED,
        test: test_view(state).await?,
        error,
    };
    html(if page.error.is_some() { StatusCode::BAD_REQUEST } else { StatusCode::OK }, page)
}

pub async fn page(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    Query(query): Query<PageQuery>,
) -> Page {
    let user = reader(&auth)?;
    if !user.is_admin() {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    render(&state, &user, &session, None, None, query.saved.is_some()).await
}

/// Saves the form; with `test`, also asks the pipeline to test the models it names.
async fn save_form(
    state: AppState,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    form: HashMap<String, String>,
    test: bool,
) -> Page {
    let user = reader(&auth)?;
    if !user.is_admin() {
        return refused(StatusCode::FORBIDDEN, ADMINS_ONLY);
    }
    if !allowed(&headers, &session, form.get("csrf").map(String::as_str).unwrap_or("")).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let mut values: Vec<(&Spec, String)> = Vec::new();
    for spec in SPECS.iter() {
        match spec.check(form.get(&spec.key)) {
            Ok(value) => values.push((spec, value)),
            Err(problem) => {
                // Shown again as typed, so one wrong field does not lose the rest.
                let mut typed = form.clone();
                for spec in SPECS.iter().filter(|spec| spec.kind == "bool") {
                    typed.insert(spec.key.clone(), if form.contains_key(&spec.key) { "1" } else { "0" }.to_owned());
                }
                return render(&state, &user, &session, Some(typed), Some(problem), false).await;
            }
        }
    }
    let now = now_iso();
    let mut tx = state.pool.begin().await?;
    for (spec, value) in values {
        if value == spec.default_text() {
            sqlx::query!("DELETE FROM settings WHERE key = ?", spec.key).execute(&mut *tx).await?;
        } else {
            sqlx::query!(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                 ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                 WHERE value != excluded.value",
                spec.key,
                value,
                now
            )
            .execute(&mut *tx)
            .await?;
        }
    }
    if test {
        sqlx::query!(
            "INSERT INTO model_tests (id, requested_at) VALUES (1, ?)
             ON CONFLICT (id) DO UPDATE SET requested_at = excluded.requested_at, answered = '', started = '', result = ''",
            now
        )
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(Redirect::to(if test { "/settings/test" } else { "/settings?saved=1" }).into_response())
}

pub async fn save(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<HashMap<String, String>>,
) -> Page {
    save_form(state, auth, session, headers, form, false).await
}

/// The Test button: saves the form, then asks for the test.
pub async fn test(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<HashMap<String, String>>,
) -> Page {
    save_form(state, auth, session, headers, form, true).await
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn every_setting_has_a_usable_default() {
        assert!(SPECS.len() > 10);
        for spec in SPECS.iter() {
            let default = spec.default_text();
            let sent = if spec.kind == "bool" { (default == "1").then_some(&default) } else { Some(&default) };
            assert_eq!(spec.check(sent).as_ref(), Ok(&default), "{}", spec.key);
        }
    }

    #[test]
    fn values_are_checked_by_kind() {
        let spec = |key: &str| SPECS.iter().find(|spec| spec.key == key).unwrap();
        let check = |key: &str, text: &str| spec(key).check(Some(&text.to_owned()));
        assert_eq!(check("delivery_time", " 07:30 ").unwrap(), "07:30");
        for bad in ["7:30", "24:00", "06:60", "soon", ""] {
            assert!(check("delivery_time", bad).is_err(), "{bad}");
        }
        assert!(check("keep_pages_days", "-1").is_ok() && check("keep_pages_days", "-2").is_err());
        assert!(check("max_age_days", "0").is_err() && check("max_age_days", "2.5").is_err());
        assert_eq!(check("alerts.to", " Me@Example.com ").unwrap(), "me@example.com");
        assert!(check("alerts.to", "").is_ok() && check("alerts.to", "nope").is_err());
        assert!(check("llm.model", "anthropic/claude-sonnet-5-5").is_ok());
        assert!(check("llm.model", "").is_err() && check("llm.model", "claude-haiku-5-5").is_err());
        // Any model typed is kept, as long as it names its connection.
        assert!(check("llm.model", "anthropic/claude-haiku-5-5-20261001").is_ok());
        assert!(check("llm.model", "lan/unsloth/Qwen3.8-27B-GGUF").is_ok());
        assert!(check("llm.model", "gpu_box/qwen").is_ok() && check("llm.model", "LAN/qwen").is_err());
        assert!(check("llm.model", "anthropic/").is_err() && check("llm.model", "anthropic/a b").is_err());
        assert!(
            check("llm.choose_model", "").is_ok() && check("llm.choose_model", "anthropic/claude-opus-5-5").is_ok()
        );
        assert_eq!(spec("alerts.enabled").check(None).unwrap(), "0");
    }
}
