//! A small in-memory limit on attempts: sign-ins per client address and per account name,
//! account emails per account, and rating taps per client address.

use std::{
    collections::{HashMap, VecDeque},
    sync::Mutex,
    time::{Duration, Instant},
};

const MAX_TRACKED: usize = 10_000;

/// What becomes of a key not seen before when the table is full of live ones.
#[derive(Clone, Copy, Debug)]
enum WhenFull {
    /// It is refused. For sign-in: a flood of new keys must not lift a block.
    Refuse,
    /// The key least recently seen is forgotten. For the rating links, which are public: a
    /// flood of addresses must not stop a reader's Kindle, which arrives from an address not
    /// seen before, and a block lifted that way costs only a few more taps.
    EvictOldest,
}

pub struct Limiter {
    window: Duration,
    when_full: WhenFull,
    hits: Mutex<HashMap<String, VecDeque<Instant>>>,
}

impl Limiter {
    /// A limiter that refuses new keys while the table is full.
    pub fn new(window: Duration) -> Self {
        Self { window, when_full: WhenFull::Refuse, hits: Mutex::new(HashMap::new()) }
    }

    /// A limiter that forgets the key least recently seen to make room for a new one.
    pub fn evicting(window: Duration) -> Self {
        Self { when_full: WhenFull::EvictOldest, ..Self::new(window) }
    }

    /// Count one attempt for this key; false when it has already had `max` in the window.
    ///
    /// A refused attempt is not counted, so a key's memory is bounded and its block ends
    /// once the counted attempts age out, however many more arrive meanwhile.
    pub fn allow(&self, key: &str, max: usize) -> bool {
        let now = Instant::now();
        let mut hits = self.hits.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if hits.len() >= MAX_TRACKED && !hits.contains_key(key) {
            // Make room by forgetting keys whose attempts have all aged out. Live ones stay,
            // or a flood of new keys would wipe the count of a blocked one; a limiter that
            // evicts gives up the one longest unseen instead.
            hits.retain(|_, window| window.back().is_some_and(|at| now.duration_since(*at) <= self.window));
            if hits.len() >= MAX_TRACKED {
                match self.when_full {
                    WhenFull::Refuse => return false,
                    WhenFull::EvictOldest => {
                        let oldest =
                            hits.iter().min_by_key(|(_, window)| window.back().copied()).map(|(k, _)| k.clone());
                        if let Some(oldest) = oldest {
                            hits.remove(&oldest);
                        }
                    }
                }
            }
        }
        let window = hits.entry(key.to_owned()).or_default();
        while window.front().is_some_and(|at| now.duration_since(*at) > self.window) {
            window.pop_front();
        }
        if window.len() >= max {
            return false;
        }
        window.push_back(now);
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allows_up_to_the_limit_per_key() {
        let limiter = Limiter::new(Duration::from_secs(60));
        assert!(limiter.allow("a", 2) && limiter.allow("a", 2));
        assert!(!limiter.allow("a", 2));
        assert!(limiter.allow("b", 2));
    }

    #[test]
    fn a_flood_of_other_keys_does_not_lift_a_block() {
        let limiter = Limiter::new(Duration::from_secs(60));
        assert!(limiter.allow("target", 1));
        for n in 0..MAX_TRACKED + 10 {
            limiter.allow(&format!("other {n}"), 1);
        }
        assert!(!limiter.allow("target", 1));
        assert!(!limiter.allow("one more new key", 1), "a full table refuses new keys");
    }

    #[test]
    fn an_evicting_limiter_makes_room_for_a_new_key_by_forgetting_the_oldest() {
        let limiter = Limiter::evicting(Duration::from_secs(60));
        assert!(limiter.allow("first", 1));
        for n in 0..MAX_TRACKED - 1 {
            assert!(limiter.allow(&format!("other {n}"), 1));
        }
        // Full of live keys: the new one still gets through, at the cost of the oldest.
        assert!(limiter.allow("new", 1), "a new address is never refused for others' sake");
        assert!(limiter.allow("first", 1), "the oldest key was forgotten, so it counts afresh");
        assert!(limiter.hits.lock().unwrap().len() <= MAX_TRACKED, "the table stays bounded");
        assert!(!limiter.allow("new", 1), "a recent key keeps its count");
    }
}
