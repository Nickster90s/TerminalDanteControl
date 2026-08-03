# Minimal mDNS client -- enough to browse and resolve Dante's _netaudio-* services.
#
# Not a general zeroconf implementation and not trying to be. It sends one-shot
# queries (RFC 6762 s5.1) and parses whatever comes back, from either the
# unicast reply socket or the multicast group. Dante devices answer a service
# query with SRV + TXT + A in one datagram (firmware/mdns.c does exactly this,
# copying the A16R), so browse + resolve usually completes in a single round
# trip and the explicit resolve query is only a fallback.
#
# Records we care about:
#   PTR  _netaudio-arc._udp.local  -> "<DeviceName>._netaudio-arc._udp.local"
#   SRV  <instance>                -> host + port
#   TXT  <instance>                -> id=, model=, mf=, arcp_vers=, ...
#   A    <host>                    -> IPv4

import socket
import struct

MCAST_ADDR = "224.0.0.251"
MCAST_PORT = 5353

T_A = 1
T_PTR = 12
T_TXT = 16
T_SRV = 33
T_ANY = 255

# The service types a Dante device advertises. arc and cmc are the device-level
# ones -- everything on the Discover page comes from these two. chan/bund are
# per-channel and per-flow, browsed only when a device is opened.
SVC_ARC = "_netaudio-arc._udp.local"
SVC_CMC = "_netaudio-cmc._udp.local"
SVC_CHAN = "_netaudio-chan._udp.local"
SVC_BUND = "_netaudio-bund._udp.local"
SVC_DBC = "_netaudio-dbc._udp.local"

DEVICE_SERVICES = (SVC_ARC, SVC_CMC, SVC_DBC)


class DnsError(Exception):
    pass


def encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        b = label.encode("utf-8")
        if len(b) > 63:
            raise DnsError("label too long: %r" % label)
        out += bytes([len(b)]) + b
    return out + b"\x00"


def decode_name(buf, off):
    """Decode a possibly-compressed name. Returns (name, next_offset)."""
    labels = []
    jumped = False
    next_off = off
    hops = 0
    while True:
        if off >= len(buf):
            raise DnsError("name runs past end of packet")
        ln = buf[off]
        if ln & 0xC0 == 0xC0:
            if off + 1 >= len(buf):
                raise DnsError("truncated compression pointer")
            ptr = ((ln & 0x3F) << 8) | buf[off + 1]
            if not jumped:
                next_off = off + 2
                jumped = True
            off = ptr
            hops += 1
            if hops > 64:
                raise DnsError("compression pointer loop")
            continue
        off += 1
        if ln == 0:
            if not jumped:
                next_off = off
            break
        if off + ln > len(buf):
            raise DnsError("truncated label")
        labels.append(buf[off:off + ln].decode("utf-8", "replace"))
        off += ln
    return ".".join(labels), next_off


def build_query(questions, unicast_reply=True):
    """questions: [(name, qtype)]. QU bit asks for a unicast answer."""
    hdr = struct.pack(">HHHHHH", 0, 0, len(questions), 0, 0, 0)
    body = b""
    for name, qtype in questions:
        qclass = 1 | (0x8000 if unicast_reply else 0)
        body += encode_name(name) + struct.pack(">HH", qtype, qclass)
    return hdr + body


def _parse_txt(rdata):
    out = {}
    i = 0
    while i < len(rdata):
        ln = rdata[i]
        i += 1
        item = rdata[i:i + ln]
        i += ln
        if not item:
            continue
        text = item.decode("utf-8", "replace")
        key, _, value = text.partition("=")
        out[key] = value
    return out


def parse_message(buf):
    """Parse a full mDNS message into a flat list of records.

    Every section is parsed the same way: mDNS responders scatter the useful
    records between answers and additionals with no consistency (the A16R puts
    SRV in answers and TXT+A in additionals; avahi does the opposite), so
    treating them alike is the only thing that works.

    Returns list of dicts: {name, type, ttl, ...type-specific fields}.
    """
    if len(buf) < 12:
        raise DnsError("short message")
    _id, flags, qd, an, ns, ar = struct.unpack(">HHHHHH", buf[:12])
    off = 12
    for _ in range(qd):
        _name, off = decode_name(buf, off)
        off += 4
    records = []
    for _ in range(an + ns + ar):
        if off >= len(buf):
            break
        name, off = decode_name(buf, off)
        if off + 10 > len(buf):
            break
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", buf[off:off + 10])
        off += 10
        rdata = buf[off:off + rdlen]
        rdata_off = off
        off += rdlen
        rec = {"name": name, "type": rtype, "ttl": ttl, "flush": bool(rclass & 0x8000)}
        try:
            if rtype == T_A and rdlen >= 4:
                rec["addr"] = socket.inet_ntoa(rdata[:4])
            elif rtype == T_PTR:
                rec["target"], _ = decode_name(buf, rdata_off)
            elif rtype == T_SRV and rdlen >= 6:
                prio, weight, port = struct.unpack(">HHH", rdata[:6])
                target, _ = decode_name(buf, rdata_off + 6)
                rec["port"] = port
                rec["target"] = target
                rec["priority"] = prio
                rec["weight"] = weight
            elif rtype == T_TXT:
                rec["txt"] = _parse_txt(rdata)
            else:
                rec["rdata"] = rdata
        except DnsError:
            continue
        records.append(rec)
    return records


def instance_of(fqdn, service):
    """"Foo._netaudio-arc._udp.local" + service -> "Foo", else None."""
    suffix = "." + service
    if fqdn.lower().endswith(suffix.lower()):
        return fqdn[: -len(suffix)]
    return None


def service_of(fqdn):
    """Return the service type an instance name belongs to, or None."""
    low = fqdn.lower()
    for svc in (SVC_ARC, SVC_CMC, SVC_CHAN, SVC_BUND, SVC_DBC):
        if low.endswith("." + svc):
            return svc
    return None
