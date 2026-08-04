# Dante control-plane wire formats, host side.
#
# This is the mirror image of firmware/dante_msg.c, firmware/dante_arc.c and
# firmware/dante_info.c: the firmware encodes these messages as a device, this
# encodes the queries a controller sends and decodes what devices answer.
#
# Provenance for every layout here is either a capture in captures/README.md or
# the firmware source that was written against one. Nothing is guessed; fields
# whose meaning is unknown are carried as raw bytes and displayed as such.
#
# Two different framings are in play, which is the first thing to get straight:
#
#   ARC / CMC / flow control (4440 / 8800 / 4455)
#       10-byte header: start_code, total_length, seqnum, opcode1, opcode2.
#       String fields are referenced by offsets ABSOLUTE FROM THE START OF THE
#       PACKET -- they include the 10-byte header (dante_msg.h).
#
#   Device info / heartbeat (8700 / 8702 / 8708)
#       32-byte header: start_code, total_length, seqnum, process,
#       factory_device_id[8], vendor[8] = "Audinate", opcode[8].

import socket
import struct

# --------------------------------------------------------------------------
# Ports and groups (firmware/dante_dev.h)
# --------------------------------------------------------------------------

PORT_ARC = 4440           # _netaudio-arc   control / routing
PORT_CMC = 8800           # _netaudio-cmc   device advertisement
PORT_FLOWS = 4455         # _netaudio-chan  flow control
PORT_MEDIA = 4321         # audio
PORT_INFO_REQ = 8700      # info requests land here; heartbeat source port
PORT_INFO = 8702          # device-info multicast
PORT_HEARTBEAT = 8708     # heartbeat multicast

GRP_DEVINFO = "224.0.0.231"
GRP_HEARTBEAT = "224.0.0.233"

PTPV1_GROUP = "224.0.1.129"
PTPV1_EVENT_PORT = 319
PTPV1_GENERAL_PORT = 320

# --------------------------------------------------------------------------
# ARC framing
# --------------------------------------------------------------------------

ARC_HDR_LEN = 10
ARC_START_CODE = 0x2714   # what Dante Controller puts on the wire
ARC_OK = 1
ARC_MORE = 0x8112         # paginated response: more items remain

OP_CHANNEL_COUNTS = 0x1000
OP_GET_DEVICE_NAME = 0x1002
OP_GET_DEVICE_NAMES = 0x1003
OP_GET_TX_CHANNELS = 0x2000
OP_GET_RX_CHANNELS = 0x3000
OP_SET_SUBSCRIPTIONS = 0x3010

# Subscription status, from the u32 at offset 12 of a receive-channel item.
# 0x01010009 is a resolved, running subscription; 0x00000001 means the device
# remembers a subscription whose transmitter it cannot currently find (a
# RedNet A16R on the bench reports exactly this for channels pointed at a
# Yamaha QL1 that is powered off).
SUB_ACTIVE = 0x01010009
SUB_UNRESOLVED = 0x00000001


def arc_request(opcode, seq, content=b"", start_code=ARC_START_CODE):
    return struct.pack(">HHHHH", start_code, ARC_HDR_LEN + len(content), seq, opcode, 0) + content


def arc_parse(data):
    """-> dict(seq, opcode1, opcode2, ok, content, raw) or None if malformed."""
    if len(data) < ARC_HDR_LEN:
        return None
    start_code, total_len, seq, op1, op2 = struct.unpack(">HHHHH", data[:ARC_HDR_LEN])
    return {
        "start_code": start_code,
        "length": total_len,
        "seq": seq,
        "opcode1": op1,
        "opcode2": op2,
        "ok": op2 in (ARC_OK, ARC_MORE),
        "more": op2 == ARC_MORE,
        "content": data[ARC_HDR_LEN:],
        "raw": data,
    }


def _cstr_at(raw, abs_off, limit=128):
    """Read a NUL-terminated string at an ABSOLUTE packet offset."""
    if abs_off <= 0 or abs_off >= len(raw):
        return None
    end = raw.find(b"\x00", abs_off)
    if end < 0:
        end = len(raw)
    if end - abs_off > limit:
        return None
    s = raw[abs_off:end].decode("utf-8", "replace")
    return s or None


def parse_device_name(msg):
    """0x1002 -- a plain NUL-terminated string. netaudio uses this one."""
    return _cstr_at(msg["raw"], ARC_HDR_LEN)


