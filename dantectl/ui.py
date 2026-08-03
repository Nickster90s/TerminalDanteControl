# curses front end: a Discover page and a Sync page.
#
# The two pages mirror what Dante Controller shows in its Device list and its
# Clock Status tab, because those are the two views this bench actually needs:
# "is the device on the network at all" and "is its clock locked, to whom, and
# how far off". Everything drawn here comes from engine.Device -- the UI does no
# protocol work of its own.

import curses
import sys
import time

from . import mdns, proto

PAGES = ("Discover", "Sync")

C_DEFAULT = 0
C_HEAD = 1
C_GOOD = 2
C_BAD = 3
C_WARN = 4
C_DIM = 5
C_SEL = 6
C_ACCENT = 7

SPARK = " ▁▂▃▄▅▆▇█"


# Mouse buttons. BUTTON5 (wheel down) is missing from some ncurses builds, so
# fall back to its documented bit rather than crashing at import.
BUTTON5 = getattr(curses, "BUTTON5_PRESSED", 0x00200000)


def enable_mouse():
    """Turn on click reporting. Returns True if the terminal plays along.

    Two steps, because either alone is not enough on a modern terminal:
    curses.mousemask() tells ncurses to decode mouse events, and the SGR
    sequence (\\033[?1006h) asks the terminal for the extended encoding. Without
    SGR, X10 encoding packs the column into one byte and clicks past column 223
    are unreportable -- which on a wide terminal is most of the screen.
    """
    try:
        avail, _ = curses.mousemask(curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED |
                                    curses.BUTTON4_PRESSED | BUTTON5)
    except (curses.error, AttributeError):
        return False
    if not avail:
        return False
    # Resolve press+release into a CLICKED event quickly; the default 200 ms
    # makes every click feel like it lands late.
    try:
        curses.mouseinterval(80)
    except curses.error:
        pass
    try:
        sys.stdout.write("\033[?1006h")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass
    return True


def disable_mouse():
    try:
        sys.stdout.write("\033[?1006l")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def init_colors():
    if not curses.has_colors():
        return False
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    curses.init_pair(C_HEAD, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(C_GOOD, curses.COLOR_GREEN, bg)
    curses.init_pair(C_BAD, curses.COLOR_RED, bg)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, bg)
    curses.init_pair(C_DIM, curses.COLOR_BLUE, bg)
    curses.init_pair(C_SEL, curses.COLOR_BLACK, curses.COLOR_WHITE)
    curses.init_pair(C_ACCENT, curses.COLOR_CYAN, bg)
    return True


