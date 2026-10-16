//! The archive: the reader's library as a list that can be filtered and searched.
//!
//! The newsstand shows what was delivered, day by day. This shows that and what the model
//! weighed for the reader and passed over, one row each, with how it reached us and what the
//! model made of it. Filters are a plain form sent by GET, so any view is a link. A row can be
//! rated and sent to the Kindle with the newsstand's own handlers.

use std::str::FromStr;

use askama::Template;
use axum::{
    Form,
    extract::{Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Redirect},
};
use chrono::NaiveDate;
use chrono_tz::Tz;
use percent_encoding::{NON_ALPHANUMERIC, utf8_percent_encode};
use serde::Deserialize;
use tower_sessions::Session;

use crate::{
    auth::AuthSession,
    db::{self, Shelf, Shelved},
    pages::{
        AppState, MAX_DESCRIPTION_CHARS, NOT_FROM_HERE, Nav, Page, ReasonOption, Star, allowed, back_to, csrf_token,
        html, length, local, reader, reason_options, refused, send_outcome, shorten, stars, web_url,
    },
    ratings,
};

const PER_PAGE: i64 = 50;
const MAX_SEARCH: usize = 100;
const MAX_URL: usize = 2000;
const SHOW: [(&str, &str); 5] = [
    ("", "Delivered, added, or read by the model"),
    ("delivered", "Delivered"),
    ("near", "Read by the model, not delivered"),
    ("added", "Added by you"),
    ("all", "Everything weighed"),
];
const ROUTE: [(&str, &str); 6] = [
    ("", "Any way"),
    ("feed", "A feed you follow"),
    ("recipe", "A Calibre recipe"),
    ("found", "Found on a front page"),
    ("linked", "Linked from another article"),
    ("manual", "Added by hand"),
];
const RATING: [(&str, &str); 6] = [
    ("", "Any"),
    ("rated", "Rated"),
    ("unrated", "Not rated"),
    ("high", "4 or 5"),
    ("low", "1 or 2"),
    ("finished", "Finished"),
];
const LASTING: [(&str, &str); 3] = [("", "Any"), ("months", "Months or longer"), ("years", "Years")];
const SORT: [(&str, &str); 4] =
    [("", "Newest"), ("score", "Model's score"), ("rating", "Your rating"), ("length", "Longest")];

#[derive(Deserialize, Default)]
pub struct ArchiveQuery {
    q: Option<String>,
    show: Option<String>,
    via: Option<String>,
    route: Option<String>,
    rating: Option<String>,
    lasting: Option<String>,
    from: Option<String>,
    to: Option<String>,
    sort: Option<String>,
    page: Option<String>,
    error: Option<String>,
}

struct Pending {
    url: String,
    note: String,
    failed: bool,
}

struct Choice {
    value: String,
    label: String,
    selected: bool,
}

struct Select {
    name: &'static str,
    label: &'static str,
    choices: Vec<Choice>,
}

/// One article. Named for what the shared rating form expects of a newsstand card.
struct Row {
    id: String,
    link: Option<String>,
    title: String,
    kicker: String,
    desc: Option<String>,
    /// What the model said of it, with whether it had read it.
    said: Option<String>,
    facts: Vec<String>,
    marks: Vec<String>,
    unrated: bool,
    finished: bool,
    stars: Vec<Star>,
    reasons: Vec<ReasonOption>,
    question: &'static str,
    sendable: bool,
    sent: Option<String>,
    /// How the last "Fetch again" went, within the day; and whether it is still waiting.
    refreshed: Option<String>,
    refreshing: bool,
}

#[derive(Template)]
#[template(path = "archive.html")]
struct ArchivePage {
    nav: Option<Nav>,
    csrf: String,
    /// False until the pipeline keeps what it weighs: only then is there more than deliveries.
    ready: bool,
    search: String,
    from: String,
    to: String,
    selects: Vec<Select>,
    filtered: bool,
    error: Option<&'static str>,
    /// Links still being fetched, and ones that could not be.
    pending: Vec<Pending>,
    /// Reload while a link is being fetched, so its article appears without a click.
    fetching: bool,
    /// Some links could not be added: offer to clear them.
    failed: bool,
    rows: Vec<Row>,
    /// "214 articles match, showing 51–100.": none when nothing matches.
    count: Option<String>,
    back: String,
    newer: Option<String>,
    older: Option<String>,
}

