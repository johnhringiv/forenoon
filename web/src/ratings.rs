//! Rating signals and the reasons a reader can give, matching `app/tokens.py`.

use std::fmt;

/// Offered after a rating of 1 to 3.
pub const LOW_REASONS: [(&str, &str); 5] = [
    ("topic", "Not interested in the topic"),
    ("thin", "Too thin, nothing in it"),
    ("known", "Already knew this"),
    ("long", "Too long"),
    ("wrong", "Wrong or misleading"),
];
/// Offered after a rating of 4 or 5.
pub const HIGH_REASONS: [(&str, &str); 3] =
    [("more-topic", "More on this topic"), ("more-source", "More from this source"), ("depth", "Liked the depth")];

/// The reasons that fit a rating: one list for 4 and 5, the other for the rest.
pub fn reasons_for(rating: Option<i64>) -> &'static [(&'static str, &'static str)] {
    if rating.is_some_and(|n| n >= 4) { &HIGH_REASONS } else { &LOW_REASONS }
}

pub fn fits(reason: &str, rating: Option<i64>) -> bool {
    reasons_for(rating).iter().any(|(key, _)| *key == reason)
}

/// A one or two word form of a reason, for a button.
pub fn short(reason: &str) -> &'static str {
    match reason {
        "topic" => "Not my topic",
        "thin" => "Thin",
        "known" => "Knew it",
        "long" => "Too long",
        "wrong" => "Wrong",
        "more-topic" => "More like this",
        "more-source" => "More from source",
        "depth" => "Depth",
        _ => "",
    }
}

pub fn label(reason: &str) -> Option<&'static str> {
    LOW_REASONS.iter().chain(&HIGH_REASONS).find(|(key, _)| *key == reason).map(|(_, label)| *label)
}

/// What gets written to the event log, in the same words the Kindle links use.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Signal {
    /// A rating of 1 to 5 and whether the article was finished: `f4`, `r2`.
    Rated { rating: i64, finished: bool },
    /// Finished, with no rating: `done`.
    Done,
    /// Why the rating was given: `why-thin`.
    Why(String),
}

impl Signal {
    /// (rating, finished) this signal sets; `None` leaves that fact as it was.
    pub fn meaning(&self) -> (Option<i64>, Option<bool>) {
        match self {
            Signal::Rated { rating, finished } => (Some(*rating), Some(*finished)),
            Signal::Done => (None, Some(true)),
            Signal::Why(_) => (None, None),
        }
    }
}

impl fmt::Display for Signal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Signal::Rated { rating, finished } => write!(f, "{}{rating}", if *finished { 'f' } else { 'r' }),
            Signal::Done => f.write_str("done"),
            Signal::Why(reason) => write!(f, "why-{reason}"),
        }
    }
}
