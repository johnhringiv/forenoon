//! Rating signals and the reasons a reader can give, matching `app/tokens.py`.
//!
//! A rating is two facts: what the article was worth to the reader (the verdict) and how far
//! they got (the progress). Either can be given without the other.

use std::{fmt, str::FromStr};

/// (key, label), in the order they are offered.
pub const VERDICTS: [(&str, &str); 4] =
    [("not-for-me", "Not for me"), ("not-good", "Not good"), ("worth-it", "Worth it"), ("more", "More like this")];
/// (key, label) for how far the reader got.
pub const PROGRESS: [(&str, &str); 3] =
    [("finished", "Finished"), ("partway", "Stopped partway"), ("skipped", "Skipped")];

/// "Not for me" is about the subject, so its reasons are about the reader's level.
const NOT_FOR_ME: [(&str, &str); 2] = [("basic", "Too basic for me"), ("specialist", "Too specialised for me")];
const NOT_GOOD: [(&str, &str); 5] = [
    ("thin", "Too thin"),
    ("known", "Nothing new in it"),
    ("padded", "Longer than it needed to be"),
    ("oversold", "The headline oversold it"),
    ("wrong", "Wrong or misleading"),
];
const MORE: [(&str, &str); 3] = [
    ("more-topic", "More on this topic"),
    ("more-source", "More from this source"),
    ("depth", "Liked the depth or format"),
];

pub fn verdict_label(key: &str) -> Option<&'static str> {
    VERDICTS.iter().find(|(k, _)| *k == key).map(|(_, label)| *label)
}

pub fn progress_label(key: &str) -> Option<&'static str> {
    PROGRESS.iter().find(|(k, _)| *k == key).map(|(_, label)| *label)
}

/// The reasons that fit a verdict; none for "worth it", and none without a verdict: a skip is
/// a deliberate act and says enough, and an article already seen elsewhere can be rated.
pub fn reasons_for(verdict: Option<&str>) -> &'static [(&'static str, &'static str)] {
    match verdict {
        Some("not-for-me") => &NOT_FOR_ME,
        Some("not-good") => &NOT_GOOD,
        Some("more") => &MORE,
        _ => &[],
    }
}

/// What was liked can be several things at once; anything else is one.
pub fn several(verdict: Option<&str>) -> bool {
    verdict == Some("more")
}

/// The reason keys stored for a rating: what was liked can be several things at once.
pub fn chosen(reason: &str) -> impl Iterator<Item = &str> {
    reason.split(',').filter(|key| !key.is_empty())
}

/// The stored reasons after one more press of `key`, by the same rules as `app/tokens.py`.
///
/// Pressing a chosen reason again takes it back. What was liked adds up (the topic and the
/// depth); anything else is one thing, so a new choice replaces the old. Reasons that do not
/// fit the rating are dropped; an empty `key` only does that.
pub fn with_reason(reason: &str, key: &str, verdict: Option<&str>) -> String {
    let fitting = reasons_for(verdict);
    let fits = |k: &str| fitting.iter().any(|(f, _)| *f == k);
    let mut now: Vec<&str> = chosen(reason).filter(|k| fits(k)).collect();
    if now.contains(&key) {
        now.retain(|k| *k != key);
    } else if fits(key) {
        if !several(verdict) {
            now.clear();
        }
        now.push(key);
    }
    fitting.iter().map(|(k, _)| *k).filter(|k| now.contains(k)).collect::<Vec<_>>().join(",")
}

pub fn fits(reason: &str, verdict: Option<&str>) -> bool {
    reasons_for(verdict).iter().any(|(key, _)| *key == reason)
}

/// What the reason buttons are asked under: they read as answers to it.
pub fn question(verdict: Option<&str>) -> &'static str {
    match verdict {
        Some("not-for-me") => "Why not for you?",
        Some("more") => "What did you like?",
        _ => "What was wrong?",
    }
}

/// A reason as a short answer to `question`, for a button.
pub fn short(reason: &str) -> &'static str {
    match reason {
        "basic" => "Too basic",
        "specialist" => "Too specialised",
        "known" => "Nothing new",
        "thin" => "Too thin",
        "padded" => "Padded",
        "oversold" => "Oversold",
        "wrong" => "Inaccurate",
        "more-topic" => "The topic",
        "more-source" => "This source",
        "depth" => "The depth",
        _ => "",
    }
}

