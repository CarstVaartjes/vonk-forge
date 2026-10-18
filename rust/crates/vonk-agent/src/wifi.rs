//! Read-only nl80211 evidence for the wireless NAS route. GET_INTERFACE and
//! GET_STATION require no CAP_NET_ADMIN (Linux net/wireless/nl80211.c).
//! No subprocess, privileges, cache or retry loop: each inventory probes anew.

use vonk_agent_protocol::generated::NetworkInterface;

#[cfg(any(target_os = "linux", test))]
mod attributes {
    // Stable Linux UAPI: include/uapi/linux/nl80211.h.
    pub const IFINDEX: u16 = 3;
    pub const IFTYPE: u16 = 5;
    const IFTYPE_STATION: u32 = 2;
    pub const STA_INFO: u16 = 21;
    pub const WIPHY_FREQ: u16 = 38;
    const SIGNAL: u16 = 7;
    const TX_BITRATE: u16 = 8;
    const RX_BITRATE: u16 = 14;
    const BITRATE: u16 = 1;
    const BITRATE32: u16 = 5;

    /// Netlink attributes have native-endian headers and four-byte alignment.
    /// An unknown attribute is ignored; a truncated stream is never evidence.
    pub fn parse(mut bytes: &[u8]) -> Option<Vec<(u16, &[u8])>> {
        let mut result = Vec::new();
        // Each attribute consumes at least its four-byte header.
        for _ in 0..bytes.len() / 4 {
            if bytes.is_empty() {
                break;
            }
            let length = usize::from(u16::from_ne_bytes(bytes.get(..2)?.try_into().ok()?));
            let kind = u16::from_ne_bytes(bytes.get(2..4)?.try_into().ok()?) & 0x3fff;
            if length < 4 {
                return None;
            }
            result.push((kind, bytes.get(4..length)?));
            bytes = bytes.get((length + 3) & !3..)?;
        }
        bytes.is_empty().then_some(result)
    }

    pub fn get(bytes: &[u8], kind: u16) -> Option<&[u8]> {
        parse(bytes)?
            .into_iter()
            .find_map(|(key, value)| (key == kind).then_some(value))
    }

    pub fn u32_value(bytes: &[u8]) -> Option<u32> {
        Some(u32::from_ne_bytes(bytes.try_into().ok()?))
    }

    fn bitrate(bytes: &[u8]) -> Option<f64> {
        let rate = if let Some(value) = get(bytes, BITRATE32) {
            u32_value(value)?
        } else {
            u32::from(u16::from_ne_bytes(get(bytes, BITRATE)?.try_into().ok()?))
        };
        (rate > 0).then_some(f64::from(rate) / 10.0)
    }

    pub fn radio(interface: &mut super::NetworkInterface, bytes: &[u8], index: u32) -> Option<()> {
        if u32_value(get(bytes, IFINDEX)?)? != index
            || u32_value(get(bytes, IFTYPE)?)? != IFTYPE_STATION
        {
            return None;
        }
        interface.frequency_mhz = get(bytes, WIPHY_FREQ)
            .and_then(u32_value)
            .filter(|value| *value > 0);
        Some(())
    }

    pub fn station(interface: &mut super::NetworkInterface, bytes: &[u8]) {
        let Some(info) = get(bytes, STA_INFO) else {
            return;
        };
        interface.signal_dbm = get(info, SIGNAL)
            .filter(|value| value.len() == 1)
            .map(|value| i64::from(i8::from_ne_bytes([value[0]])))
            .filter(|value| *value <= 0);
        interface.tx_bitrate_mbps = get(info, TX_BITRATE).and_then(bitrate);
        interface.rx_bitrate_mbps = get(info, RX_BITRATE).and_then(bitrate);
    }
}

pub fn collect(interface: &mut NetworkInterface) {
    #[cfg(target_os = "linux")]
    let _ = linux::collect(interface);
    #[cfg(not(target_os = "linux"))]
    let _ = interface;
}

