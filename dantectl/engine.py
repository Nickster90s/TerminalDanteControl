# The discovery / polling engine.
#
# One background thread owns every socket and the device registry; the UI only
# ever reads snapshots. That split exists because curses and select() do not mix
# comfortably, and because the poll cadence must not depend on how long a
# redraw takes.
#
# What it listens to, and why each one is needed:
#
#   mDNS 5353            _netaudio-arc / -cmc PTR+SRV+TXT+A -> the device list
#   224.0.0.231:8702     device / product / network / clock announcements
#   224.0.0.233:8708     1 Hz heartbeat -> live ppb, offset, path delay
#   224.0.1.129:319/320  PTPv1 Sync -> who is actually grandmaster (needs root)
#
# and what it asks for:
#
#   ARC 4440             device names (0x1003) and channel counts (0x1000)
#   info 8700            0x61 / 0xc1 / 0x13 device state, 0x21 clock stats
#
# Dante Controller polls the same way: devices announce on the info group every
# ~2-3 s, and a controller only sends unicast requests on refresh. The default
# cadence here is deliberately no more aggressive than that -- firmware
# dante_info.c notes a real device gets "flooded with info multicast requests
# around 1 per second" when a controller misbehaves.

import errno
import select
import socket
import struct
import threading
import time
from collections import deque

from . import mdns, net, proto

PPB_HISTORY = 240          # ~20 min of heartbeats, enough for a drift trend

# Offset-from-master below which a device is called locked when it does not
# answer clock stats. Real followers on this bench sit at 200 ns - 1 us; the
# FPGA target reports ~400 ns when its PTPv1 servo is settled. 100 us is two
# orders of magnitude above that -- comfortably "in sync" without being so
# tight that a normal servo excursion flips the indicator.
SYNC_OFFSET_NS = 100_000

RETRY_BASE = 4.0           # seconds before re-asking an unanswered question
RETRY_MAX = 60.0
DEVICE_COOLDOWN = 1.0      # minimum gap between two requests to the same device
CHANNEL_INTERVAL = 6.0     # re-read the routing grid's channel lists this often
# Just after a patch, read back faster and for a while. Two reasons: a device
# that does not acknowledge 0x3010 gives us no other signal that anything
# happened, and even one that does needs a moment -- the subscription appears as
# unresolved first and only becomes active once the receiver has found the
# transmitter and set up a flow. Waiting a full interval for either reads as
# "the patch did not take".
CHANNEL_BURST_INTERVAL = 0.5
CHANNEL_BURST_SECS = 12.0


def retry_delay(misses):
    return min(RETRY_MAX, RETRY_BASE * (2 ** min(misses, 4)))


