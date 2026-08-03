# dantectl -- a terminal Dante controller.
#
#   ./dantectl                     # TUI on the auto-picked interface
#   ./dantectl -i ens5             # pin the interface
#   ./dantectl --list -t 6         # one-shot text listing, no curses
#   ./dantectl --json -t 6         # same, machine readable
#
# Equivalently `python3 -m dantectl` from the repository root.
#
# Read-only by design: it browses, queries and listens. It never advertises a
# device of its own and never writes to one, so it cannot disturb a live
# network the way a second controller announcing itself would.

import argparse
import curses
import json
import sys
import time

from . import engine as engine_mod
from . import mdns, net, proto, ui


def build_parser():
    p = argparse.ArgumentParser(
        prog="dantectl", description="Terminal Dante controller: discover devices and watch clock sync.")
    p.add_argument("-i", "--interface", help="network interface (default: the link-local/AoIP one)")
    p.add_argument("--list", action="store_true", help="print a device table and exit")
    p.add_argument("--json", action="store_true", help="print discovered devices as JSON and exit")
    p.add_argument("-t", "--time", type=float, default=5.0,
                   help="seconds to collect for in --list/--json mode (default 5)")
    p.add_argument("--passive", action="store_true",
                   help="listen only: no mDNS queries, no ARC or info requests")
    p.add_argument("--no-mouse", action="store_true",
                   help="do not capture the mouse (restores plain terminal text selection)")
    p.add_argument("--no-ptp", action="store_true",
                   help="do not try to sniff PTPv1 (avoids the port-320 permission attempt)")
    p.add_argument("--browse-interval", type=float, default=10.0)
    p.add_argument("--clock-interval", type=float, default=5.0)
    p.add_argument("--info-interval", type=float, default=15.0)
    p.add_argument("--arc-interval", type=float, default=60.0)
    p.add_argument("--interfaces", action="store_true", help="list candidate interfaces and exit")
    return p


def device_dict(dev):
    return {
        "ip": dev.ip,
        "name": dev.name,
        "hostname": dev.hostname,
        "device_id": dev.device_id.hex() if dev.device_id else None,
        "mac": dev.mac,
        "manufacturer": dev.manufacturer,
        "model": dev.model,
        "board_name": dev.board_name,
        "product_version": dev.product_version,
        "firmware_version": dev.firmware_version,
        "hardware_version": dev.hardware_version,
        "revision": dev.revision,
        "link_speed_mbps": dev.link_speed_mbps,
        "tx_channels": dev.tx_channels,
        "rx_channels": dev.rx_channels,
        "max_channels_in_flow": dev.max_channels_in_flow,
        "max_tx_flows": dev.max_tx_flows,
        "max_rx_flows": dev.max_rx_flows,
        "sample_rate": dev.sample_rate,
        "services": {k: v.get("port") for k, v in dev.services.items()},
        "txt": dev.txt,
        "sync": dev.sync_state,
        "ptp_role_reported": dev.clock.get("port_state_name"),
        "ptp_role_sniffed": dev.ptp_role,
        "is_leader": dev.is_leader,
        "offset_ns": dev.offset_ns,
        "path_delay_ns": dev.path_delay_ns,
        "freq_offset_ppb": dev.ppb,
        "clock": dev.clock,
        "heartbeat": dev.heartbeat,
        "age_s": round(dev.age, 1),
    }


def print_list(eng):
    devices = eng.snapshot()
    if not devices:
        print("no Dante devices seen on %s (%s)" % (eng.ifname, eng.ifaddr))
        return 1
    hdr = "%-22s %-15s %-17s %-9s %4s %4s  %-9s %-9s %-18s %10s %9s"
    print(hdr % ("NAME", "IP", "MODEL", "MFR", "TX", "RX", "SYNC", "PTP ROLE",
                 "GRANDMASTER", "OFFSET", "FREQ ppb"))
    for d in devices:
        offset = d.offset_ns
        print(hdr % (
            d.display_name[:22], d.ip, (d.model or d.board_name or "-")[:17],
            (d.manufacturer or "-")[:9],
            "-" if d.tx_channels is None else d.tx_channels,
            "-" if d.rx_channels is None else d.rx_channels,
            d.sync_state,
            d.clock.get("port_state_name") or
            (d.ptp_role.lower() + "~" if d.ptp_role else "-"),
            d.clock.get("grandmaster_id") or
            ((d.ptp_leader_mac + "~") if d.ptp_leader_mac else "-"),
            ("n/a" if d.is_leader else "-") if offset is None else "%d ns" % offset,
            "-" if d.ppb is None else "%+d" % d.ppb))
    leader = eng.ptp_leader()
    if leader:
        print("\nPTPv1 leader on the wire: %s (%s, subdomain %s)"
              % (leader[0], leader[1].get("src_ip", "?"), leader[1].get("subdomain", "?")))
    for err in eng.socket_errors:
        print("note: %s" % err, file=sys.stderr)
    return 0


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.interfaces:
        for name, ip in net.candidates():
            print("%-10s %s" % (name, ip))
        return 0

    ifname, ifaddr = net.pick_interface(args.interface)
    eng = engine_mod.Engine(
        ifname, ifaddr,
        browse_interval=args.browse_interval,
        info_interval=args.info_interval,
        clock_interval=args.clock_interval,
        arc_interval=args.arc_interval,
        passive=args.passive,
        want_ptp=not args.no_ptp,
    )
    eng.start()
    try:
        if args.list or args.json:
            deadline = time.monotonic() + args.time
            # Two browses: the first fills the instance table, the second picks
            # up anything that only answered the follow-up resolve.
            eng.browse()
            time.sleep(min(1.0, args.time / 2))
            eng.refresh()
            while time.monotonic() < deadline:
                time.sleep(0.2)
            if args.json:
                print(json.dumps({
                    "interface": ifname,
                    "address": ifaddr,
                    "devices": [device_dict(d) for d in eng.snapshot()],
                    "ptp_leader": eng.ptp_leader(),
                    "notes": eng.socket_errors,
                }, indent=2, default=str))
                return 0
            return print_list(eng)

        eng.refresh()
        curses.wrapper(ui.run, eng, not args.no_mouse)
        return 0
    except KeyboardInterrupt:
        return 130
    finally:
        eng.stop()


if __name__ == "__main__":
    sys.exit(main())