pub fn label(reason: &str) -> Option<&'static str> {
    NOT_FOR_ME.iter().chain(&NOT_GOOD).chain(&MORE).find(|(key, _)| *key == reason).map(|(_, l)| *l)
}

/// What gets written to the event log, in the same words the Kindle links use.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Signal {
    /// A verdict; progress stays as it was, but a skipped article becomes not known: `worth-it`.
    Verdict(&'static str),
    /// The links at an article's end: the verdict, and finished: `end-worth-it`.
    End(&'static str),
    /// Progress only: `finished`.
    Finished,
    /// Progress only: `partway`.
    Partway,
    /// Not read: drops the verdict: `skip`.
    Skip,
    /// Why: `why-thin`.
    Why(String),
}

/// What a signal does to one fact: leave it as it was, or set it (`None` clears it).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Change {
    Keep,
    Set(Option<&'static str>),
}

impl Signal {
    /// (verdict, progress) this signal sets. A verdict on a skipped article also makes its
    /// progress not known; the caller sees to that, as it depends on what is stored.
    pub fn meaning(&self) -> (Change, Change) {
        match self {
            Signal::Verdict(v) => (Change::Set(Some(v)), Change::Keep),
            Signal::End(v) => (Change::Set(Some(v)), Change::Set(Some("finished"))),
            Signal::Finished => (Change::Keep, Change::Set(Some("finished"))),
            Signal::Partway => (Change::Keep, Change::Set(Some("partway"))),
            Signal::Skip => (Change::Set(None), Change::Set(Some("skipped"))),
            Signal::Why(_) => (Change::Keep, Change::Keep),
        }
    }
}

impl fmt::Display for Signal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Signal::Verdict(v) => f.write_str(v),
            Signal::End(v) => write!(f, "end-{v}"),
            Signal::Finished => f.write_str("finished"),
            Signal::Partway => f.write_str("partway"),
            Signal::Skip => f.write_str("skip"),
            Signal::Why(reason) => write!(f, "why-{reason}"),
        }
    }
}

pub fn verdict_key(text: &str) -> Option<&'static str> {
    VERDICTS.iter().find(|(k, _)| *k == text).map(|(k, _)| *k)
}

/// The signals a rating link carries, in the words `Display` writes, and nothing more: a
/// reason nobody can give is not one, nor a verdict that does not exist.
impl FromStr for Signal {
    type Err = ();

    fn from_str(text: &str) -> Result<Signal, ()> {
        if let Some(reason) = text.strip_prefix("why-") {
            return label(reason).map(|_| Signal::Why(reason.to_owned())).ok_or(());
        }
        if let Some(verdict) = text.strip_prefix("end-") {
            return verdict_key(verdict).map(Signal::End).ok_or(());
        }
        match text {
            "finished" => Ok(Signal::Finished),
            "partway" => Ok(Signal::Partway),
            "skip" => Ok(Signal::Skip),
            _ => verdict_key(text).map(Signal::Verdict).ok_or(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_signal_reads_back_as_it_was_written() {
        let all = VERDICTS
            .iter()
            .flat_map(|(v, _)| [Signal::Verdict(v), Signal::End(v)])
            .chain([Signal::Finished, Signal::Partway, Signal::Skip, Signal::Why("thin".into())])
            .chain([Signal::Why("depth".into()), Signal::Why("specialist".into())]);
        for signal in all {
            assert_eq!(signal.to_string().parse::<Signal>(), Ok(signal.clone()), "{signal}");
        }
        assert_eq!("end-more".parse::<Signal>(), Ok(Signal::End("more")));
        for text in [
            "",
            "f4",
            "r2",
            "end-",
            "end-great",
            "great",
            "done",
            "why-",
            "why-broken",
            "why-topic",
            "why-no-time",
            "why-seen",
            "problem",
        ] {
            assert_eq!(text.parse::<Signal>(), Err(()), "{text}");
        }
    }

    #[test]
    fn reasons_follow_the_verdict() {
        assert!(reasons_for(Some("worth-it")).is_empty() && reasons_for(None).is_empty());
        assert!(fits("basic", Some("not-for-me")) && !fits("basic", Some("not-good")));
        // One reason, except for what was liked.
        assert_eq!(with_reason("thin", "padded", Some("not-good")), "padded");
        assert_eq!(with_reason("depth", "more-topic", Some("more")), "more-topic,depth");
        assert_eq!(with_reason("depth", "depth", Some("more")), "");
        assert_eq!(with_reason("thin,depth", "", Some("more")), "depth");
    }
}
