//! Rating signals and the reader's note, matching `app/tokens.py`.

use std::{fmt, str::FromStr};

/// The longest note kept, in characters.
pub const MAX_NOTE: usize = 200;

/// A note as kept, by the same rule as `app/tokens.py`: one line, at most `MAX_NOTE` characters.
pub fn clean_note(text: &str) -> String {
    let line = text.split_whitespace().collect::<Vec<_>>().join(" ");
    line.chars().take(MAX_NOTE).collect::<String>().trim().to_owned()
}

/// What gets written to the event log, in the same words the Kindle links use.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Signal {
    /// Rated 1 to 5: `r4`.
    Rated(i64),
    /// Rated 1 to 5 by a book sent before October 2026, when the links came in two rows and
    /// this one also said the article was finished: `f4`. Finished is no longer kept; the
    /// rating is. Answered while those books' links last, never made for a new one.
    Finished(i64),
}

impl Signal {
    pub fn rating(&self) -> i64 {
        match self {
            Signal::Rated(n) | Signal::Finished(n) => *n,
        }
    }
}

impl fmt::Display for Signal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Signal::Rated(n) => write!(f, "r{n}"),
            Signal::Finished(n) => write!(f, "f{n}"),
        }
    }
}

/// The signals a rating link carries, in the words `Display` writes, and nothing more.
impl FromStr for Signal {
    type Err = ();

    fn from_str(text: &str) -> Result<Signal, ()> {
        let (kind, n) = text.split_at_checked(1).ok_or(())?;
        let n = n.parse::<i64>().map_err(|_| ())?;
        if !(1..=5).contains(&n) || n.to_string() != text[1..] {
            return Err(());
        }
        match kind {
            "r" => Ok(Signal::Rated(n)),
            "f" => Ok(Signal::Finished(n)),
            _ => Err(()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_signal_reads_back_as_it_was_written() {
        for n in 1..=5 {
            for signal in [Signal::Rated(n), Signal::Finished(n)] {
                assert_eq!(signal.to_string().parse::<Signal>(), Ok(signal.clone()), "{signal}");
                assert_eq!(signal.rating(), n);
            }
        }
        assert_eq!("r4".parse::<Signal>(), Ok(Signal::Rated(4)));
        for text in ["", "r", "r0", "r6", "r04", "r+4", "x3", "done", "end-more", "worth-it", "skip", "note", "problem"]
        {
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
