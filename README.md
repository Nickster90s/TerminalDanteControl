# dantectl — a terminal Dante controller

A read-only Dante controller for the terminal: **Discover** (what is on the
network) and **Sync** (what its clock is doing). Python 3 standard library only
— no pip install, no venv, no daemon, no root for the normal path.

```bash
git clone git@github.com:Nickster90s/TerminalDanteControl.git
cd TerminalDanteControl
./dantectl.py                    # TUI, interface auto-picked
./dantectl.py -i ens5            # pin the interface
./dantectl.py --list -t 10       # one-shot text table
./dantectl.py --json -t 10       # same, machine readable
./dantectl.py --interfaces       # what it would choose from
```

`python3 -m dantectl` is equivalent. Requires Python 3.8+ on Linux.

Keys: `1`/`2`/`Tab` page · `↑↓`/`jk` select · `r` refresh · `a` passive ·
`l` log · `q` quit. Click the header tabs or a device row with the mouse;
the wheel moves through the list.

```
 dantectl  ens5 169.254.9.111    1 Discover   2 Sync                    3 devices
 NAME                   IP              MODEL             MFR        TX  RX  SVC   AGE
▸N-Series-Switchover    169.254.9.200   N-Series USB 48   Inferno     48   2  acih  now
 RedNetA16R             169.254.60.249  RedNet A16R MkII  Focusrite   18  18  acih  now
 RF04-RedNetAM2-RFtech  169.254.61.114  RedNet AM2        Focusrite    0   2  acih  now
```

> **Not affiliated with, authorized by, or approved by Audinate.** "Dante" is
> Audinate's trademark. The protocol is undocumented; everything here is derived
> from public reverse-engineering work and from captures taken on my own bench.
> Bench and research use.

---

## What it does, and does not

It **browses, listens and asks**. It never advertises a device of its own, never
writes to a device, and never touches routing — so it cannot disturb a live
network the way a second controller announcing itself would. Subscriptions,
renames and flow creation are out of scope by design.

Interface choice is deliberate: an explicit `-i` always wins, otherwise it
prefers a **link-local 169.254/16** address (what an un-DHCPed Dante network
looks like) and never auto-picks the interface holding the default route — so it
does not start spraying mDNS at the house network.

## Discover

| Source | Gives |
|---|---|
| mDNS `_netaudio-arc` / `_netaudio-cmc` PTR+SRV+TXT+A | the device list, `id=`, `model=`, protocol versions |
| info multicast `224.0.0.231:8702` | model name, product/firmware/hardware version, MAC, primary address, link speed |
| ARC `4440` opcode `0x1003` / `0x1000` | friendly name, factory hostname, board, revision, tx/rx channel counts, flow limits |

The `SVC` column is a four-flag summary: `a` arc, `c` cmc in mDNS, `i` info
replies received, `h` heartbeats received. A device showing `ac--` is
advertising itself but answering nothing — a useful state to be able to see
when you are bringing up a device's control plane.

## Sync

| Source | Gives |
|---|---|
| clock stats reply `0x0020` (asked with `0x21`) | locked / not locked, PTP port state (Leader/Follower), grandmaster and parent clock ids, frequency offset |
| heartbeat `224.0.0.233:8708`, sub-record `0x8001` | frequency offset in ppb — the number Dante Controller plots in its clock histogram |
| heartbeat sub-record `0x8000` | offset from master, mean path delay |
| PTPv1 `224.0.1.129:319/320` | who is actually transmitting Sync (needs root; optional) |

The detail pane draws a sparkline of the frequency offset over the last ~240
heartbeats — the cheapest way to see a servo that is hunting or drifting.

### Reading the Sync column honestly

- `LOCKED` / `UNLOCKED` — from the device's own clock-stats reply.
- `locked ~` / `unlocked ~` — **inferred**, because the device does not answer
  clock stats. The state comes from the heartbeat's offset-from-master against
  a 100 µs threshold. A RedNet AM2 lands here: it answers `0x13`/`0x61`/`0xc1`
  and stays silent on `0x21`, tested across four opcode-family bytes.
- `n/a` under OFFSET — the device is the grandmaster, so "offset from master"
  means nothing. The A16R keeps sending a `0x8000` record while it leads, with
  values around 455 µs that are not an offset from anything; the raw numbers are
  still shown in the detail pane.

Nothing in the UI claims more than the wire supports. Where a field's meaning is
unestablished it is shown raw (`clock words 0003 0003`) rather than decoded into
a confident guess.

---

## Findings from the bench