def parse_device_names(msg):
    """0x1003 -- 38-byte offset table, then the strings it points at.

    Field positions verified against a RedNet A16R and a RedNet AM2 (both
    answer with the friendly name at content+20 and the factory hostname at
    content+22, NOT at the +12/+14 positions inferno's struct names). The
    firmware writes both, so accept either and prefer what real hardware uses.
    """
    raw = msg["raw"]
    c = msg["content"]
    if len(c) < 24:
        return {}

    def off(i):
        return struct.unpack(">H", c[i:i + 2])[0]

    def pick(*idxs):
        for i in idxs:
            if i + 2 > len(c):
                continue
            s = _cstr_at(raw, off(i))
            if s:
                return s
        return None

    return {
        "board_name": pick(6),
        "revision": pick(8),
        "friendly_name": pick(20, 12, 24),
        "factory_hostname": pick(22, 14),
    }


def parse_channel_counts(msg):
    """0x1000 -- channel and flow capabilities (proto_arc.rs channels_and_flows_count).

    A16R answers 0f f9 0012 0012 0000 0040 0040 0020 0020 0012: 18 tx, 18 rx,
    64 channels per flow, 32 tx flows, 32 rx flows.
    """
    c = msg["content"]
    if len(c) < 20:
        return {}
    flags2 = c[1]
    vals = struct.unpack(">9H", c[2:20])
    return {
        "supports_tx_channel_rename": bool(flags2 & 0x10),
        "supports_tx_multicast": bool(flags2 & 0x20),
        "tx_channels": vals[0],
        "rx_channels": vals[1],
        "max_channels_in_flow": vals[3],
        "max_tx_flows": vals[5],
        "max_rx_flows": vals[6],
        "total_channels": vals[7],
    }


# --------------------------------------------------------------------------
# Channel lists (the routing grid)
# --------------------------------------------------------------------------

def channels_request(opcode, seq, start=1):
    """Ask for a page of channels beginning at `start` (1-based).

    The argument block is SIX bytes and the first word must be 1. This was not
    guessable from the firmware, which defaults the start index whenever the
    content is short and so accepts anything: real hardware does not. A RedNet
    A16R rejects an empty request, a 2-byte request, both 4-byte forms and an
    8-byte form with code 0x0022, and rejects `0000 ...` with 0x0023. Only
    `0001 <start> 0000` is answered. Swept against the device to find it.
    """
    return arc_request(opcode, seq, struct.pack(">HHH", 1, start, 0))


def _page_items(msg, item_size):
    """Paged list responses: u8 space, u8 actual, then `actual` fixed items.

    Returns (items, more). `more` means the device has further channels and
    wants another request with a higher start index -- opcode2 0x8112.
    """
    c = msg["content"]
    if len(c) < 2:
        return [], False
    actual = c[1]
    items = []
    for i in range(actual):
        off = 2 + i * item_size
        if off + item_size > len(c):
            break
        items.append(c[off:off + item_size])
    return items, msg["more"]


def parse_tx_channels(msg):
    """0x2000 -- transmit channels. 8-byte items: id, 7, common offset, name."""
    raw = msg["raw"]
    out = []
    items, more = _page_items(msg, 8)
    for item in items:
        cid, _unk, _common, name_off = struct.unpack(">HHHH", item)
        out.append({"id": cid, "name": _cstr_at(raw, name_off) or str(cid)})
    return out, more


def parse_rx_channels(msg):
    """0x3000 -- receive channels, 20-byte items.

      0  channel_id            6  tx_channel_name_offset
      2  unknown (6)           8  tx_hostname_offset
      4  common_desc_offset   10  friendly_name_offset
     12  subscription_status u32
    """
    raw = msg["raw"]
    out = []
    items, more = _page_items(msg, 20)
    for item in items:
        cid, _unk, _common, tx_off, host_off, label_off, status = struct.unpack(
            ">HHHHHHI", item[:16])
        out.append({
            "id": cid,
            "label": _cstr_at(raw, label_off) or str(cid),
            "tx_name": _cstr_at(raw, tx_off),
            "tx_host": _cstr_at(raw, host_off),
            "status": status,
            "active": status == SUB_ACTIVE,
            "unresolved": status == SUB_UNRESOLVED,
        })
    return out, more


