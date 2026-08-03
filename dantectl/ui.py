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

PAGES = ("Discover", "Sync", "Routing")

# Grid glyphs. A subscription a device calls resolved and running looks
# different from one it merely remembers -- an A16R on the bench holds channels
# pointed at a Yamaha that is switched off, and those must not read as patched.
CELL_ACTIVE = "●"
CELL_UNRESOLVED = "○"
CELL_EMPTY = "·"
CELL_PENDING = "…"           # sent, not yet confirmed by a read-back
PENDING_TIMEOUT = 12.0
COL_PITCH = 2                # one glyph plus one space per transmit channel
LABEL_W = 20                 # width of the receive-channel label column

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
        # Routing page state.
        self.route_tx_ip = None
        self.route_rx_ip = None
        self.cur_row = 0            # cursor in the grid, indices into the lists
        self.cur_col = 0
        self.grid_scroll_row = 0
        self.grid_scroll_col = 0
        self.grid_geom = None       # (x0, y0, rows, cols) of the painted grid
        self.selector_hits = []     # (y, x0, x1, "tx"/"rx")
        self.dropdown = None        # open device chooser, if any
        self.confirm = None         # pending write, waiting for y/n
        self.pending = None         # a patch we sent, not yet seen in a read-back
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

        # A pending write owns the keyboard until it is answered. Only an
        # explicit y goes ahead; every other key cancels.
        if self.confirm:
            action = self.confirm
            self.confirm = None
            if ch in (ord("y"), ord("Y")):
                ok = action["action"]()
                self.flash(action["done"] if ok else
                           "refused: passive mode is on (press a to allow writes)")
            else:
                self.flash("cancelled")
            return True

        if self.dropdown:
            return self.handle_dropdown_key(ch, devices)

        if ch in (ord("q"), 27):
            return False
        elif ch in (ord("\t"), curses.KEY_BTAB):
            self.page = (self.page + 1) % len(PAGES)
        elif ch == ord("1"):
            self.page = 0
        elif ch == ord("2"):
            self.page = 1
        elif ch == ord("3"):
            self.page = 2
        elif self.page == 2 and ch in (ord("s"), ord("d")):
            self.open_dropdown("tx" if ch == ord("s") else "rx", devices)
        elif self.page == 2 and ch in (curses.KEY_LEFT, ord("h")):
            self.cur_col = max(0, self.cur_col - 1)
        elif self.page == 2 and ch in (curses.KEY_RIGHT, ord("l")):
            self.cur_col += 1
        elif self.page == 2 and ch in (curses.KEY_DOWN, ord("j")):
            self.cur_row += 1
        elif self.page == 2 and ch in (curses.KEY_UP, ord("k")):
            self.cur_row = max(0, self.cur_row - 1)
        elif self.page == 2 and ch in (10, 13, curses.KEY_ENTER, ord(" ")):
            self.toggle_subscription(devices)
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
            self.engine.refresh_channels()
            self.flash("refresh sent: mDNS browse + ARC + info to %d device(s)" % len(devices))
        elif ch == ord("a"):
            self.engine.passive = not self.engine.passive
            self.flash("passive (listen-only) mode ON" if self.engine.passive
                       else "active polling ON")
        elif ch == ord("L"):
            # Capital L: lowercase l moves the routing cursor right.
            self.show_log = not self.show_log
        elif ch == curses.KEY_MOUSE:
            self.handle_mouse(devices)
        elif ch == curses.KEY_RESIZE:
            self.stdscr.clear()
        return True

    # -- routing interaction ----------------------------------------------

    def open_dropdown(self, kind, devices):
        current = self.route_tx_ip if kind == "tx" else self.route_rx_ip
        sel = 0
        for i, d in enumerate(devices):
            if d.ip == current:
                sel = i
                break
        box = next((s for s in self.selector_hits if s[3] == kind), None)
        self.dropdown = {
            "kind": kind, "items": list(devices), "sel": sel, "hits": {},
            "y": box[0] if box else 2, "x": box[1] if box else 1,
        }

    def choose_device(self, kind, dev):
        if kind == "tx":
            self.route_tx_ip = dev.ip
        else:
            self.route_rx_ip = dev.ip
        self.cur_row = self.cur_col = 0
        self.grid_scroll_row = self.grid_scroll_col = 0
        self.dropdown = None
        self.engine.set_routing(self.route_tx_ip, self.route_rx_ip)

    def handle_dropdown_key(self, ch, devices):
        dd = self.dropdown
        if ch in (27, ord("q")):
            self.dropdown = None
        elif ch in (curses.KEY_DOWN, ord("j")):
            dd["sel"] = min(dd["sel"] + 1, max(0, len(dd["items"]) - 1))
        elif ch in (curses.KEY_UP, ord("k")):
            dd["sel"] = max(0, dd["sel"] - 1)
        elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
            if dd["items"]:
                self.choose_device(dd["kind"], dd["items"][dd["sel"]])
        elif ch == curses.KEY_MOUSE:
            self.handle_mouse(devices)
        return True

    def toggle_subscription(self, devices):
        """Ask before writing. This is the one thing that changes a device."""
        tx_dev = self._by_ip(devices, self.route_tx_ip)
        rx_dev = self._by_ip(devices, self.route_rx_ip)
        if not tx_dev or not rx_dev:
            return
        tx_list = tx_dev.tx_list or []
        rx_list = rx_dev.rx_list or []
        if not tx_list or not rx_list:
            return
        rc = rx_list[min(self.cur_row, len(rx_list) - 1)]
        tc = tx_list[min(self.cur_col, len(tx_list) - 1)]
        col_hit, _elsewhere = self._subscription_for(rc, tx_dev, tx_list)
        engine = self.engine
        rx_ip, rx_id = rx_dev.ip, rc["id"]

        if col_hit == self.cur_col:
            self.confirm = {
                "text": "UNSUBSCRIBE %s Rx%d '%s' (currently %s@%s)?"
                        % (rx_dev.display_name, rx_id, rc["label"],
                           rc["tx_name"], rc["tx_host"] or "?"),
                "action": lambda: self._write(engine.unsubscribe(rx_ip, rx_id),
                                              rx_ip, rx_id, None),
                "done": "unsubscribe sent to %s Rx%d -- reading back"
                        % (rx_dev.display_name, rx_id),
            }
        else:
            tx_name = tc["name"]
            tx_host = tx_dev.name or tx_dev.hostname or ""
            # Say so when this overwrites an existing patch. A receive channel
            # holds one subscription, so quietly replacing one is a way to take
            # audio off air without meaning to.
            verb = "SUBSCRIBE"
            if rc["tx_name"]:
                verb = "REPLACE %s@%s ON" % (rc["tx_name"], rc["tx_host"] or "?")
            self.confirm = {
                "text": "%s %s Rx%d '%s' <- '%s'@%s?"
                        % (verb, rx_dev.display_name, rx_id, rc["label"], tx_name, tx_host),
                "action": lambda: self._write(
                    engine.subscribe(rx_ip, rx_id, tx_name, tx_host),
                    rx_ip, rx_id, tx_name),
                "done": "subscribe sent: %s Rx%d <- %s@%s -- reading back"
                        % (rx_dev.display_name, rx_id, tx_name, tx_host),
            }

    def _write(self, sent, rx_ip, rx_id, tx_name):
        """Mark the cell in flight so the confirmation is visible immediately."""
        if sent:
            self.pending = {"rx_ip": rx_ip, "rx_id": rx_id, "tx_name": tx_name,
                            "until": time.monotonic() + PENDING_TIMEOUT}
        return sent

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
                self.dropdown = None
                return

        # An open dropdown swallows the click: either it picks an entry or it
        # closes, so a stray click never falls through to the grid underneath.
        if self.dropdown:
            idx = self.dropdown["hits"].get(y)
            if idx is not None and self.dropdown["items"]:
                self.choose_device(self.dropdown["kind"], self.dropdown["items"][idx])
            else:
                for sy, x0, x1, kind in self.selector_hits:
                    if y == sy and x0 <= x <= x1 and kind != self.dropdown["kind"]:
                        self.open_dropdown(kind, devices)
                        return
                self.dropdown = None
            return

        for sy, x0, x1, kind in self.selector_hits:
            if y == sy and x0 <= x <= x1:
                self.open_dropdown(kind, devices)
                return

        if self.page == 2 and self.grid_geom:
            gx0, gy0, rows_vis, cols_vis = self.grid_geom
            if gy0 <= y < gy0 + rows_vis:
                row = self.grid_scroll_row + (y - gy0)
                # The label column selects a row without touching the patch.
                if x < gx0:
                    self.cur_row = row
                    return
                col = self.grid_scroll_col + (x - gx0) // COL_PITCH
                # Clicking the cell the cursor is already on is the commit
                # gesture -- so a first click always just aims, and patching a
                # device is never one stray click away.
                if (row, col) == (self.cur_row, self.cur_col):
                    self.toggle_subscription(devices)
                else:
                    self.cur_row, self.cur_col = row, col
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

        if self.page == 2:
            self.draw_routing(2, h, w, devices)
            if self.dropdown:
                self.draw_dropdown(devices, w, h)
            if self.show_log:
                self.draw_log(w, h)
            self.draw_footer(h, w)
            if self.confirm:
                self.draw_confirm(w, h)
            self.stdscr.refresh()
            return

        self.row_hits = {}
        self.grid_geom = None
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

    # -- routing page -----------------------------------------------------

    @staticmethod
    def _by_ip(devices, ip):
        for d in devices:
            if d.ip == ip:
                return d
        return None

    def _route_defaults(self, devices):
        """Pick a sensible pair the first time the page is opened."""
        if self.route_tx_ip is None:
            for d in devices:
                if d.tx_channels:
                    self.route_tx_ip = d.ip
                    break
        if self.route_rx_ip is None:
            for d in devices:
                if d.rx_channels and d.ip != self.route_tx_ip:
                    self.route_rx_ip = d.ip
                    break
            else:
                for d in devices:
                    if d.rx_channels:
                        self.route_rx_ip = d.ip
                        break

    @staticmethod
    def _subscription_for(rx_chan, tx_dev, tx_list):
        """Which column of this grid, if any, a receive channel is patched to.

        A receive channel can be subscribed to a device that is not the one
        selected as the transmitter. That is not a grid cell -- it belongs in
        the row's annotation instead, or the grid would silently imply the
        channel is free.
        """
        name = rx_chan.get("tx_name")
        host = (rx_chan.get("tx_host") or "").lower()
        if not name:
            return None, False
        mine = {(tx_dev.name or "").lower(), (tx_dev.hostname or "").lower()}
        mine.discard("")
        if host and host not in mine:
            return None, True          # patched, but to some other device
        for col, tx in enumerate(tx_list):
            if tx["name"] == name:
                return col, False
        return None, True              # patched to a channel we cannot see

    def _pending_col(self, rx_dev, rx_chan, tx_list):
        """Column to draw as in-flight, if a patch we sent is not visible yet.

        The device is the authority: as soon as its read-back agrees with what
        we asked for, the marker is dropped and the real state is drawn. It also
        times out, so a device that silently ignores the write does not leave a
        cell claiming forever that something is on its way.
        """
        p = self.pending
        if not p or p["rx_ip"] != rx_dev.ip or p["rx_id"] != rx_chan["id"]:
            return None
        if time.monotonic() > p["until"]:
            self.pending = None
            return None
        if rx_chan.get("tx_name") == p["tx_name"]:      # device agrees; done
            self.pending = None
            return None
        if p["tx_name"] is None:                        # unsubscribe in flight
            return None
        for col, tx in enumerate(tx_list):
            if tx["name"] == p["tx_name"]:
                return col
        return None

    def draw_routing(self, top, h, w, devices):
        self._route_defaults(devices)
        self.engine.set_routing(self.route_tx_ip, self.route_rx_ip)
        tx_dev = self._by_ip(devices, self.route_tx_ip)
        rx_dev = self._by_ip(devices, self.route_rx_ip)

        # -- the two select boxes ----------------------------------------
        self.selector_hits = []
        y = top
        x = 1
        for kind, dev, label in (("tx", tx_dev, "Transmitter"), ("rx", rx_dev, "Receiver")):
            self.put(y, x, label + " ", self.attr(C_DIM))
            x += len(label) + 1
            name = dev.display_name if dev else "(none)"
            box = "[ %-24s ▾ ]" % name[:24]
            attr = self.attr(C_ACCENT, bold=True)
            if self.dropdown and self.dropdown["kind"] == kind:
                attr |= curses.A_REVERSE
            elif self.mouse:
                attr |= curses.A_UNDERLINE
            self.put(y, x, box, attr)
            self.selector_hits.append((y, x, x + len(box) - 1, kind))
            x += len(box) + 4

        tx_list = (tx_dev.tx_list if tx_dev else None) or []
        rx_list = (rx_dev.rx_list if rx_dev else None) or []

        # The legend lives on the row below the boxes, not beside them: the two
        # selectors already reach past column 88 on a 140-column terminal.
        legend = "%s active   %s unresolved   %s free   → patched to another device" % (
            CELL_ACTIVE, CELL_UNRESOLVED, CELL_EMPTY)
        info_y = y + 1
        self.put(info_y, max(0, w - len(legend) - 2), legend, self.attr(C_DIM))

        # -- status / loading --------------------------------------------
        if not tx_dev or not rx_dev:
            self.put(info_y + 1, 2, "select a transmitter and a receiver "
                                    "(click a box, or press s / d)", self.attr(C_DIM))
            self.grid_geom = None
            return
        err = tx_dev.chan_error or rx_dev.chan_error
        if not tx_list or not rx_list:
            msg = "reading channel lists from %s and %s ..." % (
                tx_dev.display_name, rx_dev.display_name)
            if err:
                msg = "channel list unavailable: %s" % err
            elif tx_dev.tx_list is not None and not tx_list:
                msg = "%s advertises no transmit channels" % tx_dev.display_name
            elif rx_dev.rx_list is not None and not rx_list:
                msg = "%s advertises no receive channels" % rx_dev.display_name
            self.put(info_y + 1, 2, msg, self.attr(C_WARN if err else C_DIM))
            self.grid_geom = None
            return

        self.cur_col = max(0, min(self.cur_col, len(tx_list) - 1))
        self.cur_row = max(0, min(self.cur_row, len(rx_list) - 1))

        self.put(info_y, 2, "%d transmit × %d receive" % (len(tx_list), len(rx_list)),
                 self.attr(C_DIM))

        # -- geometry -----------------------------------------------------
        hdr_y = info_y + 2                     # two rows of vertical numbering
        grid_y0 = hdr_y + 2
        grid_x0 = LABEL_W + 1
        bottom = h - 3                          # leave the detail + footer rows
        rows_vis = max(1, bottom - grid_y0)
        cols_vis = max(1, (w - grid_x0 - 1) // COL_PITCH)

        if self.cur_row < self.grid_scroll_row:
            self.grid_scroll_row = self.cur_row
        elif self.cur_row >= self.grid_scroll_row + rows_vis:
            self.grid_scroll_row = self.cur_row - rows_vis + 1
        if self.cur_col < self.grid_scroll_col:
            self.grid_scroll_col = self.cur_col
        elif self.cur_col >= self.grid_scroll_col + cols_vis:
            self.grid_scroll_col = self.cur_col - cols_vis + 1
        self.grid_scroll_row = max(0, min(self.grid_scroll_row, max(0, len(rx_list) - rows_vis)))
        self.grid_scroll_col = max(0, min(self.grid_scroll_col, max(0, len(tx_list) - cols_vis)))
        self.grid_geom = (grid_x0, grid_y0, rows_vis, cols_vis)

        # -- column header: transmit channel numbers, written vertically ---
        self.put(hdr_y, 1, ("%-*s" % (LABEL_W - 1, tx_dev.display_name[:LABEL_W - 1])),
                 self.attr(C_ACCENT))
        self.put(hdr_y + 1, 1, "%-*s" % (LABEL_W - 1, "TX →  /  RX ↓"), self.attr(C_DIM))
        for j in range(cols_vis):
            col = self.grid_scroll_col + j
            if col >= len(tx_list):
                break
            num = tx_list[col]["id"]
            cx = grid_x0 + j * COL_PITCH
            attr = self.attr(C_ACCENT, bold=True) if col == self.cur_col else self.attr(C_DIM)
            self.put(hdr_y, cx, str(num // 10 % 10) if num >= 10 else " ", attr)
            self.put(hdr_y + 1, cx, str(num % 10), attr)

        # -- rows ----------------------------------------------------------
        for i in range(rows_vis):
            row = self.grid_scroll_row + i
            if row >= len(rx_list):
                break
            rc = rx_list[row]
            ry = grid_y0 + i
            col_hit, elsewhere = self._subscription_for(rc, tx_dev, tx_list)
            pend_col = self._pending_col(rx_dev, rc, tx_list)
            mark = "→" if elsewhere else " "
            label = "%2d %s%s" % (rc["id"], mark, rc["label"])
            lattr = self.attr(C_SEL) if row == self.cur_row else 0
            if row == self.cur_row:
                self.put(ry, 0, " " * LABEL_W, lattr)
            self.put(ry, 0, label[:LABEL_W], lattr)
            for j in range(cols_vis):
                col = self.grid_scroll_col + j
                if col >= len(tx_list):
                    break
                cx = grid_x0 + j * COL_PITCH
                if col == pend_col:
                    glyph = CELL_PENDING
                    cattr = self.attr(C_WARN, bold=True)
                elif col == col_hit:
                    glyph = CELL_ACTIVE if rc["active"] else CELL_UNRESOLVED
                    cattr = self.attr(C_GOOD if rc["active"] else C_WARN, bold=True)
                else:
                    glyph = CELL_EMPTY
                    cattr = self.attr(C_DIM)
                if row == self.cur_row and col == self.cur_col:
                    cattr = self.attr(C_SEL) | curses.A_BOLD
                self.put(ry, cx, glyph, cattr)

        # -- what the cursor is on ----------------------------------------
        rc = rx_list[self.cur_row]
        tc = tx_list[self.cur_col]
        col_hit, elsewhere = self._subscription_for(rc, tx_dev, tx_list)
        if rc["tx_name"]:
            state = "active" if rc["active"] else ("unresolved" if rc["unresolved"]
                                                   else "0x%08x" % rc["status"])
            current = "%s@%s (%s)" % (rc["tx_name"], rc["tx_host"] or "?", state)
        else:
            current = "not subscribed"
        detail = "Rx %s '%s'  ←  %s        cursor: Tx '%s' on %s" % (
            rc["id"], rc["label"], current, tc["name"], tx_dev.display_name)
        self.put(h - 2, 1, detail[:w - 2], self.attr(C_DIM))

    def draw_dropdown(self, devices, w, h):
        """Device chooser hanging under whichever select box is open."""
        dd = self.dropdown
        items = dd["items"]
        y0 = dd["y"] + 1
        width = max(28, min(46, w - dd["x"] - 2))
        dd["hits"] = {}
        for i, dev in enumerate(items):
            y = y0 + i
            if y >= h - 2:
                break
            tag = "%s  tx %s / rx %s" % (
                dev.display_name[:24],
                "?" if dev.tx_channels is None else dev.tx_channels,
                "?" if dev.rx_channels is None else dev.rx_channels)
            attr = self.attr(C_SEL) if i == dd["sel"] else self.attr(C_ACCENT)
            self.put(y, dd["x"], " " + tag.ljust(width - 2), attr)
            dd["hits"][y] = i
        if not items:
            self.put(y0, dd["x"], " no devices ".ljust(width), self.attr(C_DIM))

    def draw_confirm(self, w, h):
        c = self.confirm
        self.put(h - 1, 0, " " * w, self.attr(C_WARN) | curses.A_REVERSE)
        text = " %s   [y] yes   [any other key] cancel " % c["text"]
        self.put(h - 1, 0, text[:w], self.attr(C_WARN) | curses.A_REVERSE | curses.A_BOLD)

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
        if self.page == 2:
            keys = (" q quit   tab/1/2/3 page   s/d device   ↑↓←→ cell   "
                    "enter patch   r refresh   L log ")
            if self.mouse:
                keys += "  click box/cell, click again to patch "
        else:
            keys = " q quit   tab/1/2/3 page   ↑↓ select   r refresh   a passive   L log "
            if self.mouse:
                keys += "  click tab/row · shift+drag to select text "
        counters = "mdns %d  info %d  hb %d  arc %d  ptp %d  tx %d" % (
            st["mdns_rx"], st["info_rx"], st["hb_rx"], st["arc_rx"], st["ptp_rx"], st["tx"])
        self.put(h - 1, 0, " " * w, self.attr(C_HEAD))
        self.put(h - 1, 0, keys, self.attr(C_HEAD))
        self.put(h - 1, max(0, w - len(counters) - 1), counters, self.attr(C_HEAD))


def run(stdscr, engine, mouse=True):
    App(stdscr, engine, mouse_enabled=mouse).run()
