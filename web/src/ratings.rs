//! Rating signals and the reader's note, matching `app/tokens.py`.
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

/// The longest note kept, in characters.
pub const MAX_NOTE: usize = 200;

pub fn verdict_label(key: &str) -> Option<&'static str> {
    VERDICTS.iter().find(|(k, _)| *k == key).map(|(_, label)| *label)
}

pub fn progress_label(key: &str) -> Option<&'static str> {
    PROGRESS.iter().find(|(k, _)| *k == key).map(|(_, label)| *label)
}

/// A note as kept, by the same rule as `app/tokens.py`: one line, at most `MAX_NOTE` characters.
pub fn clean_note(text: &str) -> String {
    let line = text.split_whitespace().collect::<Vec<_>>().join(" ");
    line.chars().take(MAX_NOTE).collect::<String>().trim().to_owned()
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
        }
    }
}

pub fn verdict_key(text: &str) -> Option<&'static str> {
    VERDICTS.iter().find(|(k, _)| *k == text).map(|(k, _)| *k)
}

/// The signals a rating link carries, in the words `Display` writes, and nothing more: a verdict
/// that does not exist is not one.
impl FromStr for Signal {
    type Err = ();

    fn from_str(text: &str) -> Result<Signal, ()> {
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
        let all = VERDICTS.iter().flat_map(|(v, _)| [Signal::Verdict(v), Signal::End(v)]).chain([
            Signal::Finished,
            Signal::Partway,
            Signal::Skip,
        ]);
        for signal in all {
            assert_eq!(signal.to_string().parse::<Signal>(), Ok(signal.clone()), "{signal}");
        }
        assert_eq!("end-more".parse::<Signal>(), Ok(Signal::End("more")));
        for text in ["", "f4", "r2", "end-", "end-great", "great", "done", "why-thin", "note", "problem", "skip-rest"] {
            assert_eq!(text.parse::<Signal>(), Err(()), "{text}");
        }
    }

    #[test]
    fn a_note_is_one_short_line() {
        assert_eq!(clean_note("  one \n  line\t "), "one line");
        assert_eq!(clean_note(&"é".repeat(300)).chars().count(), MAX_NOTE);
        assert_eq!(clean_note("   "), "");
    }
}
