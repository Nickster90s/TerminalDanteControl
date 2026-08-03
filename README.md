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

Keys: `1`/`2`/`3`/`Tab` page · `↑↓`/`jk` select · `r` refresh · `a` passive ·
`L` log · `q` quit. Click the header tabs or a device row with the mouse;
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

It **browses, listens and asks**, and it never advertises a device of its own —
so it cannot disturb a live network the way a second controller announcing
itself would.

**One thing writes: the Routing page.** Patching a receive channel sends
`0x3010` to that device, and that changes it. Nothing else in the tool sends a
write, and no write happens without an explicit confirmation keystroke — a
click aims, it never patches. `--passive` blocks writes entirely, as does
pressing `a`. Renames and flow creation are still out of scope.

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
| PTPv1 `224.0.1.129:319/320` | who leads and who follows, from the traffic itself (needs root) |

Run it with `sudo` if you want roles for every device. Both PTP ports matter:
319 (event) carries Sync and Delay_Req, 320 (general) carries Follow_Up and
Delay_Resp. Listening only on 320 — which this did at first — sees the leader's
Follow_Ups and never a single Delay_Req, so no follower can be identified.

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
- `follower ~` / `leader ~` under PTP ROLE — the device never reported a role,
  so it was classified from its PTPv1 traffic. See below.

### Why a device can show no PTP role

PTP ROLE normally comes from the device's clock-stats reply (`0x0020`), where
offset 40 holds the IEEE 1588 port state — 9 FOLLOWER, 6 LEADER. **Some devices
never send that message at all.** A RedNet AM2 is one: it answers `0x13`,
`0x61` and `0xc1` within milliseconds and is silent on `0x21` across every
variation tried — four opcode family bytes (`0x3e`/`0x38`/`0x32`/`0x2a`), both
start codes, trailing byte `0x64` and `0x00`, request sourced from port 8700 and
from 8702, all-zero and real `factory_device_id`, with and without content. Over
45 s of passive listening it announced 45 heartbeats and three rounds of
device/product/network info, and **zero** clock stats. The ARC property tables
(`0x1100`/`0x1102`) do not carry the grandmaster id either, so they are not a
back door to it.

The role is still knowable, because PTP itself says so: Sync, Follow_Up and
Delay_Resp are only ever sent *by* the leader, and Delay_Req only *by* a
follower. On the bench, over 20 s:

```
00:1d:c1:2d:4a:18  Sync x81, Follow_Up x80, Delay_Resp x10   -> A16R, leader
00:1d:c1:a1:72:3c  Delay_Req x5                              -> AM2,  follower
02:00:00:00:00:42  Delay_Req x5                              -> FPGA, follower
```

So with root, the AM2 shows `follower ~` and the grandmaster column fills in
from the wire. Without root that column stays `-`, because guessing would be
worse than admitting we did not look.

Nothing in the UI claims more than the wire supports. Where a field's meaning is
unestablished it is shown raw (`clock words 0003 0003`) rather than decoded into
a confident guess.

## Routing

A patchbay for one pair of devices: pick a transmitter and a receiver in the two
select boxes, and the grid below is **transmit channels across the top, receive
channels down the left**, sized by what those two devices actually advertise.

```
 Transmitter [ N-Series-Switchover      ▾ ]    Receiver [ RedNetA16R               ▾ ]
  48 transmit × 18 receive

 N-Series-Switchover                   1 1 1 1 1 1 1 1 1 1 2 2 2 2 2 2 2 2 2 2 3
 TX →  /  RX ↓       1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
 1  01               ● · · · · · · · · · · · · · · · · · · · · · · · · · · · · · ·
 2  02               · ● · · · · · · · · · · · · · · · · · · · · · · · · · · · · ·
 3 →03               · · · · · · · · · · · · · · · · · · · · · · · · · · · · · · ·
```

| Glyph | Meaning |
|---|---|
| `●` | subscribed and resolved — status `0x01010009` |
| `○` | subscribed but the transmitter cannot be found — status `0x00000001` |
| `·` | free |
| `→` on the row label | that receive channel is patched to a device other than the selected transmitter, so it has no cell in this grid |

Keys: `s` and `d` open the transmitter and receiver choosers, arrows or `hjkl`
move the cell cursor, `Enter` patches. With the mouse: click a box to open its
chooser, click a cell to aim, **click the same cell again to patch**.

**Every patch asks first.** A click only moves the cursor; the write needs a
second click or `Enter`, and then a `y` at the confirmation bar. Anything else
cancels. If the target channel already holds a subscription the prompt says
`REPLACE <existing> ON ...` rather than `SUBSCRIBE`, because a receive channel
holds exactly one subscription and silently overwriting one is a way to take
audio off air without meaning to.

Channel lists are read only for the two selected devices, and re-read every 6 s
so the grid reflects what the devices say rather than what was asked for.

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

**3. The channel-list request needs a six-byte argument block, first word 1.**
The firmware this was written against defaults the start index whenever the
content is short, so it accepts anything — real hardware does not. A RedNet A16R
rejects an empty request, a 2-byte one, both 4-byte forms and an 8-byte form
with code `0x0022`, and rejects a leading `0000` with `0x0023`. Only
`0001 <start> 0000` is answered. Found by sweeping the request shape against the
device; `channels_request()` carries the result.

**4. Some devices drop control requests that arrive in a burst.** Measured, and
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
| `dantectl/ui.py` | the three curses pages, the routing grid, mouse handling |
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

- Channel-level mDNS browsing (`_netaudio-chan` / `_netaudio-bund`) is not
  implemented; only device-level services are browsed. Channel names come from
  ARC instead.
- The routing grid covers one device pair at a time. There is no all-devices
  matrix, and no flow view (`0x2200`/`0x3200`) behind the subscriptions.
- Multicast transmit flows cannot be created or deleted (`0x2201`/`0x2202`).
- The PTPv1 sniffer needs root (ports 319/320). Without it, a device that does
  not answer clock stats shows no PTP role and no grandmaster.
- `word0` of the clock-stats payload (3 on a follower, 2 on the A16R while
  leading) looks like a clock-source field but is displayed raw, not decoded.
- Linux only: interface enumeration uses netlink and `/sys/class/net`.

## Licence

Apache-2.0 — see `LICENSE` and `NOTICE`. Original code; no third-party source is
vendored here.