/// The value when it is one of the choices offered, else the first ("any").
fn one_of(value: &Option<String>, choices: &[(&'static str, &'static str)]) -> &'static str {
    choices.iter().map(|(key, _)| *key).find(|key| Some(*key) == value.as_deref()).unwrap_or("")
}

fn select(name: &'static str, label: &'static str, choices: &[(&'static str, &'static str)], chosen: &str) -> Select {
    let choices = choices
        .iter()
        .map(|(value, label)| Choice {
            value: (*value).to_owned(),
            label: (*label).to_owned(),
            selected: *value == chosen,
        })
        .collect();
    Select { name, label, choices }
}

fn date(value: &Option<String>) -> String {
    value.as_deref().filter(|day| NaiveDate::parse_from_str(day, "%Y-%m-%d").is_ok()).unwrap_or_default().to_owned()
}

/// This view's address, on another page of it.
fn address(parts: &[(&str, &str)], page: i64) -> String {
    let number = page.to_string();
    let query: Vec<String> = parts
        .iter()
        .copied()
        .chain((page > 1).then_some(("page", number.as_str())))
        .filter(|(_, value)| !value.is_empty())
        .map(|(key, value)| format!("{key}={}", utf8_percent_encode(value, NON_ALPHANUMERIC)))
        .collect();
    if query.is_empty() { "/archive".to_owned() } else { format!("/archive?{}", query.join("&")) }
}

/// "Found on Hacker News", "From the feed Blog": one per way the article reached us.
fn arrivals(routes: &str) -> Vec<String> {
    routes
        .lines()
        .filter_map(|line| line.split_once(':'))
        .filter_map(|(route, via)| {
            Some(match route {
                "feed" => format!("From the feed {via}"),
                "recipe" => format!("From the recipe {via}"),
                "found" => format!("Found on {via}"),
                "linked" => format!("Linked from an article in {via}"),
                "manual" => return None,
                _ => format!("Through {via}"),
            })
        })
        .collect()
}

/// "214 articles match.", with which of them this page shows when it is not the first.
fn matching(total: i64, offset: i64, shown: usize) -> Option<String> {
    let count = match total {
        0 => return None,
        1 => "1 article matches".to_owned(),
        _ => format!("{total} articles match"),
    };
    Some(match (offset, shown) {
        (0, _) | (_, 0) => format!("{count}."),
        _ => format!("{count}, showing {}–{}.", offset + 1, offset + shown as i64),
    })
}

fn row(found: Shelved, zone: Tz) -> Row {
    let author = if found.author.to_lowercase() == found.source.to_lowercase() { "" } else { found.author.as_str() };
    let kicker =
        [found.source.as_str(), author].into_iter().filter(|part| !part.is_empty()).collect::<Vec<_>>().join(" · ");
    let day = |stamp: &str| local(stamp, zone).map_or(stamp.to_owned(), |at| at.format("%-d %B %Y").to_string());
    let finished = found.finished == Some(true);
    let mut facts = Vec::new();
    let added = found.added_at.as_deref().zip(found.added_for.as_deref());
    facts.push(match (&found.delivered_at, found.policy.as_deref(), found.read) {
        (Some(at), Some("requested"), _) => format!("Sent on request {}", day(at)),
        (Some(at), _, _) => format!("Delivered {}", day(at)),
        (None, _, _) if added.is_some_and(|(_, wanted)| wanted == "next") && found.rating.is_none() && !finished => {
            "Added by you; in your next issue".to_owned()
        }
        (None, _, _) if added.is_some() => format!("Added by you {}", day(added.map_or("", |(at, _)| at))),
        (None, _, true) => format!("Read by the model {}, not delivered", day(&found.at)),
        (None, _, false) => format!("Weighed from its headline {}", day(&found.at)),
    });
    if let Some(score) = found.score {
        facts.push(format!("Score {score}"));
    }
    facts.extend(length(found.word_count));
    match found.lasting.as_str() {
        "days" => facts.push("Worth reading for days".to_owned()),
        "months" => facts.push("Worth reading for months".to_owned()),
        "years" => facts.push("Worth reading for years".to_owned()),
        _ => {}
    }
    facts.extend(arrivals(&found.routes));
    facts.extend(ratings::chosen(&found.why).filter_map(ratings::label).map(str::to_owned));
    let mut marks = Vec::new();
    if let Some(rating) = found.rating {
        marks.push(format!("Rated {rating} of 5"));
    }
    if finished {
        marks.push("Finished".to_owned());
    }
    let said = (!found.said.is_empty()).then(|| {
        let how = if found.read || found.delivered_at.is_some() { "The model" } else { "The model, from the headline" };
        format!("{how}: {}", found.said)
    });
    Row {
        link: web_url(&found.url),
        title: if found.title.is_empty() { found.url.clone() } else { found.title },
        kicker,
        desc: (!found.description.is_empty()).then(|| shorten(&found.description, MAX_DESCRIPTION_CHARS)),
        said,
        facts,
        marks,
        unrated: found.rating.is_none() && !finished,
        finished,
        stars: stars(found.rating),
        reasons: reason_options(found.rating, &found.why),
        question: ratings::question(found.rating),
        sendable: false,
        sent: None,
        refreshed: None,
        refreshing: false,
        id: found.id,
    }
}