#[cfg(target_os = "linux")]
mod linux {
    use super::{NetworkInterface, attributes::*};
    use rustix::net::{
        AddressFamily, RecvFlags, SendFlags, SocketType, bind,
        netlink::{self, SocketAddrNetlink},
        recvfrom, sendto, socket,
        sockopt::{Timeout, set_socket_timeout},
    };
    use std::{
        fs,
        os::fd::OwnedFd,
        time::{Duration, Instant},
    };

    const GENL_ID_CTRL: u16 = 0x10;
    const CTRL_CMD_GETFAMILY: u8 = 3;
    const CTRL_ATTR_FAMILY_ID: u16 = 1;
    const CTRL_ATTR_FAMILY_NAME: u16 = 2;
    const GET_INTERFACE: u8 = 5;
    const GET_STATION: u8 = 17;
    const NLM_F_REQUEST: u16 = 1;
    const NLM_F_DUMP: u16 = 0x300;
    const NLM_F_DUMP_INTR: u16 = 0x10;
    const NLMSG_ERROR: u16 = 2;
    const NLMSG_DONE: u16 = 3;
    const NLMSG_OVERRUN: u16 = 4;

    struct Connection {
        socket: OwnedFd,
        deadline: Instant,
        sequence: u32,
    }

    fn attribute(kind: u16, value: &[u8]) -> Option<Vec<u8>> {
        let length = u16::try_from(value.len() + 4).ok()?;
        let mut bytes = Vec::from(length.to_ne_bytes());
        bytes.extend(kind.to_ne_bytes());
        bytes.extend(value);
        bytes.resize((bytes.len() + 3) & !3, 0);
        Some(bytes)
    }

    impl Connection {
        fn query(
            &mut self,
            family: u16,
            command: u8,
            dump: bool,
            attrs: &[u8],
        ) -> Option<Vec<Vec<u8>>> {
            self.sequence += 1;
            let mut message = Vec::from(u32::try_from(20 + attrs.len()).ok()?.to_ne_bytes());
            message.extend(family.to_ne_bytes());
            message.extend((NLM_F_REQUEST | if dump { NLM_F_DUMP } else { 0 }).to_ne_bytes());
            message.extend(self.sequence.to_ne_bytes());
            message.extend(0u32.to_ne_bytes());
            message.extend([command, 1, 0, 0]);
            message.extend(attrs);
            set_socket_timeout(&self.socket, Timeout::Send, Some(self.remaining()?)).ok()?;
            sendto(
                &self.socket,
                &message,
                SendFlags::empty(),
                &SocketAddrNetlink::new(0, 0),
            )
            .ok()?;
            let mut result = Vec::new();
            // Bound both wall time and retained kernel data, including a busy
            // AP's station dump. The outer network probe also holds one slot.
            let mut received = 0usize;
            loop {
                set_socket_timeout(&self.socket, Timeout::Recv, Some(self.remaining()?)).ok()?;
                let mut buffer = [0u8; 65536];
                let (_, size, sender) =
                    recvfrom(&self.socket, &mut buffer[..], RecvFlags::TRUNC).ok()?;
                if SocketAddrNetlink::try_from(sender?).ok()?.pid() != 0
                    || size > buffer.len()
                    || size == 0
                {
                    return None;
                }
                received += size;
                if received > 256 * 1024 {
                    return None;
                }
                let mut bytes = &buffer[..size];
                // Each message consumes at least its 16-byte header.
                for _ in 0..size / 16 {
                    if bytes.is_empty() {
                        break;
                    }
                    let length = usize::try_from(u32_value(bytes.get(..4)?)?).ok()?;
                    if length < 16 {
                        return None;
                    }
                    let kind = u16::from_ne_bytes(bytes.get(4..6)?.try_into().ok()?);
                    let flags = u16::from_ne_bytes(bytes.get(6..8)?.try_into().ok()?);
                    if u32_value(bytes.get(8..12)?)? != self.sequence
                        || flags & NLM_F_DUMP_INTR != 0
                    {
                        return None;
                    }
                    let body = bytes.get(16..length)?;
                    match kind {
                        NLMSG_ERROR | NLMSG_OVERRUN => return None,
                        NLMSG_DONE => {
                            if !body.is_empty() && u32_value(body.get(..4)?)? != 0 {
                                return None;
                            }
                            return Some(result);
                        }
                        value if value == family => {
                            result.push(body.get(4..)?.to_vec());
                            if !dump {
                                return Some(result);
                            }
                        }
                        _ => return None,
                    }
                    bytes = bytes.get((length + 3) & !3..)?;
                }
                if !bytes.is_empty() {
                    return None;
                }
            }
        }

