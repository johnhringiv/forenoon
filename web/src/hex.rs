//! Bytes as lowercase hex digits, and back.

pub(crate) fn encode(bytes: &[u8]) -> String {
    bytes.iter().map(|byte| format!("{byte:02x}")).collect()
}

/// `None` unless `text` is an even number of hex digits.
pub(crate) fn decode(text: &str) -> Option<Vec<u8>> {
    let digits = text.as_bytes();
    if !digits.len().is_multiple_of(2) {
        return None;
    }
    digits.chunks(2).map(|pair| u8::from_str_radix(std::str::from_utf8(pair).ok()?, 16).ok()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_and_refuses_what_is_not_hex() {
        assert_eq!(encode(&[0, 15, 16, 255]), "000f10ff");
        assert_eq!(decode("000f10ff"), Some(vec![0, 15, 16, 255]));
        assert_eq!(decode(""), Some(Vec::new()));
        assert_eq!(decode("abc"), None, "odd length");
        assert_eq!(decode("zz"), None);
        assert_eq!(decode("é0"), None, "two bytes, not two digits");
    }
}
