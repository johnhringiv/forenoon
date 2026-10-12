//! A small in-memory limit on sign-in attempts, per client address and per account name.

use std::{
    collections::{HashMap, VecDeque},
    sync::Mutex,
    time::{Duration, Instant},
};

const MAX_TRACKED: usize = 10_000;

pub struct Limiter {
    max: usize,
    window: Duration,
    hits: Mutex<HashMap<String, VecDeque<Instant>>>,
}

impl Limiter {
    pub fn new(max: usize, window: Duration) -> Self {
        Self { max, window, hits: Mutex::new(HashMap::new()) }
    }

    /// Count one attempt for this key; false when it has had too many in the window.
    pub fn allow(&self, key: &str) -> bool {
        let now = Instant::now();
        let mut hits = self.hits.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if hits.len() > MAX_TRACKED {
            hits.clear();
        }
        let window = hits.entry(key.to_owned()).or_default();
        while window.front().is_some_and(|at| now.duration_since(*at) > self.window) {
            window.pop_front();
        }
        window.push_back(now);
        window.len() <= self.max
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn allows_up_to_the_limit_per_key() {
        let limiter = Limiter::new(2, Duration::from_secs(60));
        assert!(limiter.allow("a") && limiter.allow("a"));
        assert!(!limiter.allow("a"));
        assert!(limiter.allow("b"));
    }
}