        fn remaining(&self) -> Option<Duration> {
            self.deadline
                .checked_duration_since(Instant::now())
                .filter(|value| !value.is_zero())
        }
    }

    pub fn collect(interface: &mut NetworkInterface) -> Option<()> {
        let index: u32 = fs::read_to_string(format!("/sys/class/net/{}/ifindex", interface.name))
            .ok()?
            .trim()
            .parse()
            .ok()?;
        let socket = socket(
            AddressFamily::NETLINK,
            SocketType::RAW,
            Some(netlink::GENERIC),
        )
        .ok()?;
        bind(&socket, &SocketAddrNetlink::new(0, 0)).ok()?;
        let mut connection = Connection {
            socket,
            deadline: Instant::now() + Duration::from_millis(300),
            sequence: 0,
        };
        let family = connection.query(
            GENL_ID_CTRL,
            CTRL_CMD_GETFAMILY,
            false,
            &attribute(CTRL_ATTR_FAMILY_NAME, b"nl80211\0")?,
        )?;
        let family =
            u16::from_ne_bytes(get(family.first()?, CTRL_ATTR_FAMILY_ID)?.try_into().ok()?);
        let attrs = attribute(IFINDEX, &index.to_ne_bytes())?;
        let info = connection.query(family, GET_INTERFACE, false, &attrs)?;
        let info = info.first()?;
        // Only managed station links; AP clients are not this host's uplink.
        radio(interface, info, index)?;
        let stations = connection.query(family, GET_STATION, true, &attrs)?;
        if let [info] = stations.as_slice()
            && u32_value(get(info, IFINDEX)?)? == index
        {
            station(interface, info);
        }
        Some(())
    }
    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn kernel_refusal_does_not_poison_the_next_probe() {
            // Wrong implementation: a failed optional query latches failure,
            // or generic netlink reads accidentally require CAP_NET_ADMIN.
            let socket = socket(
                AddressFamily::NETLINK,
                SocketType::RAW,
                Some(netlink::GENERIC),
            )
            .unwrap();
            bind(&socket, &SocketAddrNetlink::new(0, 0)).unwrap();
            let mut connection = Connection {
                socket,
                deadline: Instant::now() + Duration::from_millis(300),
                sequence: 0,
            };
            // A missing required family identity deterministically returns EINVAL.
            assert!(
                connection
                    .query(GENL_ID_CTRL, CTRL_CMD_GETFAMILY, false, &[])
                    .is_none()
            );
            let present = attribute(CTRL_ATTR_FAMILY_NAME, b"nlctrl\0").unwrap();
            let reply = connection
                .query(GENL_ID_CTRL, CTRL_CMD_GETFAMILY, false, &present)
                .unwrap();
            assert_eq!(
                get(&reply[0], CTRL_ATTR_FAMILY_ID).unwrap(),
                GENL_ID_CTRL.to_ne_bytes()
            );
        }
    }
}

#[cfg(test)]
#[path = "wifi_tests.rs"]
mod tests;
