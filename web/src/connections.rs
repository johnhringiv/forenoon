//! Model connections on the settings page: servers reached through the OpenAI chat completions
//! API, such as OpenAI itself or LM Studio, Ollama, llama.cpp or vLLM on the network. Admins only.
//!
//! This app never talks to them: it holds no keys, and its pages run no scripts. It saves what an
//! admin sets and asks for tests; the pipeline, which holds each key as `FORENOON_LLM_KEY_<NAME>`,
//! checks every connection within a minute of a change, lists its models and runs the tests, and
//! writes what it found back to the row (see app/connections.py).

use axum::{
    Form,
    extract::{Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Redirect},
};
use serde::Deserialize;
use sqlx::SqlitePool;
use tower_sessions::Session;

use crate::{
    auth::AuthSession,
    db::now_iso,
    pages::{AppState, NOT_FROM_HERE, Page, allowed, reader, refused},
    settings,
};

const ADMINS_ONLY: &str = "Settings are managed by an admin.";
const NO_SUCH: &str = "There is no such connection.";
const MAX_ADDRESS: usize = 200;

/// A connection as the page shows it.
pub(crate) struct View {
    pub name: String,
    pub address: String,
    pub context_tokens: i64,
    pub output_tokens: i64,
    pub timeout_seconds: i64,
    pub free: bool,
    pub reasoning_off: bool,
    /// The variable its key is read from, in the pipeline's environment.
    pub key_env: String,
    /// What the pipeline last found, in a line.
    pub status: String,
    pub models: Vec<String>,
    pub test_result: String,
    /// A test is asked for and the pipeline has not run it yet.
    pub testing: bool,
}

/// The variable a connection's key is read from: lan-box → FORENOON_LLM_KEY_LAN_BOX.
pub(crate) fn key_env(name: &str) -> String {
    format!("FORENOON_LLM_KEY_{}", name.to_uppercase().replace('-', "_"))
}

fn listed(models: &str) -> Vec<String> {
    serde_json::from_str(models).unwrap_or_default()
}

pub(crate) async fn views(pool: &SqlitePool) -> Result<Vec<View>, sqlx::Error> {
    let rows = sqlx::query!(
        r#"SELECT name AS "name!", address, context_tokens, output_tokens, timeout_seconds,
                  free AS "free: bool", reasoning_off AS "reasoning_off: bool", checked_at, key_set AS "key_set: bool",
                  error, models, test_requested_at, tested_at, test_result
           FROM connections ORDER BY name"#
    )
    .fetch_all(pool)
    .await?;
    Ok(rows
        .into_iter()
        .map(|row| {
            let models = listed(&row.models);
            let key = if row.key_set { "key set" } else { "no key set" };
            let status = if row.checked_at.is_empty() {
                "Not checked yet: the pipeline looks within a minute.".to_owned()
            } else if !row.error.is_empty() {
                format!("Could not be reached ({key}): {}", row.error)
            } else {
                format!("Reached ({key}); lists {} models.", models.len())
            };
            View {
                key_env: key_env(&row.name),
                testing: !row.test_requested_at.is_empty() && row.test_requested_at > row.tested_at,
                name: row.name,
                address: row.address,
                context_tokens: row.context_tokens,
                output_tokens: row.output_tokens,
                timeout_seconds: row.timeout_seconds,
                free: row.free,
                reasoning_off: row.reasoning_off,
                status,
                models,
                test_result: row.test_result,
            }
        })
        .collect())
}

/// Every model the connections list, for the model settings: (`<connection>/<model>`, label).
pub(crate) fn model_choices(connections: &[View]) -> Vec<(String, String)> {
    connections
        .iter()
        .flat_map(|c| c.models.iter().map(move |m| (format!("{}/{m}", c.name), format!("{m} (on {})", c.name))))
        .collect()
}

#[derive(Deserialize)]
pub struct ConnectionForm {
    csrf: String,
    name: Option<String>,
    address: String,
    context_tokens: String,
    output_tokens: String,
    timeout_seconds: String,
    free: Option<String>,
    reasoning_off: Option<String>,
}

struct Checked {
    address: String,
    context_tokens: i64,
    output_tokens: i64,
    timeout_seconds: i64,
    free: bool,
    reasoning_off: bool,
}