class Device:
    __slots__ = (
        "ip", "device_id", "name", "hostname", "services", "txt",
        "manufacturer", "model", "product_version", "firmware_version",
        "hardware_version", "board_name", "revision", "mac", "link_speed_mbps",
        "netmask", "tx_channels", "rx_channels", "max_channels_in_flow",
        "max_tx_flows", "max_rx_flows", "sample_rate", "clock", "heartbeat",
        "ppb_history", "first_seen", "last_seen", "last_info", "last_clock",
        "last_arc", "arc_error", "is_self", "next_info", "next_clock", "next_arc",
        "sent_info", "sent_clock", "sent_arc", "miss_info", "miss_clock", "miss_arc",
        "last_heartbeat", "last_tx", "ptp_role", "ptp_leader_mac",
        "tx_list", "rx_list", "_tx_acc", "_rx_acc", "chan_error",
        "next_chan_tx", "next_chan_rx", "chan_burst_until",
    )

    def __init__(self, ip):
        self.ip = ip
        self.device_id = None
        self.name = None
        self.hostname = None
        self.services = {}          # service type -> {"port":, "instance":, "txt":}
        self.txt = {}               # merged TXT keys, cmc winning over arc
        self.manufacturer = None
        self.model = None
        self.product_version = None
        self.firmware_version = None
        self.hardware_version = None
        self.board_name = None
        self.revision = None
        self.mac = None
        self.link_speed_mbps = None
        self.netmask = None
        self.tx_channels = None
        self.rx_channels = None
        self.max_channels_in_flow = None
        self.max_tx_flows = None
        self.max_rx_flows = None
        self.sample_rate = None
        self.clock = {}             # last parse_clock_stats()
        self.heartbeat = {}         # last parse_heartbeat()
        self.ppb_history = deque(maxlen=PPB_HISTORY)
        now = time.monotonic()
        self.first_seen = now
        self.last_seen = now
        self.last_info = 0.0        # last device/product/network reply
        self.last_clock = 0.0       # last clock-stats reply
        self.last_arc = 0.0
        self.arc_error = None
        self.is_self = False
        self.last_heartbeat = 0.0
        # When to ask again, when we last asked, and how many asks went
        # unanswered. Kept separate from last_* because a device may answer via
        # the multicast group rather than to us, and because a device that
        # dropped one request should be retried sooner than the steady-state
        # interval -- the FPGA target does drop ARC requests that arrive in the
        # same burst as four info queries.
        self.next_info = 0.0
        self.next_clock = 0.0
        self.next_arc = 0.0
        self.sent_info = 0.0
        self.sent_clock = 0.0
        self.sent_arc = 0.0
        self.miss_info = 0
        self.miss_clock = 0
        self.miss_arc = 0
        self.last_tx = 0.0          # last request of any kind sent to this device
        # Filled in by Engine.snapshot() from the PTPv1 sniffer, for devices
        # that never report a role themselves.
        self.ptp_role = None
        self.ptp_leader_mac = None
        # Channel lists for the routing grid. Fetched only for the two devices
        # the Routing page has selected -- pulling every channel of every
        # device on a large network would be a lot of traffic for a view
        # nobody is looking at. Pages accumulate in _tx_acc/_rx_acc and are
        # swapped in whole, so the grid never renders a half-loaded list.
        self.tx_list = None
        self.rx_list = None
        self._tx_acc = None
        self._rx_acc = None
        self.next_chan_tx = 0.0
        self.next_chan_rx = 0.0
        self.chan_burst_until = 0.0
        self.chan_error = None

    # -- derived ----------------------------------------------------------

    @property
    def display_name(self):
        return self.name or self.hostname or self.ip

    @property
    def age(self):
        return time.monotonic() - self.last_seen

    @property
    def clock_age(self):
        return time.monotonic() - self.last_clock if self.last_clock else None

    @property
    def sync_state(self):
        """locked / unlocked / stale / unknown, plus whether it was inferred.

        Clock stats (0x0020) are authoritative but not every device answers
        them -- a RedNet AM2 answers 0x13/0x61/0xc1 and stays silent on 0x21.
        For those, fall back to the heartbeat's 0x8000 offset-from-master,
        which is the record Dante Controller's own Sync indicator reads.
        """
        age = self.clock_age
        if self.clock and age is not None and age <= 30:
            return "locked" if self.clock.get("locked") else "unlocked"
        offset = self.offset_ns
        if offset is not None and time.monotonic() - self.last_heartbeat < 15:
            return "locked~" if offset < SYNC_OFFSET_NS else "unlocked~"
        if self.clock:
            return "stale"
        return "unknown"

    @property
    def sync_inferred(self):
        return self.sync_state.endswith("~")

    @property
    def is_leader(self):
        return self.clock.get("port_state") == 6

    @property
    def offset_ns(self):
        """Offset from master -- undefined for the grandmaster itself.

        The A16R keeps sending a 0x8000 record while it is the elected leader,
        with values around 455 us that are not an offset from anything. Showing
        them would say the network's own clock source is half a millisecond out.
        """
        return None if self.is_leader else self.heartbeat.get("offset_ns")

    @property
    def path_delay_ns(self):
        return None if self.is_leader else self.heartbeat.get("path_delay_ns")

    @property
    def ppb(self):
        # Heartbeat first, but only the documented 4-byte 0x8001 form sets it;
        # otherwise clock stats, which carry the same value in a fixed slot.
        if "freq_offset_ppb" in self.heartbeat:
            return self.heartbeat["freq_offset_ppb"]
        return self.clock.get("freq_offset_ppb")


