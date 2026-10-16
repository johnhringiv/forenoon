//! Who a request is from, for the limits on sign-in attempts and rating taps.
//!
//! Behind a reverse proxy or a tunnel every request arrives from the proxy, so the limits would
//! be one bucket shared by everyone. A proxy says who the client was in a header; that header is
//! believed only when the request came from an address the install trusts, since anyone can
//! send it.

use std::net::IpAddr;

use axum::http::{HeaderMap, HeaderName};

#[derive(Clone, Debug, Default)]
pub struct Proxy {
    /// The header the proxy puts the client's address in: `CF-Connecting-IP` for a Cloudflare
    /// tunnel, `X-Forwarded-For` or `X-Real-IP` for most others. `None`: none is believed.
    header: Option<HeaderName>,
    /// Networks the header is believed from, as (address, prefix length).
    trusted: Vec<(IpAddr, u8)>,
}

impl Proxy {
    /// `trusted` is a comma-separated list of addresses and networks (`127.0.0.1`, `10.0.0.0/8`).
    pub fn new(header: &str, trusted: &str) -> Result<Proxy, String> {
        let header = header.trim();
        let header = if header.is_empty() {
            None
        } else {
            Some(HeaderName::try_from(header).map_err(|_| format!("{header} is not a header name"))?)
        };
        let trusted =
            trusted.split(',').map(str::trim).filter(|part| !part.is_empty()).map(network).collect::<Result<_, _>>()?;
        Ok(Proxy { header, trusted })
    }

    /// The client's address: the proxy's word for it when `peer` is trusted, `peer` otherwise.
    pub fn client(&self, peer: IpAddr, headers: &HeaderMap) -> IpAddr {
        let peer = peer.to_canonical();
        let Some(header) = &self.header else { return peer };
        if !self.trusted.iter().any(|&(net, bits)| contains(net, bits, peer)) {
            return peer;
        }
        // X-Forwarded-For is a list that the client can start: the last entry is the one the
        // trusted proxy added. Single-address headers are their own last entry.
        let named = headers.get_all(header).iter().filter_map(|value| value.to_str().ok()).next_back();
        named
            .and_then(|value| value.rsplit(',').next())
            .and_then(|last| last.trim().parse::<IpAddr>().ok())
            .map_or(peer, |ip| ip.to_canonical())
    }
}

fn network(text: &str) -> Result<(IpAddr, u8), String> {
    let invalid = || format!("{text} is not an address or a network");
    let (address, bits) = text.split_once('/').map_or((text, None), |(a, b)| (a, Some(b)));
    let address = address.parse::<IpAddr>().map_err(|_| invalid())?.to_canonical();
    let most = if address.is_ipv4() { 32 } else { 128 };
    let bits = match bits {
        Some(bits) => bits.parse::<u8>().ok().filter(|&n| n <= most).ok_or_else(invalid)?,
        None => most,
    };
    Ok((address, bits))
}

fn contains(net: IpAddr, bits: u8, ip: IpAddr) -> bool {
    let (net, ip, width) = match (net, ip) {
        (IpAddr::V4(a), IpAddr::V4(b)) => (u128::from(u32::from(a)), u128::from(u32::from(b)), 32),
        (IpAddr::V6(a), IpAddr::V6(b)) => (u128::from(a), u128::from(b), 128),
        _ => return false,
    };
    let shift = width - u32::from(bits);
    shift >= width || net >> shift == ip >> shift
}

#[cfg(test)]
mod tests {
    use super::*;

    fn headers(pairs: &[(&str, &str)]) -> HeaderMap {
        pairs.iter().map(|(k, v)| (HeaderName::try_from(*k).unwrap(), v.parse().unwrap())).collect()
    }

    fn ip(text: &str) -> IpAddr {
        text.parse().unwrap()
    }

    #[test]
    fn believes_the_header_only_from_a_trusted_proxy() {
        let tunnel = Proxy::new("CF-Connecting-IP", "127.0.0.1, ::1").unwrap();
        let sent = headers(&[("cf-connecting-ip", "203.0.113.9")]);
        assert_eq!(tunnel.client(ip("127.0.0.1"), &sent), ip("203.0.113.9"));
        assert_eq!(tunnel.client(ip("::ffff:127.0.0.1"), &sent), ip("203.0.113.9"));
        assert_eq!(tunnel.client(ip("192.168.1.5"), &sent), ip("192.168.1.5"), "not from the proxy");
        assert_eq!(tunnel.client(ip("127.0.0.1"), &HeaderMap::new()), ip("127.0.0.1"));
        let bad = headers(&[("cf-connecting-ip", "not an address")]);
        assert_eq!(tunnel.client(ip("127.0.0.1"), &bad), ip("127.0.0.1"));
        // No header configured: nothing is believed, from anywhere.
        assert_eq!(Proxy::new("", "127.0.0.1").unwrap().client(ip("127.0.0.1"), &sent), ip("127.0.0.1"));
    }

    #[test]
    fn takes_the_address_the_proxy_added_to_a_list() {
        let proxy = Proxy::new("X-Forwarded-For", "10.0.0.0/8").unwrap();
        // The client wrote the first entry itself; the proxy at 10.1.2.3 added the last.
        let sent = headers(&[("x-forwarded-for", "1.1.1.1, 198.51.100.7")]);
        assert_eq!(proxy.client(ip("10.1.2.3"), &sent), ip("198.51.100.7"));
        assert_eq!(proxy.client(ip("11.0.0.1"), &sent), ip("11.0.0.1"));
    }

    #[test]
    fn reads_networks() {
        assert!(Proxy::new("X-Real-IP", "fd00::/8, 172.16.0.0/12, 0.0.0.0/0").is_ok());
        for bad in ["10.0.0.0/33", "nas", "10.0.0.1/x"] {
            assert!(Proxy::new("X-Real-IP", bad).is_err(), "{bad}");
        }
        assert!(Proxy::new("not a header", "").is_err());
        assert!(contains(ip("0.0.0.0"), 0, ip("8.8.8.8")));
        assert!(!contains(ip("172.16.0.0"), 12, ip("172.32.0.1")));
    }
}