/// An http or https address with a host and, if given, a port that is a number: what the
/// pipeline's HTTP client can parse. No user name or password in it, as it is shown on the page.
fn web_address(address: &str) -> bool {
    let Some(rest) = address.strip_prefix("http://").or_else(|| address.strip_prefix("https://")) else {
        return false;
    };
    let authority = rest.split(['/', '?', '#']).next().unwrap_or("");
    let (host, port) = match authority.strip_prefix('[') {
        // [::1]:8080
        Some(v6) => match v6.split_once(']') {
            Some((inside, after))
                if !inside.is_empty() && inside.chars().all(|c| c.is_ascii_hexdigit() || c == ':' || c == '.') =>
            {
                (inside, after.strip_prefix(':'))
            }
            _ => return false,
        },
        None => match authority.rsplit_once(':') {
            Some((host, port)) => (host, Some(port)),
            None => (authority, None),
        },
    };
    let host_ok = authority.starts_with('[')
        || !host.is_empty() && host.chars().all(|c| c.is_ascii_alphanumeric() || c == '.' || c == '-');
    let port_ok = port.is_none_or(|p| {
        !p.is_empty()
            && p.len() <= 5
            && p.chars().all(|c| c.is_ascii_digit())
            && p.parse::<u32>().is_ok_and(|n| n > 0 && n < 65536)
    });
    host_ok && port_ok && !rest.chars().any(|c| c.is_whitespace() || c.is_control())
}

/// A name for a new connection, or what is wrong with it.
fn checked_name(name: &str) -> Result<String, String> {
    let name = name.trim().to_lowercase();
    let ok = !name.is_empty()
        && name.len() <= 32
        && name.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-')
        && !name.starts_with('-');
    match () {
        _ if !ok => Err("Name: letters, digits and dashes, up to 32, as in lan or openai.".to_owned()),
        _ if name == "anthropic" => Err("Name: anthropic is built in; choose another.".to_owned()),
        _ => Ok(name),
    }
}

impl ConnectionForm {
    fn checked(&self) -> Result<Checked, String> {
        let address = self.address.trim().trim_end_matches('/').to_owned();
        if address.len() > MAX_ADDRESS || !web_address(&address) {
            return Err("Address: the API's base, as in http://192.168.1.20:8080/v1.".to_owned());
        }
        let number = |text: &str, label: &str, min: i64, max: i64| {
            text.trim()
                .parse::<i64>()
                .ok()
                .filter(|n| (min..=max).contains(n))
                .ok_or_else(|| format!("{label}: give a whole number from {min} to {max}."))
        };
        let context_tokens = number(&self.context_tokens, "Context size", 2048, 2_000_000)?;
        let output_tokens = number(&self.output_tokens, "Answer limit", 1024, 200_000)?;
        if output_tokens >= context_tokens {
            return Err("Answer limit: it must be less than the context size, which holds the prompt too.".to_owned());
        }
        Ok(Checked {
            address,
            context_tokens,
            output_tokens,
            timeout_seconds: number(&self.timeout_seconds, "Time limit", 5, 3600)?,
            free: self.free.is_some(),
            reasoning_off: self.reasoning_off.is_some(),
        })
    }
}

/// Signed in as an admin, from this site's own page: None, or the response that refuses it.
async fn guard(
    auth: &AuthSession,
    session: &Session,
    headers: &HeaderMap,
    csrf: &str,
) -> Result<Option<Page>, crate::pages::AppError> {
    let user = reader(auth)?;
    if !user.is_admin() {
        return Ok(Some(refused(StatusCode::FORBIDDEN, ADMINS_ONLY)));
    }
    if !allowed(headers, session, csrf).await? {
        return Ok(Some(refused(StatusCode::FORBIDDEN, NOT_FROM_HERE)));
    }
    Ok(None)
}

fn done(what: &str) -> Page {
    Ok(Redirect::to(&format!("/settings?connection={what}#model-connections")).into_response())
}

pub async fn add(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<ConnectionForm>,
) -> Page {
    if let Some(refusal) = guard(&auth, &session, &headers, &form.csrf).await? {
        return refusal;
    }
    let user = reader(&auth)?;
    let checked = checked_name(form.name.as_deref().unwrap_or("")).and_then(|name| Ok((name, form.checked()?)));
    let (name, c) = match checked {
        Ok(checked) => checked,
        Err(problem) => return settings::render(&state, &user, &session, None, Some(problem)).await,
    };
    let now = now_iso();
    let added = sqlx::query!(
        "INSERT INTO connections (name, address, context_tokens, output_tokens, timeout_seconds, free, reasoning_off, created_at)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (name) DO NOTHING",
        name,
        c.address,
        c.context_tokens,
        c.output_tokens,
        c.timeout_seconds,
        c.free,
        c.reasoning_off,
        now
    )
    .execute(&state.pool)
    .await?;
    if added.rows_affected() == 0 {
        let problem = format!("Name: there is already a connection named {name}.");
        return settings::render(&state, &user, &session, None, Some(problem)).await;
    }
    done("added")
}

