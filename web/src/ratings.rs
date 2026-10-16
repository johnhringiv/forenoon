//! Rating signals and the reasons a reader can give, matching `app/tokens.py`.

use std::{fmt, str::FromStr};

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

/// The reason keys stored for a rating: what was liked can be several things at once.
pub fn chosen(reason: &str) -> impl Iterator<Item = &str> {
    reason.split(',').filter(|key| !key.is_empty())
}

/// The stored reasons after one more press of `key`, by the same rules as `app/tokens.py`.
///
/// Pressing a chosen reason again takes it back. What was liked adds up (the topic and the
/// depth); what was wrong is one thing, so a new choice replaces the old. Reasons that do not
/// fit the rating are dropped; an empty `key` only does that.
pub fn with_reason(reason: &str, key: &str, rating: Option<i64>) -> String {
    let fitting = reasons_for(rating);
    let several = rating.is_some_and(|n| n >= 4);
    let mut now: Vec<&str> = chosen(reason).filter(|k| fits(k, rating)).collect();
    if now.contains(&key) {
        now.retain(|k| *k != key);
    } else if fits(key, rating) {
        if !several {
            now.clear();
        }
        now.push(key);
    }
    fitting.iter().map(|(k, _)| *k).filter(|k| now.contains(k)).collect::<Vec<_>>().join(",")
}

pub fn fits(reason: &str, rating: Option<i64>) -> bool {
    reasons_for(rating).iter().any(|(key, _)| *key == reason)
}

/// What the reason buttons are asked under: they read as answers to it.
pub fn question(rating: Option<i64>) -> &'static str {
    if rating.is_some_and(|n| n >= 4) { "What did you like?" } else { "What was wrong?" }
}

/// A reason as a short answer to `question`, for a button.
pub fn short(reason: &str) -> &'static str {
    match reason {
        "topic" => "The topic",
        "thin" => "Too thin",
        "known" => "Nothing new",
        "long" => "Too long",
        "wrong" => "Inaccurate",
        "more-topic" => "The topic",
        "more-source" => "This source",
        "depth" => "The depth",
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

/// The reverse of `Display`, and nothing more: `f+3` and `f03` are not signals, nor is a
/// reason nobody can give.
impl FromStr for Signal {
    type Err = ();

    fn from_str(text: &str) -> Result<Signal, ()> {
        if text == "done" {
            return Ok(Signal::Done);
        }
        if let Some(reason) = text.strip_prefix("why-") {
            return label(reason).map(|_| Signal::Why(reason.to_owned())).ok_or(());
        }
        let (kind, digit) = text.split_at_checked(1).ok_or(())?;
        let finished = match kind {
            "f" => true,
            "r" => false,
            _ => return Err(()),
        };
        let rating = match digit {
            "1" | "2" | "3" | "4" | "5" => digit.parse().expect("a digit"),
            _ => return Err(()),
        };
        Ok(Signal::Rated { rating, finished })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_signal_reads_back_as_it_was_written() {
        let all = [Signal::Rated { rating: 1, finished: true }, Signal::Rated { rating: 5, finished: false }];
        for signal in all.into_iter().chain([Signal::Done, Signal::Why("thin".into()), Signal::Why("depth".into())]) {
            assert_eq!(signal.to_string().parse::<Signal>(), Ok(signal.clone()), "{signal}");
        }
        assert_eq!("f4".parse::<Signal>(), Ok(Signal::Rated { rating: 4, finished: true }));
        assert_eq!("r2".parse::<Signal>(), Ok(Signal::Rated { rating: 2, finished: false }));
        for text in ["", "f", "f0", "f6", "f+3", "f03", "x3", "up", "down", "why-", "why-broken", "problem"] {
            assert_eq!(text.parse::<Signal>(), Err(()), "{text}");
        }
    }
}