def fmt_age(seconds):
    if seconds is None:
        return "-"
    if seconds < 1:
        return "now"
    if seconds < 60:
        return "%ds" % int(seconds)
    if seconds < 3600:
        return "%dm" % int(seconds // 60)
    return "%dh" % int(seconds // 3600)


def fmt_ns(value):
    if value is None:
        return "-"
    if value < 1000:
        return "%d ns" % value
    if value < 1_000_000:
        return "%.1f us" % (value / 1000.0)
    return "%.2f ms" % (value / 1_000_000.0)


def fmt_int(value, dash="-"):
    return dash if value is None else str(value)


def sparkline(values, width):
    if not values or width < 2:
        return ""
    vals = values[-width:]
    lo, hi = min(vals), max(vals)
    if hi == lo:
        return SPARK[4] * len(vals)
    span = float(hi - lo)
    return "".join(SPARK[1 + int((v - lo) / span * (len(SPARK) - 2))] for v in vals)


class Table:
    """Columns are (title, width, extractor). width <= 0 means 'take the rest'."""

    def __init__(self, columns):
        self.columns = columns

    def header(self, width):
        return self._row([c[0] for c in self.columns], width)

    def render(self, dev, width):
        cells = []
        attrs = []
        for _title, _w, fn in self.columns:
            out = fn(dev)
            if isinstance(out, tuple):
                text, attr = out
            else:
                text, attr = out, None
            cells.append(text)
            attrs.append(attr)
        return self._row(cells, width), attrs

    def spans(self, width):
        """Character ranges each column occupies, for per-cell colouring."""
        out = []
        x = 0
        for _title, w, _fn in self.columns:
            cw = w if w > 0 else max(4, width - x)
            out.append((x, cw))
            x += cw + 1
        return out

    def _row(self, cells, width):
        parts = []
        x = 0
        for (_title, w, _fn), text in zip(self.columns, cells):
            text = "" if text is None else str(text)
            cw = w if w > 0 else max(4, width - x)
            if len(text) > cw:
                text = text[:cw - 1] + "…"
            parts.append(text.ljust(cw))
            x += cw + 1
        return " ".join(parts)[:width]


def _svc_flags(dev):
    flags = ""
    flags += "a" if mdns.SVC_ARC in dev.services else "-"
    flags += "c" if mdns.SVC_CMC in dev.services else "-"
    flags += "i" if dev.last_info else "-"
    flags += "h" if dev.heartbeat else "-"
    return flags


DISCOVER = Table([
    ("NAME", 22, lambda d: (d.display_name, C_ACCENT if d.name else None)),
    ("IP", 15, lambda d: d.ip),
    ("MODEL", 17, lambda d: d.model or d.board_name or "-"),
    ("MFR", 9, lambda d: d.manufacturer or "-"),
    ("TX", 4, lambda d: fmt_int(d.tx_channels)),
    ("RX", 4, lambda d: fmt_int(d.rx_channels)),
    ("SVC", 5, lambda d: _svc_flags(d)),
    ("AGE", 0, lambda d: (fmt_age(d.age), C_WARN if d.age > 30 else None)),
])


def _sync_cell(dev):
    # A trailing ~ means the state was inferred from the heartbeat's
    # offset-from-master because the device does not answer clock stats.
    return {
        "locked": ("LOCKED", C_GOOD),
        "unlocked": ("UNLOCKED", C_BAD),
        "locked~": ("locked ~", C_GOOD),
        "unlocked~": ("unlocked ~", C_BAD),
        "stale": ("STALE", C_WARN),
        "unknown": ("-", C_DIM),
    }[dev.sync_state]


def _ppb_cell(dev):
    ppb = dev.ppb
    if ppb is None:
        return ("-", C_DIM)
    attr = C_GOOD if abs(ppb) < 1000 else (C_WARN if abs(ppb) < 10000 else C_BAD)
    return ("%+d" % ppb, attr)


def _offset_cell(dev):
    off = dev.offset_ns
    if off is None:
        return ("n/a" if dev.is_leader else "-", C_DIM)
    attr = C_GOOD if off < 10_000 else (C_WARN if off < 1_000_000 else C_BAD)
    return (fmt_ns(off), attr)


def _role_cell(dev):
    # The device's own clock-stats answer wins. Failing that, what it does on
    # the PTP wire -- marked ~ because we inferred it rather than being told.
    state = dev.clock.get("port_state_name")
    if state:
        return (state, C_ACCENT if dev.clock.get("port_state") == 6 else None)
    if dev.ptp_role:
        return (dev.ptp_role.lower() + " ~", C_ACCENT if dev.ptp_role == "LEADER" else None)
    return ("-", C_DIM)


def _gm_cell(dev):
    gm = dev.clock.get("grandmaster_id")
    if gm:
        return gm
    if dev.ptp_leader_mac:
        return (dev.ptp_leader_mac + " ~", C_DIM)
    return ("-", C_DIM)


SYNC = Table([
    ("NAME", 22, lambda d: (d.display_name, C_ACCENT if d.name else None)),
    ("SYNC", 9, _sync_cell),
    ("PTP ROLE", 12, _role_cell),
    ("GRANDMASTER", 18, _gm_cell),
    ("OFFSET", 10, _offset_cell),
    ("PATH DELAY", 10, lambda d: (fmt_ns(d.path_delay_ns),
                                  C_DIM if d.path_delay_ns is None else None)),
    ("FREQ ppb", 9, _ppb_cell),
    ("AGE", 0, lambda d: (fmt_age(d.clock_age), C_WARN if (d.clock_age or 0) > 30 else None)),
])


class App:
    def __init__(self, stdscr, engine, mouse_enabled=True):
        self.stdscr = stdscr
        self.engine = engine
        self.mouse_enabled = mouse_enabled
        self.page = 0
        self.sel = 0
        self.scroll = 0
        self.show_log = False
        self.color = init_colors()
        self.status = ""
        self.status_until = 0.0
        # Click targets, rebuilt on every draw: the header tabs and the visible
        # device rows. Hit-testing against what was actually painted is the only
        # way to stay correct through resizes and scrolling.
        self.tab_hits = []          # (y, x0, x1, page index)
        self.row_hits = {}          # screen y -> device index
        self.mouse = enable_mouse() if mouse_enabled else False
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(200)

    # -- helpers ----------------------------------------------------------

    def attr(self, code, bold=False):
        a = curses.color_pair(code) if (self.color and code) else 0
        if bold:
            a |= curses.A_BOLD
        return a

    def put(self, y, x, text, attr=0):
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        text = text[: max(0, w - x)]
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass       # writing the last cell of the last line always raises

    def flash(self, msg, seconds=3.0):
        self.status = msg
        self.status_until = time.monotonic() + seconds

    # -- main loop --------------------------------------------------------

    def run(self):
        try:
            while True:
                self.draw()
                try:
                    ch = self.stdscr.getch()
                except KeyboardInterrupt:
                    return
                if ch == -1:
                    continue
                if not self.handle_key(ch):
                    return
        finally:
            if self.mouse:
                disable_mouse()

    def handle_key(self, ch):
        devices = self.engine.snapshot()
        if ch in (ord("q"), 27):
            return False
        elif ch in (ord("\t"), curses.KEY_BTAB):
            self.page = (self.page + 1) % len(PAGES)
        elif ch == ord("1"):
            self.page = 0
        elif ch == ord("2"):
            self.page = 1
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.sel = min(self.sel + 1, max(0, len(devices) - 1))
        elif ch in (curses.KEY_UP, ord("k")):
            self.sel = max(0, self.sel - 1)
        elif ch in (curses.KEY_NPAGE,):
            self.sel = min(self.sel + 10, max(0, len(devices) - 1))
        elif ch in (curses.KEY_PPAGE,):
            self.sel = max(0, self.sel - 10)
        elif ch in (curses.KEY_HOME, ord("g")):
            self.sel = 0
        elif ch in (curses.KEY_END, ord("G")):
            self.sel = max(0, len(devices) - 1)
        elif ch == ord("r"):
            self.engine.refresh()
            self.flash("refresh sent: mDNS browse + ARC + info to %d device(s)" % len(devices))
        elif ch == ord("a"):
            self.engine.passive = not self.engine.passive
            self.flash("passive (listen-only) mode ON" if self.engine.passive
                       else "active polling ON")
        elif ch == ord("l"):
            self.show_log = not self.show_log
        elif ch == curses.KEY_MOUSE:
            self.handle_mouse(devices)
        elif ch == curses.KEY_RESIZE:
            self.stdscr.clear()
        return True

    def handle_mouse(self, devices):
        try:
            _id, x, y, _z, bstate = curses.getmouse()
        except curses.error:
            return
        if bstate & (curses.BUTTON4_PRESSED | BUTTON5):
            step = -3 if bstate & curses.BUTTON4_PRESSED else 3
            self.sel = max(0, min(self.sel + step, max(0, len(devices) - 1)))
            return
        # Terminals differ on whether a click arrives as CLICKED or as a bare
        # PRESSED, so accept either.
        if not bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
            return
        for ty, x0, x1, page in self.tab_hits:
            if y == ty and x0 <= x <= x1:
                self.page = page
                return
        if y in self.row_hits:
            self.sel = self.row_hits[y]

    # -- drawing ----------------------------------------------------------

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        devices = self.engine.snapshot()
        if devices:
            self.sel = min(self.sel, len(devices) - 1)
        else:
            self.sel = 0

        self.draw_header(w, len(devices))
        table = DISCOVER if self.page == 0 else SYNC

        detail_h = 7
        top = 2
        bottom = h - 2
        list_h = max(3, bottom - detail_h - top)
        if h < 16:                       # tiny terminal: drop the detail pane
            detail_h = 0
            list_h = max(1, bottom - top)

        # Column 0 is the selection gutter, so every row and the header start
        # at column 1 -- otherwise the marker overwrites the first letter of
        # the selected device's name.
        self.put(top, 0, " " * w, self.attr(C_HEAD, bold=True))
        self.put(top, 1, table.header(w - 1).upper(), self.attr(C_HEAD, bold=True))
        rows = list_h - 1
        if self.sel < self.scroll:
            self.scroll = self.sel
        elif self.sel >= self.scroll + rows:
            self.scroll = self.sel - rows + 1
        self.scroll = max(0, min(self.scroll, max(0, len(devices) - rows)))

        if not devices:
            self.put(top + 2, 2, "no Dante devices seen yet on %s (%s)"
                     % (self.engine.ifname, self.engine.ifaddr), self.attr(C_DIM))
            self.put(top + 3, 2, "listening for announcements; press r to browse now",
                     self.attr(C_DIM))
        self.row_hits = {}
        for i in range(rows):
            idx = self.scroll + i
            if idx >= len(devices):
                break
            self.draw_row(top + 1 + i, w, table, devices[idx], idx == self.sel)
            self.row_hits[top + 1 + i] = idx

        if detail_h and devices:
            y = top + list_h
            self.put(y, 0, "─" * w, self.attr(C_DIM))
            if self.page == 0:
                self.draw_discover_detail(y + 1, w, devices[self.sel], detail_h - 1)
            else:
                self.draw_sync_detail(y + 1, w, devices[self.sel], detail_h - 1)

        if self.show_log:
            self.draw_log(w, h)
        self.draw_footer(h, w)
        self.stdscr.refresh()

    def draw_header(self, w, ndev):
        eng = self.engine
        left = " dantectl  %s %s " % (eng.ifname, eng.ifaddr)
        right = "%d device%s %s" % (ndev, "" if ndev == 1 else "s",
                                    "PASSIVE" if eng.passive else "")
        self.put(0, 0, " " * w, self.attr(C_HEAD))
        self.put(0, 0, left, self.attr(C_HEAD, bold=True))
        x = len(left) + 2
        self.tab_hits = []
        for i, name in enumerate(PAGES):
            label = " %d %s " % (i + 1, name)
            attr = self.attr(C_HEAD, bold=True) if i == self.page else self.attr(C_HEAD)
            if i == self.page:
                attr |= curses.A_REVERSE if self.color else curses.A_BOLD
            elif self.mouse:
                # An unselected tab is underlined to say it is a click target.
                attr |= curses.A_UNDERLINE
            self.put(0, x, label, attr)
            if x < w:
                self.tab_hits.append((0, x, min(w, x + len(label)) - 1, i))
            x += len(label) + 1
        self.put(0, max(0, w - len(right) - 1), right, self.attr(C_HEAD))

    def draw_row(self, y, w, table, dev, selected):
        text, attrs = table.render(dev, w - 1)
        base = self.attr(C_SEL) if selected else 0
        if selected:
            self.put(y, 0, " " * w, base)
        self.put(y, 1, text, base)
        if not selected and self.color:
            for (start, width), attr in zip(table.spans(w - 1), attrs):
                if attr:
                    self.put(y, 1 + start, text[start:start + width], self.attr(attr))
        if selected:
            self.put(y, 0, "▸", base | curses.A_BOLD)

    def draw_discover_detail(self, y, w, dev, lines):
        did = dev.device_id.hex() if dev.device_id else "-"
        rows = [
            [("host ", dev.hostname or "-"), ("id ", did), ("mac ", dev.mac or "-"),
             ("link ", "%s Mbps" % dev.link_speed_mbps if dev.link_speed_mbps else "-")],
            [("model ", dev.model or "-"), ("board ", dev.board_name or "-"),
             ("product ", dev.product_version or "-"), ("fw ", dev.firmware_version or "-"),
             ("hw ", dev.hardware_version or "-"), ("rev ", dev.revision or "-")],
            [("flows ", "%s tx / %s rx" % (fmt_int(dev.max_tx_flows), fmt_int(dev.max_rx_flows))),
             ("max ch/flow ", fmt_int(dev.max_channels_in_flow)),
             ("rate ", "%s Hz" % dev.sample_rate if dev.sample_rate else "-"),
             ("seen ", "%s ago" % fmt_age(dev.age))],
        ]
        svc = []
        for name, short in ((mdns.SVC_ARC, "arc"), (mdns.SVC_CMC, "cmc")):
            if name in dev.services:
                svc.append("%s:%s" % (short, dev.services[name].get("port")))
        txt_bits = [("%s=%s" % (k, v)) for k, v in sorted(dev.txt.items())
                    if k in ("model", "channels", "arcp_vers", "cmcp_vers", "server_vers",
                             "router_info", "router_vers")]
        rows.append([("mdns ", " ".join(svc) or "-"), ("txt ", " ".join(txt_bits) or "-")])
        if dev.arc_error:
            rows.append([("arc ", dev.arc_error)])
        for i, row in enumerate(rows[:lines]):
            x = 1
            for label, value in row:
                self.put(y + i, x, label, self.attr(C_DIM))
                x += len(label)
                self.put(y + i, x, str(value), self.attr(C_DEFAULT))
                x += len(str(value)) + 2
                if x >= w:
                    break

    def draw_sync_detail(self, y, w, dev, lines):
        c = dev.clock
        hb = dev.heartbeat
        leader = self.engine.ptp_leader()
        rows = []
        rows.append([
            ("clock id ", c.get("clock_id") or "-"),
            ("parent ", c.get("parent_id") or "-"),
            # Raw, not interpreted: word0 is 3 on a follower and 2 on the A16R
            # while it led, which looks like a clock-source field, but nothing
            # on the bench confirms that yet.
            ("clock words ", "%04x %04x" % (c["word0"], c["status"]) if c else "-"),
            ("payload ", "%s B" % c["payload_len"] if c.get("payload_len") else "-"),
        ])
        if dev.is_leader:
            rows.append([("offset / path delay ",
                          "n/a -- this device IS the clock (raw 0x8000: %s / %s)"
                          % (fmt_ns(hb.get("offset_ns")), fmt_ns(hb.get("path_delay_ns")))),
                         ("freq ", "%+d ppb" % dev.ppb if dev.ppb is not None else "-")])
        else:
            rows.append([
                ("offset ", fmt_ns(dev.offset_ns)),
                ("path delay ", fmt_ns(dev.path_delay_ns)),
                ("freq ", "%+d ppb" % dev.ppb if dev.ppb is not None else "-"),
                ("clock stats ", "%s ago" % fmt_age(dev.clock_age)),
            ])
        if leader:
            uuid, info = leader
            rows.append([("ptpv1 leader on the wire ", "%s  (%s, subdomain %s, %d sync)"
                          % (uuid, info.get("src_ip", "?"),
                             info.get("subdomain", "?"), info["sync"]))])
        elif not self.engine.ptp_available:
            rows.append([("ptpv1 ", "not sniffed (needs root); grandmaster is what devices report")])
        for i, row in enumerate(rows[:max(0, lines - 2)]):
            x = 1
            for label, value in row:
                self.put(y + i, x, label, self.attr(C_DIM))
                x += len(label)
                self.put(y + i, x, str(value))
                x += len(str(value)) + 2
                if x >= w:
                    break
        # Frequency-offset trend: the same number Dante Controller plots in its
        # clock histogram, straight from the 1 Hz heartbeat.
        hist = [v for _t, v in dev.ppb_history]
        yy = y + min(len(rows), max(0, lines - 2))
        if hist:
            label = "ppb %+d..%+d " % (min(hist), max(hist))
            self.put(yy, 1, label, self.attr(C_DIM))
            spark = sparkline(hist, max(0, w - len(label) - 3))
            self.put(yy, 1 + len(label), spark, self.attr(C_ACCENT))
        else:
            self.put(yy, 1, "no heartbeat yet -- device is not announcing on %s:%d"
                     % (proto.GRP_HEARTBEAT, proto.PORT_HEARTBEAT), self.attr(C_DIM))

    def draw_log(self, w, h):
        entries = list(self.engine.log)[-(h // 2):]
        errs = self.engine.socket_errors
        top = max(2, h - 2 - len(entries) - len(errs) - 2)
        self.put(top, 0, "─ log " + "─" * max(0, w - 6), self.attr(C_DIM))
        y = top + 1
        for msg in errs:
            self.put(y, 1, "! " + msg, self.attr(C_WARN))
            y += 1
        for ts, msg in entries:
            if y >= h - 1:
                break
            self.put(y, 1, "%s  %s" % (time.strftime("%H:%M:%S", time.localtime(ts)), msg),
                     self.attr(C_DIM))
            y += 1

    def draw_footer(self, h, w):
        st = self.engine.stats
        if self.status and time.monotonic() < self.status_until:
            self.put(h - 1, 0, " " * w, self.attr(C_HEAD))
            self.put(h - 1, 1, self.status[:w - 2], self.attr(C_HEAD, bold=True))
            return
        keys = " q quit   tab/1/2 page   ↑↓ select   r refresh   a passive   l log "
        if self.mouse:
            keys += "  click tab/row · shift+drag to select text "
        counters = "mdns %d  info %d  hb %d  arc %d  ptp %d  tx %d" % (
            st["mdns_rx"], st["info_rx"], st["hb_rx"], st["arc_rx"], st["ptp_rx"], st["tx"])
        self.put(h - 1, 0, " " * w, self.attr(C_HEAD))
        self.put(h - 1, 0, keys, self.attr(C_HEAD))
        self.put(h - 1, max(0, w - len(counters) - 1), counters, self.attr(C_HEAD))


def run(stdscr, engine, mouse=True):
    App(stdscr, engine, mouse_enabled=mouse).run()