pub async fn update(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(name): Path<String>,
    Form(form): Form<ConnectionForm>,
) -> Page {
    if let Some(refusal) = guard(&auth, &session, &headers, &form.csrf).await? {
        return refusal;
    }
    let user = reader(&auth)?;
    let c = match form.checked() {
        Ok(c) => c,
        Err(problem) => return settings::render(&state, &user, &session, None, Some(problem)).await,
    };
    // Saved again: the pipeline checks it afresh within a minute.
    let changed = sqlx::query!(
        "UPDATE connections SET address = ?, context_tokens = ?, output_tokens = ?, timeout_seconds = ?, free = ?,
         reasoning_off = ?, checked_at = '' WHERE name = ?",
        c.address,
        c.context_tokens,
        c.output_tokens,
        c.timeout_seconds,
        c.free,
        c.reasoning_off,
        name
    )
    .execute(&state.pool)
    .await?;
    if changed.rows_affected() == 0 {
        return refused(StatusCode::NOT_FOUND, NO_SUCH);
    }
    done("saved")
}

#[derive(Deserialize)]
pub struct ActionForm {
    csrf: String,
    #[serde(default)]
    model: String,
}

pub async fn delete(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(name): Path<String>,
    Form(form): Form<ActionForm>,
) -> Page {
    if let Some(refusal) = guard(&auth, &session, &headers, &form.csrf).await? {
        return refusal;
    }
    let gone = sqlx::query!("DELETE FROM connections WHERE name = ?", name).execute(&state.pool).await?;
    if gone.rows_affected() == 0 {
        return refused(StatusCode::NOT_FOUND, NO_SUCH);
    }
    done("deleted")
}

/// Asks the pipeline to try one small request on the connection, with the model chosen (or the
/// first it lists); the result is on the page within a minute.
pub async fn test(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Path(name): Path<String>,
    Form(form): Form<ActionForm>,
) -> Page {
    if let Some(refusal) = guard(&auth, &session, &headers, &form.csrf).await? {
        return refusal;
    }
    let model = form.model.trim();
    if model.len() > MAX_ADDRESS || model.chars().any(|c| c.is_control()) {
        return refused(StatusCode::BAD_REQUEST, "That is not a model name.");
    }
    let now = now_iso();
    let asked = sqlx::query!(
        "UPDATE connections SET test_model = ?, test_requested_at = ?, test_result = '' WHERE name = ?",
        model,
        now,
        name
    )
    .execute(&state.pool)
    .await?;
    if asked.rows_affected() == 0 {
        return refused(StatusCode::NOT_FOUND, NO_SUCH);
    }
    done("testing")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn form(address: &str, context: &str) -> ConnectionForm {
        ConnectionForm {
            csrf: String::new(),
            name: None,
            address: address.to_owned(),
            context_tokens: context.to_owned(),
            output_tokens: "16384".to_owned(),
            timeout_seconds: "300".to_owned(),
            free: Some("1".to_owned()),
            reasoning_off: None,
        }
    }

    #[test]
    fn a_connection_is_checked_before_it_is_saved() {
        assert_eq!(checked_name(" LAN ").unwrap(), "lan");
        for bad in ["", "anthropic", "-x", "a b", "a_b", &"x".repeat(33)] {
            assert!(checked_name(bad).is_err(), "{bad}");
        }
        let ok = form("http://192.168.1.20:8080/v1/", "32768").checked().unwrap();
        assert_eq!(ok.address, "http://192.168.1.20:8080/v1");
        assert!(ok.free && !ok.reasoning_off);
        for bad in [
            "192.168.1.20:8080",
            "ftp://x",
            "http://",
            "http://a b",
            "http://a:port/v1",
            "http://192.168.1.20:80o0/v1",
            "http://[::1/v1",
            "http://user:pw@host/v1",
            "http://h:99999/v1",
        ] {
            assert!(form(bad, "32768").checked().is_err(), "{bad}");
        }
        assert!(form("http://x/v1", "1000").checked().is_err() && form("http://x/v1", "lots").checked().is_err());
        assert!(form("http://x/v1", "16384").checked().is_err(), "the answer limit must leave room for the prompt");
        for good in
            ["https://api.openai.com/v1", "http://[::1]:8080/v1", "http://lan-box.local:11434/v1", "http://h/v1?x"]
        {
            assert!(form(good, "65536").checked().is_ok(), "{good}");
        }
        assert_eq!(key_env("lan-box"), "FORENOON_LLM_KEY_LAN_BOX");
    }
}