def subscribe_request(seq, rx_channel_id, tx_name="", tx_host=""):
    """0x3010 -- point a receive channel at a transmitter, or clear it.

    Content, captured from Dante Controller patching a RedNet A16R:

        0201 <rx_ch> <name_off> <host_off> then the two NUL-terminated strings

    0x0201 is fixed in every request seen. Both offsets are ABSOLUTE from the
    start of the packet, so they include the 10-byte header. An EMPTY transmit
    channel name is how Dante Controller clears a patch, so unsubscribe is the
    same message with empty strings rather than a separate opcode.
    """
    name = tx_name.encode("utf-8")[:31]
    host = tx_host.encode("utf-8")[:31]
    fixed = 8
    name_off = ARC_HDR_LEN + fixed
    host_off = name_off + len(name) + 1
    content = (struct.pack(">HHHH", 0x0201, rx_channel_id, name_off, host_off)
               + name + b"\x00" + host + b"\x00")
    return arc_request(OP_SET_SUBSCRIPTIONS, seq, content)


# --------------------------------------------------------------------------
# Device-info framing (32-byte multicast header)
# --------------------------------------------------------------------------

INFO_HDR_LEN = 32
VENDOR = b"Audinate"

# Request query byte -> what it asks for. inferno's dispatcher matches the ODD
# values (0x61, 0xc1) for board and product info while the firmware accepts
# both, so send the odd ones -- they work everywhere.
Q_DEVICE_INFO = 0x61
Q_PRODUCT_INFO = 0xC1
Q_NETWORK_INFO = 0x13
Q_CLOCK_STATS = 0x21
Q_SAMPLE_RATES = 0x81
Q_ENCODINGS = 0x83

# Reply opcode byte [3]. Note network info replies 0x11 to a 0x13 request and
# clock stats replies 0x20 to a 0x21 -- request and reply are not the same value.
R_DEVICE_INFO = 0x60
R_PRODUCT_INFO = 0xC0
R_NETWORK_INFO = 0x11
R_CLOCK_STATS = 0x20
R_SAMPLE_RATES = 0x80
R_ENCODINGS = 0x82

SC_HEARTBEAT = 0xFFFE
SC_INFO = 0xFFFF


def info_request(query, seq, device_id=b"\x00" * 8, process=0):
    """A controller's info query. 32-byte header, no content.

    Opcode shaped like Dante Controller's: 07 3e 00 <q> 00 00 00 64. The 0x3e
    family byte and the trailing 0x64 are what DC sends (firmware/dante_info.c
    records the exact bytes it saw); inferno's matcher treats both as wildcards
    and the firmware ignores everything but byte 3.
    """
    opcode = bytes([0x07, 0x3E, 0x00, query, 0x00, 0x00, 0x00, 0x64])
    return (
        struct.pack(">HHHH", SC_INFO, INFO_HDR_LEN, seq & 0xFFFF, process)
        + device_id[:8].ljust(8, b"\x00")
        + VENDOR
        + opcode
    )


def info_parse(data):
    if len(data) < INFO_HDR_LEN:
        return None
    start_code, total_len, seq, process = struct.unpack(">HHHH", data[:8])
    return {
        "start_code": start_code,
        "length": total_len,
        "seq": seq,
        "process": process,
        "device_id": data[8:16],
        "vendor": data[16:24],
        "opcode": data[24:32],
        "query": data[27],
        "content": data[INFO_HDR_LEN:],
    }


def _fixed_str(buf, at, width):
    if at + width > len(buf):
        return None
    s = buf[at:at + width].split(b"\x00")[0].decode("utf-8", "replace").strip()
    return s or None


def _ver(buf, at):
    if at + 4 > len(buf):
        return None
    a, b, c, d = buf[at:at + 4]
    return "%d.%d.%d.%d" % (a, b, c, d)


def parse_device_info(content):
    """Reply 0x0060 -- board name and firmware/hardware versions."""
    if len(content) < 0x48:
        return {}
    return {
        "firmware_version": _ver(content, 0),
        "hardware_version": _ver(content, 4),
        "board_name": _fixed_str(content, 12, 8),
        "board_name_long": _fixed_str(content, 0x38, 16),
        "supports_aes67": bool(content[0x14] & 0x04),
        "lockable": bool(content[0x14] & 0x08),
        "has_manufacturer": bool(content[0x16] & 0x10),
        "network_configurable": bool(content[0x16] & 0x40),
        # Kept raw as well. 0x17 is documented as covering identify, sample
        # rate / encoding configuration, reboot and factory reset, but which
        # bit is which was never established, so it is shown rather than
        # decoded into four confident booleans.
        "flag_bytes": bytes(content[0x14:0x18]),
    }


