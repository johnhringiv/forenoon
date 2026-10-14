//! The settings page: how the pipeline behaves, for the whole install. Admins only.
//!
//! `settings.json` describes every setting offered and its default; a Python test holds it to
//! the pipeline's own defaults. Only a value that differs from the default is stored, so a
//! default that changes in a later version reaches an install that never touched it.

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
    auth::AuthSession,
    db::{self, now_iso},
    pages::{AppState, NOT_FROM_HERE, Page, allowed, csrf_token, html, reader, refused, thousands},
};

const MAX_TEXT: usize = 200;
const ADMINS_ONLY: &str = "Settings are managed by an admin.";

#[derive(Deserialize)]
struct Spec {
    section: String,
    key: String,
    /// "time", "choice", "int", "float", "bool", "text" or "email".
    kind: String,
    default: serde_json::Value,
    label: String,
    #[serde(default)]
    hint: String,
    min: Option<f64>,
    max: Option<f64>,
    #[serde(default)]
    choices: Vec<(String, String)>,
}

static SPECS: LazyLock<Vec<Spec>> =
    LazyLock::new(|| serde_json::from_str(include_str!("../settings.json")).expect("settings.json is valid"));

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

struct Field {
    key: &'static str,
    kind: &'static str,
    label: &'static str,
    hint: &'static str,
    value: String,
    on: bool,
    changed: bool,
    choices: Vec<(&'static str, &'static str, bool)>,
    min: Option<f64>,
    max: Option<f64>,
}

struct Section {
    title: &'static str,
    fields: Vec<Field>,
}

#[derive(Template)]
#[template(path = "settings.html")]
struct SettingsPage {
    name: String,
    csrf: String,
    sections: Vec<Section>,
    error: Option<String>,
    saved: bool,
    /// What the model has used today and this month, and what it cost.
    usage: Vec<String>,
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
    let day = db::usage_since(&state.pool, &start(today)).await?;
    let month = db::usage_since(&state.pool, &start(today.with_day(1).unwrap_or(today))).await?;
    let mut lines = vec![
        format!(
            "Today: {} of {} tokens, about ${:.4}.",
            thousands(day.tokens),
            thousands(cap.parse().unwrap_or(0)),
            day.cost
        ),
        format!("This month: {} tokens, about ${:.2}.", thousands(month.tokens), month.cost),
    ];
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
        let field = Field {
            key: &spec.key,
            kind: &spec.kind,
            label: &spec.label,
            hint: &spec.hint,
            on: value == "1",
            changed: stored.get(&spec.key).is_some_and(|saved| *saved != default),
            choices: spec.choices.iter().map(|(key, label)| (key.as_str(), label.as_str(), *key == value)).collect(),
            min: spec.min,
            max: spec.max,
            value,
        };
        match sections.last_mut() {
            Some(section) if section.title == spec.section => section.fields.push(field),
            _ => sections.push(Section { title: &spec.section, fields: vec![field] }),
        }
    }
    sections
}

#[derive(Deserialize)]
pub struct PageQuery {
    saved: Option<String>,
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
    let stored = stored(&state).await?;
    let page = SettingsPage {
        name: user.name,
        csrf: csrf_token(&session).await?,
        sections: sections(&stored, &stored),
        error: None,
        saved: query.saved.is_some(),
        usage: usage(&state, &stored).await?,
    };
    html(StatusCode::OK, page)
}

pub async fn save(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<HashMap<String, String>>,
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
                let stored = stored(&state).await?;
                let page = SettingsPage {
                    name: user.name,
                    csrf: csrf_token(&session).await?,
                    sections: sections(&typed, &stored),
                    error: Some(problem),
                    saved: false,
                    usage: usage(&state, &stored).await?,
                };
                return html(StatusCode::BAD_REQUEST, page);
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
    tx.commit().await?;
    Ok(Redirect::to("/settings?saved=1").into_response())
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
        assert!(check("format", "articles").is_ok() && check("format", "pdf").is_err());
        assert!(check("keep_pages_days", "-1").is_ok() && check("keep_pages_days", "-2").is_err());
        assert!(check("max_age_days", "0").is_err() && check("max_age_days", "2.5").is_err());
        assert!(check("discovery.min_score", "7.5").is_ok() && check("discovery.min_score", "11").is_err());
        assert!(check("discovery.min_score", "NaN").is_err() && check("discovery.min_score", "inf").is_err());
        assert_eq!(check("alerts.to", " Me@Example.com ").unwrap(), "me@example.com");
        assert!(check("alerts.to", "").is_ok() && check("alerts.to", "nope").is_err());
        assert!(check("llm.model", "").is_err() && check("llm.model", "a\nb").is_err());
        assert_eq!(spec("llm.enabled").check(None).unwrap(), "0");
    }
}
