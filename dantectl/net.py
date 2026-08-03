# Interface and socket plumbing for dantectl.
#
# Stdlib only, deliberately: this tool has to run on the bench box next to the
# LiteX build, and adding a pip dependency there is a bigger cost than writing
# 100 lines of ioctl.
#
# EVERY socket here is bound to ONE interface's address, never 0.0.0.0 for
# sending and never with a wildcard multicast join. The bench host has both the
# AoIP network (ens5, 169.254/16) and the house network (eno1) -- see the top
# of the repo README. Leaking Dante queries onto eno1 is the failure mode this
# module exists to prevent.

import os
import socket
import struct

# Addresses come from a netlink RTM_GETADDR dump rather than SIOCGIFADDR.
#
# That is not gold-plating: on this bench the AoIP address is autoconfigured by
# avahi and carries the LABEL "ens5:avahi", and SIOCGIFADDR("ens5") answers
# EADDRNOTAVAIL for it -- the ioctl matches on label, not on interface. A
# link-local Dante network is exactly the case where that happens, so the ioctl
# path would fail precisely when the tool is most needed.

NETLINK_ROUTE = 0
RTM_GETADDR = 22
NLM_F_REQUEST = 0x001
NLM_F_DUMP = 0x300
NLMSG_ERROR = 2
NLMSG_DONE = 3
IFA_ADDRESS = 1
IFA_LOCAL = 2


def interfaces():
    """Every non-loopback interface the kernel knows about."""
    try:
        names = sorted(os.listdir("/sys/class/net"))
    except OSError:
        return []
    return [n for n in names if n != "lo"]


def addresses():
    """[(ifname, ip, prefixlen, scope)] for every IPv4 address on the box."""
    out = []
    try:
        s = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_ROUTE)
    except (AttributeError, OSError):
        return out
    try:
        s.settimeout(2.0)
        s.bind((0, 0))
        # nlmsghdr(len, type, flags, seq, pid) + ifaddrmsg(family, prefixlen,
        # flags, scope, index)
        body = struct.pack("BBBBI", socket.AF_INET, 0, 0, 0, 0)
        msg = struct.pack("IHHII", 16 + len(body), RTM_GETADDR,
                          NLM_F_REQUEST | NLM_F_DUMP, 1, 0) + body
        s.send(msg)
        done = False
        while not done:
            data = s.recv(65535)
            off = 0
            while off + 16 <= len(data):
                mlen, mtype, _flags, _seq, _pid = struct.unpack("IHHII", data[off:off + 16])
                if mlen < 16 or off + mlen > len(data):
                    break
                if mtype in (NLMSG_DONE, NLMSG_ERROR):
                    done = True
                    break
                payload = data[off + 16:off + mlen]
                if len(payload) >= 8:
                    family, prefixlen, _fl, scope, index = struct.unpack("BBBBI", payload[:8])
                    attrs = {}
                    a = 8
                    while a + 4 <= len(payload):
                        alen, atype = struct.unpack("HH", payload[a:a + 4])
                        if alen < 4 or a + alen > len(payload):
                            break
                        attrs[atype] = payload[a + 4:a + alen]
                        a += (alen + 3) & ~3
                    raw = attrs.get(IFA_LOCAL) or attrs.get(IFA_ADDRESS)
                    if family == socket.AF_INET and raw and len(raw) >= 4:
                        try:
                            name = socket.if_indextoname(index)
                        except OSError:
                            name = str(index)
                        out.append((name, socket.inet_ntoa(raw[:4]), prefixlen, scope))
                off += (mlen + 3) & ~3
    except OSError:
        pass
    finally:
        s.close()
    return out


def ipv4_of(ifname):
    for name, ip, _plen, _scope in addresses():
        if name == ifname:
            return ip
    return None


def netmask_of(ifname):
    for name, _ip, plen, _scope in addresses():
        if name == ifname:
            bits = (0xFFFFFFFF << (32 - plen)) & 0xFFFFFFFF if plen else 0
            return socket.inet_ntoa(struct.pack(">I", bits))
    return None


def mac_of(ifname):
    try:
        with open("/sys/class/net/%s/address" % ifname) as f:
            return f.read().strip()
    except OSError:
        return None


def is_up(ifname):
    try:
        with open("/sys/class/net/%s/operstate" % ifname) as f:
            return f.read().strip() in ("up", "unknown")
    except OSError:
        return False


def candidates():
    """(ifname, ip) for every up interface with an IPv4 address."""
    out = []
    seen = set()
    for name, ip, _plen, _scope in addresses():
        if name == "lo" or name in seen or not is_up(name):
            continue
        seen.add(name)
        out.append((name, ip))
    return out


def pick_interface(preferred=None):
    """Choose the AoIP interface.

    An explicit -i always wins. Otherwise prefer a link-local 169.254/16
    address, because that is what an un-DHCPed Dante network looks like and it
    is what this bench uses. Never auto-pick the interface holding the default
    route: on this host that is eno1, the house network, which the README says
    to keep out of.
    """
    cands = candidates()
    if preferred:
        for name, ip in cands:
            if name == preferred:
                return name, ip
        ip = ipv4_of(preferred)
        if ip:
            return preferred, ip
        raise SystemExit(
            "interface %r has no IPv4 address (have: %s)"
            % (preferred, ", ".join(n for n, _ in cands) or "none")
        )

    default_if = _default_route_iface()
    link_local = [c for c in cands if c[1].startswith("169.254.")]
    if link_local:
        return link_local[0]
    others = [c for c in cands if c[0] != default_if]
    if others:
        return others[0]
    if cands:
        return cands[0]
    raise SystemExit("no usable IPv4 interface found")


def _default_route_iface():
    try:
        with open("/proc/net/route") as f:
            next(f)
            for line in f:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return parts[0]
    except (OSError, StopIteration):
        pass
    return None


def udp_socket(bind_ip, port, mcast_if=None, reuse=True, ttl=None, nonblock=True):
    """A UDP socket bound to (bind_ip, port), optionally multicast-capable.

    bind_ip may be "" to accept multicast on any local address -- required for
    receiving on a group, since the datagram's destination is the group address
    and a socket bound to the unicast address would never see it.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if reuse:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
    s.bind((bind_ip, port))
    if mcast_if:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(mcast_if))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    if ttl is not None:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    if nonblock:
        s.setblocking(False)
    return s


def join_group(sock, group, if_ip):
    """IGMP-join `group` on exactly one interface. Returns True on success."""
    try:
        mreq = socket.inet_aton(group) + socket.inet_aton(if_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        return True
    except OSError:
        return False
