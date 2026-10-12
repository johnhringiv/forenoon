//! A small in-memory limit on sign-in attempts, per client address and per account name.

use std::{
    collections::{HashMap, VecDeque},
    sync::Mutex,
    time::{Duration, Instant},
};

const MAX_TRACKED: usize = 10_000;

pub struct Limiter {
    window: Duration,
    hits: Mutex<HashMap<String, VecDeque<Instant>>>,
}

impl Limiter {
    pub fn new(window: Duration) -> Self {
        Self { window, hits: Mutex::new(HashMap::new()) }
    }

    /// Count one attempt for this key; false when it has already had `max` in the window.
    ///
    /// A refused attempt is not counted, so a key's memory is bounded and its block ends
    /// once the counted attempts age out, however many more arrive meanwhile.
    pub fn allow(&self, key: &str, max: usize) -> bool {
        let now = Instant::now();
        let mut hits = self.hits.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if hits.len() >= MAX_TRACKED && !hits.contains_key(key) {
            // Make room by forgetting only keys whose attempts have all aged out. Live ones
            // stay, or a flood of new keys would wipe the count of a blocked one.
            hits.retain(|_, window| window.back().is_some_and(|at| now.duration_since(*at) <= self.window));
            if hits.len() >= MAX_TRACKED {
                return false;
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
}
