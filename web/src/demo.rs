//! Demo hosts: the pages shown read-only, as one reader, to anyone who asks.
//!
//! A public demo is an ordinary install whose web pages are also reached at one hostname per
//! persona (`alice.forenoon.example`, `bob.forenoon.example`), each a reader of the install.
//! A request for such a host needs no sign-in: it sees that reader's Newsstand, Archive and
//! Sources, and nothing else, and can change nothing. The same pages a reader signs in to,
//! so the demo cannot drift from the product. Since every visitor gets the same page, and no
//! session is made for them, a cache in front can keep the pages for a few minutes.

use std::sync::Arc;

use axum::{
    extract::{Request, State},
    http::{HeaderValue, Method, header},
    middleware::Next,
    response::{IntoResponse, Response},
};
use serde::Deserialize;
use sqlx::SqlitePool;

use crate::{auth, db, hex, icons, pages};

/// One reader shown at one hostname.
#[derive(Clone, Debug, PartialEq)]
pub struct Persona {
    /// Lowercase, as browsers send it in `Host`: with its port only when that is not 80 or 443,
    /// which browsers leave out.
    pub host: String,
    /// The reader's account id.
    pub id: String,
}

/// `alice.forenoon.example=alice,bob.forenoon.example=bob` (`FORENOON_WEB_DEMO`).
pub fn parse(text: &str) -> Result<Vec<Persona>, String> {
    let mut personas: Vec<Persona> = Vec::new();
    for entry in text.split(',').map(str::trim).filter(|entry| !entry.is_empty()) {
        let Some((host, id)) = entry.split_once('=') else {
            return Err(format!("{entry}: expected host=reader, such as alice.forenoon.example=alice"));
        };
        let host = host.trim().to_ascii_lowercase();
        let host = host.strip_suffix(":443").or_else(|| host.strip_suffix(":80")).map_or(host.clone(), str::to_owned);
        let plain = !host.is_empty()
            && host.chars().all(|c| c.is_ascii_alphanumeric() || matches!(c, '.' | '-' | ':' | '[' | ']'));
        if !plain {
            return Err(format!("{host}: not a hostname; give it without a scheme or a path"));
        }
        if !auth::valid_id(id.trim()) {
            return Err(format!("{}: not an account id", id.trim()));
        }
        if personas.iter().any(|persona| persona.host == host) {
            return Err(format!("{host} is listed twice"));
        }
        personas.push(Persona { host, id: id.trim().to_owned() });
    }
    Ok(personas)
}

/// On a request for a demo host: whose pages these are. Handlers read it through
/// `pages::Viewer`, and the middleware that would need a session stands aside.
#[derive(Clone)]
pub struct Demo {
    pub reader: String,
    pub personas: Arc<[Persona]>,
}

/// How long a cache in front may keep a demo page. Short: an issue arrives once a day, but a
/// thumbnail's card should not point at a picture the cache does not have yet.
const CACHE_SECONDS: u32 = 300;

/// The host a request names, lowercase, with and without its port.
fn hosts(request: &Request) -> Option<(String, String)> {
    let host = request.headers().get(header::HOST)?.to_str().ok()?.trim().to_ascii_lowercase();
    let bare = match host.rsplit_once(':') {
        Some((bare, port)) if !port.is_empty() && port.bytes().all(|b| b.is_ascii_digit()) => bare.to_owned(),
        _ => host.clone(),
    };
    Some((host, bare))
}

/// Whether a demo host answers this request: only looking, at the three pages, their
/// pictures and the icons. The health check too, so a check inside the container passes
/// whatever host it names.
fn allowed(request: &Request) -> bool {
    if request.method() != Method::GET && request.method() != Method::HEAD {
        return false;
    }
    let path = request.uri().path();
    matches!(path, "/" | "/archive" | "/sources" | "/healthz")
        || path.starts_with("/thumb/")
        || icons::FILES.iter().any(|(icon, _, _)| *icon == path)
}