Observed against a Focusrite RedNet A16R, a RedNet AM2, and my own FPGA Dante
transmitter ([Inferno-FPGA](https://github.com/Nickster90s/Inferno-FPGA)).

**1. The A16R's `0x8001` heartbeat record is 40 bytes, not 4.** The 4-byte form
is a plain `i32` of ppb, and that is what the AM2 and the FPGA send. The A16R
sends 40 bytes whose layout is not established. Reading its first word as ppb
happens to give 0 — correct for a grandmaster, but for the wrong reason. So
`parse_heartbeat()` only reports ppb from the 4-byte form and falls back to
clock stats otherwise.

**2. The second `0x8000` word is not a path delay on every device.** Where one
implementation puts mean path delay, the AM2 puts something that climbs by
~1000 per second (`0x7dd68` → `0x7e0bc` in one heartbeat interval) — a counter.
It is displayed as reported, but do not read a foreign device's value as
nanoseconds without checking that it moves like one.

**3. Some devices drop control requests that arrive in a burst.** Measured, and
the reason this tool paces itself:

| target | ARC alone, 1.6 Hz | ARC + 4 info queries in the same burst |
|---|---|---|
| FPGA transmitter | 15 / 15 answered | **0 / 12 answered** |
| RedNet A16R | 15 / 15 answered | 12 / 12 answered |

The A16R is unaffected under identical load, so it is the small device's receive
path rather than the network: each info query makes it emit several multicast
replies back to back, and a request arriving during that burst is lost. Dante
Controller does exactly this kind of burst on a refresh.

The controller works around it: at most one request group per device per tick,
a 1 s per-device cooldown, and an unanswered request retried after 4 s with
exponential backoff to 60 s rather than waiting a full poll interval.

---

## Layout

| File | Contents |
|---|---|
| `dantectl/net.py` | interface selection, sockets, IGMP joins. Addresses come from a netlink `RTM_GETADDR` dump, not `SIOCGIFADDR` — avahi labels a link-local address `ens5:avahi` and the ioctl answers `EADDRNOTAVAIL` for it, which is exactly the case this tool is for |
| `dantectl/mdns.py` | minimal one-shot mDNS: DNS codec with compression, `_netaudio-*` browse and resolve |
| `dantectl/proto.py` | ARC/CMC 10-byte framing and the 32-byte info framing, with each parser's provenance in comments |
| `dantectl/engine.py` | one background thread: sockets, poll schedule, device registry |
| `dantectl/ui.py` | the two curses pages, mouse handling |
| `dantectl/__main__.py` | argument parsing, `--list` / `--json` modes |

`proto.py` is the piece worth reading first: it documents both framings and the
field offsets that were verified on the wire.

### Ports and groups

| Port / group | Use |
|---|---|
| 5353 `224.0.0.251` | mDNS |
| 4440 | ARC — control and routing |
| 8800 | CMC — device advertisement |
| 8700 | info requests to a device |
| 8702 `224.0.0.231` | device-info multicast |
| 8708 `224.0.0.233` | 1 Hz heartbeat multicast |
| 319/320 `224.0.1.129` | PTPv1 (sniffed only, needs root) |

## Mouse

Click the `1 Discover` / `2 Sync` tabs to switch pages, click a device row to
select it, wheel to scroll. Clickable tabs are underlined.

Click reporting is enabled two ways on purpose: `curses.mousemask()` so ncurses
decodes the events, plus the SGR sequence `\033[?1006h` so the terminal reports
them in the extended encoding. Without SGR the legacy X10 encoding packs the
column into a single byte and nothing past column 223 is reportable — on a wide
terminal that is most of the screen. It is turned back off on exit.

Capturing the mouse means the terminal's own text selection needs **shift+drag**.
`--no-mouse` turns the capture off entirely.

## Known gaps

- Channel-level browsing (`_netaudio-chan` / `_netaudio-bund`) is not
  implemented; only device-level services are browsed.
- Routing/subscription state (ARC `0x3000` receive channels) is not read, so
  there is no patchbay view.
- The PTPv1 sniffer needs root for port 320. Without it the grandmaster shown is
  whatever devices report, which is normally the same answer.
- `word0` of the clock-stats payload (3 on a follower, 2 on the A16R while
  leading) looks like a clock-source field but is displayed raw, not decoded.
- Linux only: interface enumeration uses netlink and `/sys/class/net`.

## Licence

Apache-2.0 — see `LICENSE` and `NOTICE`. Original code; no third-party source is
vendored here.