def parse_product_info(content):
    """Reply 0x00c0 -- Manufacturer, Model Name and Product Version columns."""
    if len(content) < 0xBC:
        return {}
    return {
        "manufacturer": _fixed_str(content, 0x2C, 16) or _fixed_str(content, 0, 8),
        "board": _fixed_str(content, 8, 8),
        "model_name": _fixed_str(content, 0xAC, 16),
        "product_version": _ver(content, 0x1C),
    }


def parse_capability_table(content):
    """Replies 0x0080 (sample rates) and 0x0082 (encodings) share one shape:

        0  2  item size, 0x18 on every device seen
        2  2  count of entries in the list
        4  4  current value
        8  4  a second value -- 0 on the A16R and the FPGA, a copy of the
              current value on the AM2, so not interpreted here
       16  .. `count` big-endian u32 entries

    Sizes check out exactly: the A16R's 40-byte rate table is 16 + 6 x 4 and
    lists 44100/48000/88200/96000/176400/192000; its 20-byte encoding table is
    16 + 1 x 4 and lists 24.
    """
    if len(content) < 16:
        return {}
    _item_size, count = struct.unpack(">HH", content[0:4])
    current = struct.unpack(">I", content[4:8])[0]
    values = []
    for i in range(count):
        off = 16 + i * 4
        if off + 4 > len(content):
            break
        values.append(struct.unpack(">I", content[off:off + 4])[0])
    return {"current": current, "supported": values}


def parse_network_info(content):
    """Reply 0x0011 -- link speed and the primary address.

    Layout: 6-byte lead, u16 link speed in Mbps, u16 1, MAC[6], IP[4],
    netmask[4], gateway[4], DNS[4].
    """
    if len(content) < 32:
        return {}
    speed = struct.unpack(">H", content[6:8])[0]
    mac = ":".join("%02x" % b for b in content[10:16])
    return {
        "link_speed_mbps": speed,
        "mac": mac,
        "ip": socket.inet_ntoa(content[16:20]),
        "netmask": socket.inet_ntoa(content[20:24]),
        "gateway": socket.inet_ntoa(content[24:28]),
        "dns": socket.inet_ntoa(content[28:32]),
    }


# IEEE 1588 port states. Offset 40 of the clock-stats payload; a Follower sends
# 9 (SLAVE) and a Leader 6 (MASTER). This is the field behind Dante
# Controller's "Primary v1 Multicast" column.
PTP_PORT_STATES = {
    1: "INITIALIZING",
    2: "FAULTY",
    3: "DISABLED",
    4: "LISTENING",
    5: "PRE_LEADER",
    6: "LEADER",
    7: "PASSIVE",
    8: "UNCALIBRATED",
    9: "FOLLOWER",
}


def _clock_id(buf, at):
    if at + 8 > len(buf):
        return None
    b = buf[at:at + 8]
    if not any(b):
        return None
    return ":".join("%02x" % x for x in b)


def parse_clock_stats(content):
    """Reply 0x0020 -- the PTP state a controller displays.

    Only the first 42 bytes are relied on; they are identical across the three
    payload lengths seen in the wild (148 from DVS, 188 from an AM2, 208 from
    an A16R). Everything past that varies per device and is not parsed.

      [2:4]   status      0x0003 locked, 0x0001 PLL not locked
      [8:12]  freq offset ppb, signed
      [12:20] this device's clock id   (MAC + 0x0000, NOT EUI-64)
      [20:28] grandmaster clock id
      [28:36] parent clock id
      [40:42] IEEE 1588 port state     9 = Follower, 6 = Leader
    """
    if len(content) < 42:
        return {}
    word0 = struct.unpack(">H", content[0:2])[0]
    status = struct.unpack(">H", content[2:4])[0]
    ppb = struct.unpack(">i", content[8:12])[0]
    state = struct.unpack(">H", content[40:42])[0]
    # status 3 = locked follower, 1 = PLL not locked (inferno). A grandmaster
    # answers 6 here and 6 at offset 40 -- observed on a RedNet A16R while it
    # was the elected PTPv1 leader -- and a device that IS the clock is in sync
    # with it by definition, so treat LEADER as locked rather than as unknown.
    return {
        "locked": status == 3 or state == 6,
        "status": status,
        "word0": word0,
        "freq_offset_ppb": ppb,
        "clock_id": _clock_id(content, 12),
        "grandmaster_id": _clock_id(content, 20),
        "parent_id": _clock_id(content, 28),
        "port_state": state,
        "port_state_name": PTP_PORT_STATES.get(state, "0x%04x" % state),
        "payload_len": len(content),
    }