pub async fn page(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    Query(query): Query<ArchiveQuery>,
) -> Page {
    let user = reader(&auth)?;
    let zone = Tz::from_str(&user.timezone).unwrap_or(Tz::UTC);
    let pool = &state.pool;
    let search: String = query.q.as_deref().unwrap_or_default().trim().chars().take(MAX_SEARCH).collect();
    let (show, route) = (one_of(&query.show, &SHOW), one_of(&query.route, &ROUTE));
    let (rating, lasting) = (one_of(&query.rating, &RATING), one_of(&query.lasting, &LASTING));
    let sort = one_of(&query.sort, &SORT);
    let (from, to) = (date(&query.from), date(&query.to));
    let number = query.page.as_deref().and_then(|n| n.parse::<i64>().ok()).filter(|n| (1..=10_000).contains(n));
    let number = number.unwrap_or(1);
    let ready = db::pipeline_ready(pool).await? && db::library_ready(pool).await?;
    // Only a name that has brought something in can be asked for.
    let sources = if ready { db::brought_by(pool).await? } else { Vec::new() };
    let via = query.via.filter(|name| sources.contains(name)).unwrap_or_default();
    let mut by = vec![Choice { value: String::new(), label: "Any source".to_owned(), selected: via.is_empty() }];
    by.extend(sources.into_iter().map(|name| Choice { selected: name == via, label: name.clone(), value: name }));

    let parts = [
        ("q", search.as_str()),
        ("show", show),
        ("via", via.as_str()),
        ("route", route),
        ("rating", rating),
        ("lasting", lasting),
        ("from", from.as_str()),
        ("to", to.as_str()),
        ("sort", sort),
    ];
    let mut page = ArchivePage {
        nav: Nav::new(&state, &user, "archive").await?,
        csrf: csrf_token(&session).await?,
        ready,
        filtered: parts.iter().any(|(_, value)| !value.is_empty()),
        selects: vec![
            select("show", "Show", &SHOW, show),
            Select { name: "via", label: "Source", choices: by },
            select("route", "How it arrived", &ROUTE, route),
            select("rating", "Your rating", &RATING, rating),
            select("lasting", "Worth reading for", &LASTING, lasting),
            select("sort", "Order", &SORT, sort),
        ],
        error: match query.error.as_deref() {
            Some("link") => Some("That is not a web address. It must start with http:// or https://."),
            Some("many") => Some("That is the most links that can be added in a day."),
            _ => None,
        },
        pending: Vec::new(),
        fetching: false,
        failed: false,
        rows: Vec::new(),
        count: None,
        back: address(&parts, number),
        newer: (number > 1).then(|| address(&parts, number - 1)),
        older: None,
        search: search.clone(),
        from: from.clone(),
        to: to.clone(),
    };
    page.pending = db::unfinished_adds(pool, &user.id)
        .await?
        .into_iter()
        .map(|add| Pending {
            failed: add.status == "failed",
            note: if add.status == "failed" {
                format!("Could not be added: {}", add.error)
            } else {
                "Being fetched; it appears here within a minute or two.".to_owned()
            },
            url: add.url,
        })
        .collect();
    page.fetching = page.pending.iter().any(|add| !add.failed);
    page.failed = page.pending.iter().any(|add| add.failed);
    if !ready {
        return html(StatusCode::OK, page);
    }
    let shelf = Shelf {
        user: &user.id,
        show,
        title: &search,
        via: &via,
        route,
        rating,
        lasting,
        from: &from,
        to: &to,
        sort,
        // One more than a page, to know whether an older page exists.
        limit: PER_PAGE + 1,
        offset: (number - 1) * PER_PAGE,
    };
    let mut found = db::shelved(pool, &shelf).await?;
    if found.len() as i64 > PER_PAGE {
        found.truncate(PER_PAGE as usize);
        page.older = Some(address(&parts, number + 1));
    }
    page.count = matching(db::shelved_count(pool, &shelf).await?, shelf.offset, found.len());
    let can_send = !user.kindle_addresses.trim().is_empty();
    let requests = db::send_states(pool, &user.id).await?;
    let refreshes = db::refresh_states(pool, &user.id).await?;
    page.rows = found
        .into_iter()
        .map(|found| {
            let mut row = row(found, zone);
            let last = requests.iter().rev().find(|request| !request.story && request.article_id == row.id);
            row.sendable = can_send && last.is_none_or(|request| request.status != "queued");
            row.sent = last.map(|request| send_outcome(request, zone));
            if let Some(refresh) = refreshes.iter().rev().find(|refresh| refresh.article_id == row.id) {
                row.refreshing = refresh.status == "queued";
                row.refreshed = Some(refresh_outcome(refresh, zone));
            }
            row
        })
        .collect();
    page.fetching |= page.rows.iter().any(|row| row.refreshing);
    html(StatusCode::OK, page)
}