/// Outermost: a request for a demo host is marked as the persona's and allowed only to look;
/// anything else there does not exist. Other hosts pass through untouched.
pub async fn gate(State(personas): State<Arc<[Persona]>>, request: Request, next: Next) -> Response {
    let persona = hosts(&request)
        .and_then(|(host, bare)| personas.iter().find(|persona| persona.host == host || persona.host == bare));
    let Some(persona) = persona else {
        return next.run(request).await;
    };
    let mut response = if allowed(&request) {
        let mut request = request;
        request.extensions_mut().insert(Demo { reader: persona.id.clone(), personas: personas.clone() });
        next.run(request).await
    } else {
        // Answered here, outside the layers that dress every response, so dressed by hand.
        pages::secured(pages::not_found().await.unwrap_or_else(IntoResponse::into_response))
    };
    // The same answer for everyone, so a cache in front may keep it. This replaces the
    // no-store the inner layers gave it. Nothing here starts a session, but a visitor may
    // carry a stale cookie for this host (one from the install's own address on another
    // port, say), which the session layer would answer by clearing it: not on a page a
    // cache may keep.
    response.headers_mut().remove(header::SET_COOKIE);
    response.headers_mut().insert(
        header::CACHE_CONTROL,
        HeaderValue::from_str(&format!("public, max-age={CACHE_SECONDS}")).expect("a plain header"),
    );
    response
}

// seeding the demo

/// The demo's readers and their shared feeds, as `demo.json` beside the crate's manifest
/// gives them; docs/demo.md says why each is there.
#[derive(Deserialize)]
struct Seed {
    personas: Vec<SeedPersona>,
    feeds: Vec<SeedFeed>,
}

#[derive(Deserialize)]
struct SeedPersona {
    id: String,
    name: String,
    interests: String,
}

#[derive(Deserialize)]
struct SeedFeed {
    name: String,
    url: String,
}

const SEED: &str = include_str!("../demo.json");

/// Makes what the demo hosts need and the install does not have yet: a reader for each
/// configured persona the seed file knows, with its interests, and, when the install has no
/// feeds at all, the shared feeds, followed by every persona. Run on every start, it makes
/// only what is missing and touches nothing else, so a change made later on the pages
/// stands. Returns a line for the log per thing made or left unmade.
pub async fn seed(pool: &SqlitePool, personas: &[Persona], timezone: &str) -> Result<Vec<String>, crate::Error> {
    let file: Seed = serde_json::from_str(SEED)?;
    let mut made = Vec::new();
    let mut accounts = Vec::new();
    for persona in personas {
        if auth::find_user(pool, &persona.id).await?.is_some() {
            accounts.push(persona.id.as_str());
            continue;
        }
        let Some(profile) = file.personas.iter().find(|profile| profile.id == persona.id) else {
            made.push(format!(
                "Demo host {} names reader {}, which is not an account yet and is not in demo.json.",
                persona.host, persona.id
            ));
            continue;
        };
        // Nobody signs in as a persona: a password no one knows.
        let mut bytes = [0u8; 32];
        getrandom::fill(&mut bytes).map_err(|error| error.to_string())?;
        let hash = auth::hash_password(hex::encode(&bytes)).await?;
        let email = format!("{}@demo.invalid", profile.id);
        db::add_persona(pool, &profile.id, &email, &profile.name, &hash, timezone, &profile.interests).await?;
        accounts.push(persona.id.as_str());
        made.push(format!("Made reader {} for demo host {}.", profile.id, persona.host));
    }
    if db::feed_count(pool).await? == 0
        && let Some(first) = accounts.first()
    {
        let feeds: Vec<(&str, &str)> = file.feeds.iter().map(|feed| (feed.name.as_str(), feed.url.as_str())).collect();
        // Followed by the personas, and only these feeds: a persona made by hand earlier
        // keeps whatever else it chose to follow or not.
        db::add_shared_feeds(pool, &feeds, first, &accounts).await?;
        made.push(format!("Added the {} shared feeds from demo.json.", file.feeds.len()));
    }
    Ok(made)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_seed_file_is_well_formed() {
        let file: Seed = serde_json::from_str(SEED).unwrap();
        assert!(file.personas.iter().all(|p| auth::valid_id(&p.id) && !p.name.is_empty() && !p.interests.is_empty()));
        assert!(file.feeds.iter().all(|f| !f.name.is_empty() && pages::web_url(&f.url).is_some()));
    }

    #[test]
    fn personas_are_parsed_from_host_reader_pairs() {
        let personas =
            parse(" Alice.Forenoon.example:443=alice, bob.forenoon.example:8081=bob ,carol.example:80=carol").unwrap();
        assert_eq!(
            personas,
            [
                Persona { host: "alice.forenoon.example".to_owned(), id: "alice".to_owned() },
                Persona { host: "bob.forenoon.example:8081".to_owned(), id: "bob".to_owned() },
                Persona { host: "carol.example".to_owned(), id: "carol".to_owned() },
            ]
        );
        assert!(parse("").unwrap().is_empty());
        for bad in [
            "alice.forenoon.example",
            "https://alice.forenoon.example=alice",
            "a.example=no way",
            "a.example=x,a.example=y",
        ] {
            assert!(parse(bad).is_err(), "{bad}");
        }
    }
}