def parse_heartbeat(content):
    """The 1 Hz 0xfffe heartbeat: a run of length-delimited sub-records.

        0  2  record length (including this 12-byte header)
        2  2  type
        4  2  0x0004
        6  2  content length
        8  2  seqnum / uptime
       10  2  0
       12  .. content

    0x8001 carries the frequency offset in ppb that drives Dante Controller's
    clock histogram. 0x8000 carries offset-from-master and mean path delay in
    ns -- the two numbers that decide whether the Sync indicator is green.
    Walk by the record length, never by the content-length field: real devices
    send 0x8000 with content length 4 and 24 bytes of payload.

    CAVEAT on the second word of 0x8000. The firmware sends mean path delay
    there and a RedNet AM2 sends something that climbs by ~1000 per second
    (0x7dd68 -> 0x7e0bc in one heartbeat interval), which is a counter, not a
    delay. The value is reported as-is and labelled path delay because that is
    what the firmware means by it; do not read the number from a foreign device
    as nanoseconds without checking it moves like one.
    """
    out = {"records": []}
    i = 0
    while i + 12 <= len(content):
        rec_len, rtype = struct.unpack(">HH", content[i:i + 4])
        if rec_len < 12 or i + rec_len > len(content):
            break
        body = content[i + 12:i + rec_len]
        out["records"].append(rtype)
        if rtype == 0x8001:
            # ONLY the 4-byte form is the frequency offset. A RedNet A16R sends
            # a 40-byte 0x8001 whose layout is not established, and reading its
            # first word as ppb would print a confident wrong number. Clock
            # stats (0x0020) carry the same value and are used instead there.
            if len(body) == 4:
                out["freq_offset_ppb"] = struct.unpack(">i", body[:4])[0]
            else:
                out["freq_offset_raw"] = body.hex()
        elif rtype == 0x8000 and len(body) >= 12:
            # item size / pad / count / item size, then the two live values.
            out["offset_ns"] = struct.unpack(">I", body[8:12])[0]
            if len(body) >= 16:
                out["path_delay_ns"] = struct.unpack(">I", body[12:16])[0]
        elif rtype == 0x8003 and len(body) >= 8:
            rate = struct.unpack(">I", body[4:8])[0]
            if 8000 <= rate <= 768000:
                out["sample_rate"] = rate
        i += rec_len
    return out


def eui64_to_mac(dev_id):
    """001dc1fffea1723c -> 00:1d:c1:a1:72:3c. Devices advertise the EUI-64 form
    in mDNS `id=` but report clock ids as MAC + 0x0000."""
    if len(dev_id) == 8 and dev_id[3] == 0xFF and dev_id[4] == 0xFE:
        b = dev_id[:3] + dev_id[5:]
    elif len(dev_id) == 8:
        b = dev_id[:6]
    else:
        b = dev_id
    return ":".join("%02x" % x for x in b)


# --------------------------------------------------------------------------
# PTPv1 (sniffed, not spoken)
# --------------------------------------------------------------------------

PTPV1_HDR_LEN = 40
PTPV1_CTL_SYNC = 0
PTPV1_CTL_DELAY_REQ = 1
PTPV1_CTL_FOLLOWUP = 2
PTPV1_CTL_DELAY_RESP = 3


def parse_ptpv1(data):
    """Header only -- enough to see who is transmitting Sync, i.e. who leads.

    Verified byte for byte against captures/dante_ptpv1.pcap and statime's
    inferno-dev messages_v1/header.rs: 40-byte header, subdomain at 4..20,
    source uuid (the sender's MAC) at 22..28, control byte at 32.
    """
    if len(data) < PTPV1_HDR_LEN:
        return None
    version = struct.unpack(">H", data[0:2])[0]
    if version != 1:
        return None
    subdomain = data[4:20].split(b"\x00")[0].decode("ascii", "replace")
    uuid = ":".join("%02x" % b for b in data[22:28])
    seq = struct.unpack(">H", data[30:32])[0]
    return {
        "subdomain": subdomain,
        "source_uuid": uuid,
        "sequence": seq,
        "control": data[32],
    }