fn refresh_outcome(refresh: &db::RefreshState, zone: Tz) -> String {
    match refresh.status.as_str() {
        "queued" => "Being fetched again; this page updates within a minute or two.".to_owned(),
        "done" => {
            let at = refresh.done_at.as_deref().and_then(|at| local(at, zone));
            format!("Fetched again{}.", at.map(|at| at.format(" at %H:%M").to_string()).unwrap_or_default())
        }
        _ => format!("Could not be fetched again: {}", refresh.error),
    }
}

#[derive(Deserialize)]
pub struct RefreshForm {
    csrf: String,
    article: String,
    back: Option<String>,
}

/// Fetch an article's page again and extract it anew: after signing in to a site, or once the
/// extraction has been improved. This only queues it; the pipeline does the fetching.
pub async fn refresh(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<RefreshForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let pool = &state.pool;
    if !db::pipeline_ready(pool).await? || !db::in_library(pool, &user.id, &form.article).await? {
        return refused(StatusCode::NOT_FOUND, "No such article.");
    }
    match db::queue_refresh(pool, &user.id, &form.article).await? {
        db::Queued::TooMany => {
            refused(StatusCode::TOO_MANY_REQUESTS, "That is the most articles that can be fetched again in a day.")
        }
        db::Queued::Added | db::Queued::Waiting => back_to(&form.back, &form.article),
    }
}

#[derive(Deserialize)]
pub struct AddForm {
    csrf: String,
    link: String,
    /// next, now or keep.
    then: String,
    rating: Option<String>,
}

/// Add a link by hand. This only queues it: the pipeline fetches the page, as it does every
/// article, and the link is never opened from here.
#[derive(Deserialize)]
pub struct ClearForm {
    csrf: String,
}

/// Clear the links that could not be added from the page.
pub async fn clear_failed(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<ClearForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    db::dismiss_failed_adds(&state.pool, &user.id).await?;
    Ok(Redirect::to("/archive?show=added").into_response())
}

pub async fn add(
    State(state): State<AppState>,
    auth: AuthSession,
    session: Session,
    headers: HeaderMap,
    Form(form): Form<AddForm>,
) -> Page {
    let user = reader(&auth)?;
    if !allowed(&headers, &session, &form.csrf).await? {
        return refused(StatusCode::FORBIDDEN, NOT_FROM_HERE);
    }
    let Some(action) = ["next", "now", "keep"].into_iter().find(|action| *action == form.then) else {
        return refused(StatusCode::BAD_REQUEST, "Choose what to do with the link.");
    };
    let Some(link) = web_url(&form.link).filter(|link| link.len() <= MAX_URL) else {
        return Ok(Redirect::to("/archive?show=added&error=link").into_response());
    };
    if action != "keep" && user.kindle_addresses.trim().is_empty() {
        return refused(StatusCode::BAD_REQUEST, "Your account has no Kindle address; an admin sets it.");
    }
    // A rating belongs to something already read.
    let rating = form.rating.as_deref().and_then(|n| n.parse::<i64>().ok()).filter(|n| (1..=5).contains(n));
    let rating = if action == "keep" { rating } else { None };
    if !db::queue_add(&state.pool, &user.id, &link, action, rating).await? {
        return Ok(Redirect::to("/archive?show=added&error=many").into_response());
    }
    Ok(Redirect::to("/archive?show=added").into_response())
}