class Engine:
    def __init__(self, ifname, ifaddr, browse_interval=10.0, info_interval=15.0,
                 clock_interval=5.0, arc_interval=60.0, passive=False,
                 want_ptp=True):
        self.ifname = ifname
        self.ifaddr = ifaddr
        self.browse_interval = browse_interval
        self.info_interval = info_interval
        self.clock_interval = clock_interval
        self.arc_interval = arc_interval
        self.passive = passive          # listen only, never transmit
        self.want_ptp = want_ptp

        # Everything outside the chosen interface's subnet is ignored.
        #
        # A host with a foot in both networks -- a laptop running Dante Via on
        # the AoIP link-local AND on the office LAN -- advertises both addresses
        # over mDNS. Accepting the second one puts it in the device list twice,
        # and unicast requests to it leave via the default route, i.e. the very
        # interface this tool is supposed to keep off. It also produces two
        # identically named entries in the routing selectors, one of which can
        # never answer.
        mask = net.netmask_of(ifname) or (
            "255.255.0.0" if ifaddr.startswith("169.254.") else "255.255.255.0")
        self._mask = struct.unpack(">I", socket.inet_aton(mask))[0]
        self._subnet = struct.unpack(">I", socket.inet_aton(ifaddr))[0] & self._mask
        self.netmask = mask

        self.lock = threading.RLock()
        self.devices = {}               # ip -> Device
        self.instances = {}             # service instance fqdn -> dict
        self.hosts = {}                 # hostname -> ip
        self.log = deque(maxlen=200)
        self.stats = {
            "mdns_rx": 0, "info_rx": 0, "hb_rx": 0, "arc_rx": 0, "ptp_rx": 0,
            "tx": 0, "unknown_info": 0,
        }
        self.ptp = {}                   # source uuid -> {"last": t, "seq":, "count":}
        self._off_subnet = set()        # addresses ignored, logged once each
        self.ptp_available = False
        self.sockets = {}
        self.socket_errors = []

        self._seq = 0x1000
        # IPs of the two devices the Routing page is showing, if any.
        self.route_tx_ip = None
        self.route_rx_ip = None
        self._stop = threading.Event()
        self._thread = None
        self._next_browse = 0.0
        self._pending_resolve = deque()
        self._last_full_refresh = 0.0

    # -- lifecycle --------------------------------------------------------

    def start(self):
        self._open_sockets()
        self._thread = threading.Thread(target=self._run, name="dante-engine", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        for s in self.sockets.values():
            try:
                s.close()
            except OSError:
                pass

    def _note(self, msg):
        with self.lock:
            self.log.append((time.time(), msg))

    def _open_sockets(self):
        # mDNS: one socket to ask from (unicast answers land here) and one
        # joined to the group (for answers that come back multicast, and for
        # unsolicited announcements).
        self.sockets["mdns_tx"] = net.udp_socket(self.ifaddr, 0, mcast_if=self.ifaddr, ttl=255)
        try:
            rx = net.udp_socket("", mdns.MCAST_PORT, mcast_if=self.ifaddr, ttl=255)
            if net.join_group(rx, mdns.MCAST_ADDR, self.ifaddr):
                self.sockets["mdns_rx"] = rx
            else:
                rx.close()
                self.socket_errors.append("mDNS group join failed; unicast answers only")
        except OSError as e:
            self.socket_errors.append("mDNS 5353 listener unavailable (%s)" % e.strerror)

        # Device info. Bound to 8702 so that multicast announcements to
        # 224.0.0.231:8702 arrive, and used as the SOURCE for our requests so
        # that unicast replies (devices answer to the requester's source port)
        # arrive on the same socket.
        try:
            info = net.udp_socket("", proto.PORT_INFO, mcast_if=self.ifaddr)
            if not net.join_group(info, proto.GRP_DEVINFO, self.ifaddr):
                self.socket_errors.append("could not join %s" % proto.GRP_DEVINFO)
            self.sockets["info"] = info
        except OSError as e:
            self.socket_errors.append("port %d unavailable (%s) -- announcements will be missed"
                                      % (proto.PORT_INFO, e.strerror))
            self.sockets["info"] = net.udp_socket(self.ifaddr, 0, mcast_if=self.ifaddr)

        try:
            hb = net.udp_socket("", proto.PORT_HEARTBEAT, mcast_if=self.ifaddr)
            if not net.join_group(hb, proto.GRP_HEARTBEAT, self.ifaddr):
                self.socket_errors.append("could not join %s" % proto.GRP_HEARTBEAT)
            self.sockets["hb"] = hb
        except OSError as e:
            self.socket_errors.append("port %d unavailable (%s) -- no heartbeats"
                                      % (proto.PORT_HEARTBEAT, e.strerror))

        self.sockets["arc"] = net.udp_socket(self.ifaddr, 0)

        # PTPv1, BOTH ports. 319 is the event port and carries Sync and
        # Delay_Req; 320 is the general port and carries Follow_Up and
        # Delay_Resp. Listening only on 320 -- which this did at first -- sees
        # the leader's Follow_Ups and never a single Delay_Req, so no follower
        # can be identified and the leader is only found by inference. Both
        # ports are privileged, so this whole path needs root.
        #
        # It earns that root: a device that answers no clock stats at all still
        # announces its role here. A RedNet AM2 emits Delay_Req and nothing
        # else, which is a follower, stated by the device on the wire.
        if self.want_ptp:
            for port in (proto.PTPV1_EVENT_PORT, proto.PTPV1_GENERAL_PORT):
                try:
                    ptp = net.udp_socket("", port, mcast_if=self.ifaddr)
                    net.join_group(ptp, proto.PTPV1_GROUP, self.ifaddr)
                    self.sockets["ptp%d" % port] = ptp
                    self.ptp_available = True
                except OSError as e:
                    if e.errno in (errno.EACCES, errno.EPERM):
                        self.socket_errors.append(
                            "PTPv1 sniff needs root (ports 319/320); without it, devices that "
                            "do not answer clock stats show no PTP role -- run with sudo")
                    else:
                        self.socket_errors.append(
                            "PTPv1 port %d unavailable (%s)" % (port, e.strerror))
                    break

    # -- main loop --------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            socks = [s for s in self.sockets.values()]
            try:
                ready, _, _ = select.select(socks, [], [], 0.25)
            except (OSError, ValueError):
                time.sleep(0.1)
                continue
            for s in ready:
                self._drain(s)
            now = time.monotonic()
            try:
                self._tick(now)
            except OSError:
                pass

    def _drain(self, sock):
        name = None
        for k, v in self.sockets.items():
            if v is sock:
                name = k
                break
        while True:
            try:
                data, addr = sock.recvfrom(65535)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            try:
                self._handle(name, data, addr)
            except Exception as e:                       # never let one bad packet kill the loop
                self._note("parse error on %s from %s: %s" % (name, addr[0], e))

    def _handle(self, which, data, addr):
        if which in ("mdns_tx", "mdns_rx"):
            self.stats["mdns_rx"] += 1
            self._handle_mdns(data, addr)
        elif which == "info":
            self.stats["info_rx"] += 1
            self._handle_info(data, addr)
        elif which == "hb":
            self.stats["hb_rx"] += 1
            self._handle_info(data, addr)
        elif which == "arc":
            self.stats["arc_rx"] += 1
            self._handle_arc(data, addr)
        elif which and which.startswith("ptp"):
            self.stats["ptp_rx"] += 1
            self._handle_ptp(data, addr)

    # -- mDNS -------------------------------------------------------------

    def _handle_mdns(self, data, addr):
        try:
            records = mdns.parse_message(data)
        except mdns.DnsError:
            return
        with self.lock:
            for rec in records:
                rtype = rec["type"]
                name = rec["name"]
                if rtype == mdns.T_A:
                    self.hosts[name.lower()] = rec["addr"]
                elif rtype == mdns.T_PTR:
                    target = rec.get("target")
                    if target and mdns.service_of(target) in mdns.DEVICE_SERVICES:
                        inst = self.instances.setdefault(
                            target.lower(), {"fqdn": target, "svc": mdns.service_of(target)})
                        inst["seen"] = time.monotonic()
                        if "port" not in inst:
                            self._pending_resolve.append(target)
                elif rtype == mdns.T_SRV:
                    svc = mdns.service_of(name)
                    if svc:
                        inst = self.instances.setdefault(name.lower(), {"fqdn": name, "svc": svc})
                        inst["port"] = rec["port"]
                        inst["target"] = rec["target"]
                        inst["seen"] = time.monotonic()
                elif rtype == mdns.T_TXT:
                    svc = mdns.service_of(name)
                    if svc:
                        inst = self.instances.setdefault(name.lower(), {"fqdn": name, "svc": svc})
                        inst["txt"] = rec["txt"]
                        inst["seen"] = time.monotonic()
            self._materialize()

    def _materialize(self):
        """Fold resolved mDNS instances into the device registry (lock held)."""
        for key, inst in self.instances.items():
            target = inst.get("target")
            if not target:
                continue
            ip = self.hosts.get(target.lower())
            if not ip:
                continue
            dev = self._device(ip)
            if dev is None:
                continue
            dev.hostname = target.split(".")[0]
            svc = inst["svc"]
            dev.services[svc] = {
                "port": inst.get("port"),
                "instance": mdns.instance_of(inst["fqdn"], svc),
                "txt": inst.get("txt", {}),
            }
            txt = inst.get("txt") or {}
            # cmc carries the identity keys (id=, model=, channels=); arc
            # carries the router/protocol versions. Merge both, cmc last.
            if svc == mdns.SVC_ARC:
                for k, v in txt.items():
                    dev.txt.setdefault(k, v)
            else:
                dev.txt.update(txt)
            if not dev.name:
                dev.name = mdns.instance_of(inst["fqdn"], svc)
            if "id" in txt and not dev.device_id:
                try:
                    dev.device_id = bytes.fromhex(txt["id"])
                    dev.mac = dev.mac or proto.eui64_to_mac(dev.device_id)
                except ValueError:
                    pass
            if "mf" in txt and not dev.manufacturer:
                dev.manufacturer = txt["mf"]
            dev.last_seen = max(dev.last_seen, inst.get("seen", 0.0))

    def on_subnet(self, ip):
        try:
            return struct.unpack(">I", socket.inet_aton(ip))[0] & self._mask == self._subnet
        except OSError:
            return False

    def _device(self, ip):
        """The device at `ip`, or None if it is not on our interface's subnet."""
        dev = self.devices.get(ip)
        if dev is None:
            if not self.on_subnet(ip):
                if ip not in self._off_subnet:
                    self._off_subnet.add(ip)
                    self._note("ignoring %s -- not on %s (%s/%s)"
                               % (ip, self.ifname, self.ifaddr, self.netmask))
                self.stats["off_subnet"] = self.stats.get("off_subnet", 0) + 1
                return None
            dev = Device(ip)
            dev.is_self = (ip == self.ifaddr)
            self.devices[ip] = dev
            self._note("discovered %s" % ip)
        return dev

    # -- device info / heartbeat -----------------------------------------

    def _handle_info(self, data, addr):
        msg = proto.info_parse(data)
        if msg is None or msg["vendor"] != proto.VENDOR:
            return
        ip = addr[0]
        with self.lock:
            dev = self._device(ip)
            if dev is None:
                return
            dev.last_seen = time.monotonic()
            if not dev.device_id and any(msg["device_id"]):
                dev.device_id = msg["device_id"]
                dev.mac = dev.mac or proto.eui64_to_mac(dev.device_id)
            content = msg["content"]
            op = msg["opcode"]

            if msg["start_code"] == proto.SC_HEARTBEAT:
                hb = proto.parse_heartbeat(content)
                if hb:
                    dev.last_heartbeat = time.monotonic()
                    dev.heartbeat.update(hb)
                    if "freq_offset_ppb" in hb:
                        dev.ppb_history.append((time.monotonic(), hb["freq_offset_ppb"]))
                    if "sample_rate" in hb:
                        dev.sample_rate = hb["sample_rate"]
                return

            # Reply dispatch is on opcode[3] alone, with opcode[2] == 0 to keep
            # 0x1009 (opcode[2] == 0x10) from colliding with a query byte.
            if op[2] != 0:
                return
            q = op[3]
            if q == proto.R_CLOCK_STATS:
                cs = proto.parse_clock_stats(content)
                if cs:
                    dev.clock = cs
                    dev.last_clock = time.monotonic()
                    dev.miss_clock = 0
                    dev.next_clock = dev.last_clock + self.clock_interval
                    if cs.get("freq_offset_ppb") is not None and not dev.heartbeat:
                        dev.ppb_history.append((time.monotonic(), cs["freq_offset_ppb"]))
            elif q == proto.R_DEVICE_INFO:
                di = proto.parse_device_info(content)
                dev.firmware_version = di.get("firmware_version") or dev.firmware_version
                dev.hardware_version = di.get("hardware_version") or dev.hardware_version
                dev.board_name = di.get("board_name_long") or di.get("board_name") or dev.board_name
                dev.last_info = time.monotonic()
            elif q == proto.R_PRODUCT_INFO:
                pi = proto.parse_product_info(content)
                dev.manufacturer = pi.get("manufacturer") or dev.manufacturer
                dev.model = pi.get("model_name") or dev.model
                dev.product_version = pi.get("product_version") or dev.product_version
                dev.last_info = time.monotonic()
            elif q == proto.R_NETWORK_INFO:
                ni = proto.parse_network_info(content)
                dev.mac = ni.get("mac") or dev.mac
                dev.link_speed_mbps = ni.get("link_speed_mbps") or dev.link_speed_mbps
                dev.netmask = ni.get("netmask") or dev.netmask
                dev.last_info = time.monotonic()
            else:
                self.stats["unknown_info"] += 1
                return
            if q in (proto.R_DEVICE_INFO, proto.R_PRODUCT_INFO, proto.R_NETWORK_INFO):
                dev.miss_info = 0
                dev.next_info = dev.last_info + self.info_interval

    # -- ARC --------------------------------------------------------------

    def _handle_arc(self, data, addr):
        msg = proto.arc_parse(data)
        if msg is None:
            return
        with self.lock:
            dev = self._device(addr[0])
            if dev is None:
                return
            dev.last_arc = time.monotonic()
            dev.last_seen = dev.last_arc
            dev.miss_arc = 0
            dev.next_arc = dev.last_arc + self.arc_interval
            if not msg["ok"]:
                dev.arc_error = "opcode 0x%04x -> code 0x%04x" % (msg["opcode1"], msg["opcode2"])
                if msg["opcode1"] in (proto.OP_GET_TX_CHANNELS, proto.OP_GET_RX_CHANNELS,
                                      proto.OP_SET_SUBSCRIPTIONS):
                    dev.chan_error = dev.arc_error
                    dev._tx_acc = dev._rx_acc = None
                return
            dev.arc_error = None
            if msg["opcode1"] == proto.OP_GET_DEVICE_NAMES:
                names = proto.parse_device_names(msg)
                dev.name = names.get("friendly_name") or dev.name
                dev.hostname = names.get("factory_hostname") or dev.hostname
                dev.board_name = names.get("board_name") or dev.board_name
                dev.revision = names.get("revision") or dev.revision
            elif msg["opcode1"] == proto.OP_GET_DEVICE_NAME:
                dev.name = parse_or(proto.parse_device_name(msg), dev.name)
            elif msg["opcode1"] == proto.OP_GET_TX_CHANNELS:
                items, more = proto.parse_tx_channels(msg)
                dev._tx_acc = (dev._tx_acc or []) + items
                if more and items:
                    # Ask for the rest, starting one past the last id we got.
                    self._send("arc", proto.channels_request(
                        proto.OP_GET_TX_CHANNELS, self._next_seq(),
                        items[-1]["id"] + 1), (dev.ip, proto.PORT_ARC))
                else:
                    dev.tx_list = dev._tx_acc
                    dev._tx_acc = None
            elif msg["opcode1"] == proto.OP_GET_RX_CHANNELS:
                items, more = proto.parse_rx_channels(msg)
                dev._rx_acc = (dev._rx_acc or []) + items
                if more and items:
                    self._send("arc", proto.channels_request(
                        proto.OP_GET_RX_CHANNELS, self._next_seq(),
                        items[-1]["id"] + 1), (dev.ip, proto.PORT_ARC))
                else:
                    dev.rx_list = dev._rx_acc
                    dev._rx_acc = None
            elif msg["opcode1"] == proto.OP_SET_SUBSCRIPTIONS:
                # The device acknowledges the patch but does not echo the new
                # state, so re-read the receive channels to show what actually
                # took rather than what we asked for.
                dev.next_chan_rx = 0.0
                self._note("%s accepted subscription change" % dev.ip)
            elif msg["opcode1"] == proto.OP_CHANNEL_COUNTS:
                counts = proto.parse_channel_counts(msg)
                for attr in ("tx_channels", "rx_channels", "max_channels_in_flow",
                             "max_tx_flows", "max_rx_flows"):
                    if counts.get(attr) is not None:
                        setattr(dev, attr, counts[attr])

    # -- PTPv1 ------------------------------------------------------------

    def _handle_ptp(self, data, addr):
        p = proto.parse_ptpv1(data)
        if not p:
            return
        with self.lock:
            entry = self.ptp.setdefault(p["source_uuid"], {
                "count": 0, "sync": 0, "delay_req": 0, "leaderish": 0})
            entry["last"] = time.monotonic()
            entry["seq"] = p["sequence"]
            entry["subdomain"] = p["subdomain"]
            entry["count"] += 1
            entry["src_ip"] = addr[0]
            ctl = p["control"]
            if ctl == proto.PTPV1_CTL_SYNC:
                entry["sync"] += 1
            # Sync, Follow_Up and Delay_Resp are all sent BY the leader;
            # Delay_Req only ever by a follower. That asymmetry is the whole
            # classification -- no election state or clock-stats reply needed.
            if ctl in (proto.PTPV1_CTL_SYNC, proto.PTPV1_CTL_FOLLOWUP,
                       proto.PTPV1_CTL_DELAY_RESP):
                entry["leaderish"] += 1
            elif ctl == proto.PTPV1_CTL_DELAY_REQ:
                entry["delay_req"] += 1

    def ptp_role(self, mac, max_age=60.0):
        """LEADER / FOLLOWER for a MAC, from sniffed PTPv1, or None.

        A device sending Sync/Follow_Up/Delay_Resp is leading; one sending only
        Delay_Req is following. Both counters are checked because a device that
        has just lost an election can have stale counts of the other kind.
        """
        if not mac:
            return None
        with self.lock:
            entry = self.ptp.get(mac.lower())
            if not entry or time.monotonic() - entry.get("last", 0) > max_age:
                return None
            if entry["leaderish"] > entry["delay_req"]:
                return "LEADER"
            if entry["delay_req"]:
                return "FOLLOWER"
            return None

    # -- transmit ---------------------------------------------------------

    def _next_seq(self):
        self._seq = (self._seq + 1) & 0xFFFF
        return self._seq

    def _send(self, sock_name, data, dest):
        sock = self.sockets.get(sock_name)
        if sock is None or self.passive:
            return
        try:
            sock.sendto(data, dest)
            self.stats["tx"] += 1
        except OSError:
            pass

    def browse(self):
        """One-shot browse for the device-level services."""
        questions = [(svc, mdns.T_PTR) for svc in mdns.DEVICE_SERVICES]
        self._send("mdns_tx", mdns.build_query(questions), (mdns.MCAST_ADDR, mdns.MCAST_PORT))

    def resolve(self, instance):
        q = [(instance, mdns.T_SRV), (instance, mdns.T_TXT)]
        self._send("mdns_tx", mdns.build_query(q), (mdns.MCAST_ADDR, mdns.MCAST_PORT))

    def resolve_host(self, hostname):
        self._send("mdns_tx", mdns.build_query([(hostname, mdns.T_A)]),
                   (mdns.MCAST_ADDR, mdns.MCAST_PORT))

    def query_arc(self, ip):
        for opcode in (proto.OP_GET_DEVICE_NAMES, proto.OP_CHANNEL_COUNTS):
            self._send("arc", proto.arc_request(opcode, self._next_seq()), (ip, proto.PORT_ARC))

    def query_info(self, ip, queries):
        for q in queries:
            self._send("info", proto.info_request(q, self._next_seq()), (ip, proto.PORT_INFO_REQ))

    # -- routing ----------------------------------------------------------

    def set_routing(self, tx_ip, rx_ip):
        """Tell the engine which two devices the routing grid is showing."""
        with self.lock:
            changed = (tx_ip, rx_ip) != (self.route_tx_ip, self.route_rx_ip)
            self.route_tx_ip, self.route_rx_ip = tx_ip, rx_ip
            if changed:
                for ip in (tx_ip, rx_ip):
                    dev = self.devices.get(ip) if ip else None
                    if dev:
                        dev.next_chan_tx = dev.next_chan_rx = 0.0
                        dev.chan_error = None

    def refresh_channels(self):
        with self.lock:
            for ip in (self.route_tx_ip, self.route_rx_ip):
                dev = self.devices.get(ip) if ip else None
                if dev:
                    dev.next_chan_tx = dev.next_chan_rx = 0.0
                    dev.chan_error = None

    def subscribe(self, rx_ip, rx_channel_id, tx_name, tx_host):
        """Point one receive channel at a transmit channel. A WRITE.

        This is the only message dantectl sends that changes a device. Passive
        mode blocks it like everything else.
        """
        if self.passive:
            return False
        self._send("arc", proto.subscribe_request(self._next_seq(), rx_channel_id,
                                                  tx_name, tx_host),
                   (rx_ip, proto.PORT_ARC))
        self._note("subscribe %s ch%d <- %s@%s" % (rx_ip, rx_channel_id, tx_name, tx_host))
        self._read_back(rx_ip)
        return True

    def _read_back(self, rx_ip):
        """Re-read a device's receive channels right now, then keep checking.

        Driven from the write itself rather than from the acknowledgement,
        because not every device answers 0x3010 and the ack is not the point
        anyway -- what the grid must show is what the device reports about
        itself afterwards.
        """
        with self.lock:
            dev = self.devices.get(rx_ip)
            if dev:
                dev.next_chan_rx = 0.0
                dev.chan_burst_until = time.monotonic() + CHANNEL_BURST_SECS

    def unsubscribe(self, rx_ip, rx_channel_id):
        """Clear a receive channel: the same opcode with an empty name."""
        if self.passive:
            return False
        self._send("arc", proto.subscribe_request(self._next_seq(), rx_channel_id, "", ""),
                   (rx_ip, proto.PORT_ARC))
        self._note("unsubscribe %s ch%d" % (rx_ip, rx_channel_id))
        self._read_back(rx_ip)
        return True

    def refresh(self):
        """What the Refresh button does in Dante Controller: re-browse and
        re-ask everything, right now."""
        self.browse()
        now = time.monotonic()
        with self.lock:
            devs = list(self.devices.values())
            targets = [i["target"] for i in self.instances.values()
                       if i.get("target") and i["target"].lower() not in self.hosts]
            for inst in self.instances.values():
                if "port" not in inst:
                    self._pending_resolve.append(inst["fqdn"])
        for target in targets:
            self.resolve_host(target)
        for dev in devs:
            dev.next_arc = dev.next_info = dev.next_clock = now
            dev.miss_arc = dev.miss_info = dev.miss_clock = 0
        self._last_full_refresh = now

    def _tick(self, now):
        if now >= self._next_browse:
            self.browse()
            self._next_browse = now + self.browse_interval

        # Resolve at most a couple of instances per tick so a network full of
        # devices does not produce a query storm.
        for _ in range(2):
            if not self._pending_resolve:
                break
            inst = self._pending_resolve.popleft()
            self.resolve(inst)

        with self.lock:
            devices = list(self.devices.values())
            unresolved = [i["target"] for i in self.instances.values()
                          if i.get("target") and i["target"].lower() not in self.hosts]
        for target in unresolved[:2]:
            self.resolve_host(target)

        # One request group per device per tick, at most two devices per tick.
        #
        # Pacing is not politeness, it is correctness: the FPGA target answers
        # every one of these individually but drops ARC when it arrives in the
        # same burst as four info queries -- 20 s of polling produced zero ARC
        # replies from it while both RedNets answered, and the same requests
        # sent one at a time all succeeded. Assume any small device has a
        # shallow receive path and spread the load.
        # Channel lists first: if the routing grid is open it is what the user
        # is looking at, and the only view that goes stale in a way they would
        # act on. Two separate jobs, because the same device can legitimately
        # be both ends of the grid and then needs both lists.
        jobs = []
        if self.route_tx_ip:
            jobs.append((self.route_tx_ip, proto.OP_GET_TX_CHANNELS, "tx"))
        if self.route_rx_ip:
            jobs.append((self.route_rx_ip, proto.OP_GET_RX_CHANNELS, "rx"))
        for ip, opcode, kind in jobs:
            dev = self.devices.get(ip)
            if dev is None or now - dev.last_tx < DEVICE_COOLDOWN:
                continue
            if now < (dev.next_chan_tx if kind == "tx" else dev.next_chan_rx):
                continue
            interval = (CHANNEL_BURST_INTERVAL if now < dev.chan_burst_until
                        else CHANNEL_INTERVAL)
            if kind == "tx":
                dev._tx_acc = None
                dev.next_chan_tx = now + interval
            else:
                dev._rx_acc = None
                dev.next_chan_rx = now + interval
            self._send("arc", proto.channels_request(opcode, self._next_seq(), 1),
                       (ip, proto.PORT_ARC))
            dev.last_tx = now
            return                      # one channel fetch per tick, and
                                        # nothing else to that device this tick

        budget = 1
        for dev in devices:
            if budget <= 0:
                break
            if dev.is_self or now - dev.last_tx < DEVICE_COOLDOWN:
                continue
            if now >= dev.next_arc:
                self.query_arc(dev.ip)
                dev.miss_arc = self._count_miss(dev.sent_arc, dev.last_arc, dev.miss_arc)
                dev.sent_arc = now
                dev.next_arc = now + retry_delay(dev.miss_arc)
            elif now >= dev.next_clock:
                self.query_info(dev.ip, (proto.Q_CLOCK_STATS,))
                dev.miss_clock = self._count_miss(dev.sent_clock, dev.last_clock, dev.miss_clock)
                dev.sent_clock = now
                dev.next_clock = now + retry_delay(dev.miss_clock)
            elif now >= dev.next_info:
                self.query_info(dev.ip, (proto.Q_DEVICE_INFO, proto.Q_PRODUCT_INFO,
                                         proto.Q_NETWORK_INFO))
                dev.miss_info = self._count_miss(dev.sent_info, dev.last_info, dev.miss_info)
                dev.sent_info = now
                dev.next_info = now + retry_delay(dev.miss_info)
            else:
                continue
            dev.last_tx = now
            budget -= 1

    @staticmethod
    def _count_miss(sent, replied, misses):
        """A request that was sent and never answered counts as a miss; each
        miss doubles the retry delay. The answer path resets this to zero and
        pushes the next poll out to the steady interval, so a device that
        answers is polled slowly and one that just dropped a datagram is
        retried in seconds rather than after a full interval."""
        if sent and replied < sent:
            return misses + 1
        return 0

    # -- read side --------------------------------------------------------

    def snapshot(self):
        leader = self.ptp_leader()
        leader_mac = leader[0] if leader else None
        with self.lock:
            devs = sorted(self.devices.values(), key=lambda d: (d.display_name.lower(), d.ip))
        # Annotate here rather than in the UI so the tables stay simple lambdas
        # over a device, and so --json gets the same derived values.
        for dev in devs:
            dev.ptp_role = self.ptp_role(dev.mac)
            dev.ptp_leader_mac = leader_mac
        return list(devs)

    def ptp_leader(self):
        """The MAC transmitting PTPv1 Sync, if we are allowed to sniff."""
        with self.lock:
            best = None
            for uuid, e in self.ptp.items():
                if e.get("sync"):
                    if best is None or e["sync"] > best[1]["sync"]:
                        best = (uuid, e)
            return best


def parse_or(value, fallback):
    return value if value else fallback
