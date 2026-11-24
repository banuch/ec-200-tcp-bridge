# EC200U DLMS Bridge — Runbook

How to deploy, run, use and troubleshoot the meter bridge. Every step here
has been run on the real setup (L&T single-phase meter, EC200U, Airtel
NB-IoT SIM, Vultr VM) as of 2026-11-24.

```
GXDLMSDirector (PC) ──SSH tunnel──► Vultr VM ──IPv6, TCP 5000──► EC200U ──UART2 / RS-232──► Meter
                                    server.py                    device/main.py
```

- The SIM is whitelisted to the Vultr VM only. Nothing else can reach the
  module, so every tool goes **through the VM**.
- The EC200U is the TCP **server** on its own IPv6 address; the VM connects
  out to it.

---

## 1. Hardware checklist

| Item | Setting |
|---|---|
| Meter port | `UART.UART2`, 9600 baud, 8N1, no flow control |
| DB9 (RS-232) | pin 2 RX, pin 3 TX, pin 5 GND — **no loopback jumper** in normal use |
| DTR | GPIO4, **held LOW for the whole program**. The meter answers only with GPIO4 LOW. |
| Connection LED | GPIO8, ON while the VM is connected |
| Meter login | client 32 (Meter Reader), logical server 0, physical server 1, Low Level Security (password in `server/dlms_reader.py`) |

---

## 2. Module (EC200U) deployment

1. Open the module in QPYcom (USB serial port, e.g. `COM32`, 115200).
2. Upload `device/main.py` to the module as `/usr/main.py`.
3. Reboot the module (or run `example.exec('/usr/main.py')` in the REPL).
4. Check the boot log:

   ```
   Meter UART2: 9600 8N1
   DTR GPIO4 held LOW, level: 0
   IPv6: 2401:4900:....::2
   Port: 5000
   BRIDGE READY
   Waiting for Vultr client...
   ```

   - `level: 0` must be 0. If it is 1 the meter will not answer.
   - Note the `IPv6:` address. If it changed, update the VM (section 3.3).

Settings at the top of `device/main.py`: `PORT`, `DEVICE_ID`,
`METER_REPLY_TIMEOUT_MS` (4000), `FRAME_GAP_MS` (200), `IDLE_TIMEOUT_S` (120).

---

## 3. VM server

### 3.1 First-time setup

```bash
git clone https://github.com/banuch/ec-200-tcp-bridge.git ~/ec-200-tcp-bridge
cd ~/ec-200-tcp-bridge/server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### 3.2 Update and start

```bash
cd ~/ec-200-tcp-bridge && git pull origin master
cd server && source .venv/bin/activate
python3 server.py
```

Stop it with `quit` (or Ctrl+C). To keep it running after you log out, start
it inside `tmux` (`tmux new -s bridge`, detach with Ctrl+B then D, reattach
with `tmux attach -t bridge`).

### 3.3 Module address

Modules live in `server/modules.json` (not in git). Change one from the
console — saved immediately, no restart:

```text
setmodule EC200U_001 <new-ipv6>
modules
```

### 3.4 What a good automatic read looks like

At start and then every 15 minutes the server reads each module:

```
Connecting to module EC200U_001 at [...]:5000
Module #1 (EC200U_001) connected ...
DLMS #1 (EC200U_001): reading meter...
DLMS #1: associated (client 32, server 0/1)
Link #1 (EC200U_001) reconnected after 3 requests (EC200U limit)   <- normal, every 3 frames
...
Meter details via module #1
  [Identity] ... [Clock] ... [Instantaneous] ... [Energy] ...
Scheduled read EC200U_001 complete; next in 900s
```

A read is about 48 requests, so expect ~16 `reconnected` lines and
20–40 seconds per read. Results and profile rows are in `bridge_log.txt`
and `bridge_data.sqlite3` next to `server.py`.

---

## 4. Server console commands

| Command | Use |
|---|---|
| `modules` | configured modules and whether a link is open |
| `list` | open links and their ids (`#1`, `#2`, ...) |
| `poll EC200U_001` | run one full read now |
| `connect EC200U_001` | open a link without reading (prints the link id) |
| `read <id>` | full meter read on an open link |
| `send <id> <hex>` | send raw bytes, e.g. `send 1 7EA0070341935A647E` (SNRM) |
| `kick <id>` | close a link |
| `autoread off` / `autoread on` | stop / restart the scheduled reads (use **off** for manual tests and for Director) |
| `pipe EC200U_001` / `pipe off` | send the next PC tool connection to this module / back to automatic |
| `profiles <id>`, `profile-status <id>` | discover / list load, daily, billing and event profiles |
| `readprofile <id> <obis> last <n>` | read the newest *n* rows of a profile |
| `enableprofile` / `disableprofile <id> <obis>` | include / skip a profile in automatic reads |
| `quit` | stop the server |

Tip: type `autoread off` right after starting if you want no read at startup.

---

## 5. Using GXDLMSDirector through the VM

The SIM only accepts the VM, so Director talks to `server.py`'s PC relay
(port 4059) through an SSH tunnel. The relay applies the same
reconnect-every-3-requests rule, so Director works normally.

1. **Tunnel** — on the PC, in PowerShell, and leave the window open:

   ```powershell
   ssh -N -L 4059:localhost:4059 root@<vultr-ip>
   ```

   (If Director connects but gets nothing, use `-L 4059:[::1]:4059`.)

2. **VM console:**

   ```text
   autoread off
   pipe EC200U_001
   ```

3. **Director device settings:**

   | Setting | Value |
   |---|---|
   | Media | Net, TCP |
   | Host / Port | `localhost` / `4059` |
   | Interface | HDLC |
   | Standard | India |
   | Client address | 32 |
   | Logical / physical server | 0 / 1 |
   | Authentication | Low + meter password |
   | Wait time | 10 s or more |

4. Connect and read. The VM log shows `PC ... piped to module #N` and the
   relayed frames.

5. **When finished:** disconnect Director, close the tunnel, then on the VM:

   ```text
   pipe off
   autoread on
   ```

Do not open port 4059 to the internet; the tunnel keeps the meter password
and the meter itself private.

---

## 6. Known EC200U behaviour (do not "fix" these)

| Behaviour | What the code does |
|---|---|
| Firmware resets a TCP connection on the first request after the module has sent **3 replies** on it (any bytes, any timing) | `server.py` `link_write()` reconnects every `MAX_REQUESTS_PER_LINK = 3` requests under the same link id. The meter session survives because DTR stays LOW. |
| Non-blocking sockets, `uselect`, or the module sending first → firmware resets the connection | `device/main.py` uses only blocking recv-then-send |
| `0x7E in bytearray` / `.index()` raise an empty exception | `find_flag()` uses a plain loop |
| Meter is silent with GPIO4 HIGH | GPIO4 is set LOW at boot and never raised |
| Single noise bytes (FF/FE/FC/00 ...) on UART | dropped before the first `0x7E` flag |

---

## 7. Troubleshooting

| Symptom | Check / action |
|---|---|
| `Module EC200U_001 not reachable` | Module booted and shows `BRIDGE READY`? IPv6 changed (compare boot log with `modules`)? Data call up? |
| Module log `No meter reply within 4000 ms` | Boot log shows `level: 0`? DB9 cable and GND (pin 5)? Loopback jumper removed? Run `device/dlms_uart_probe.py` (section 8). |
| `Link #N closed: socket error ConnectionResetError` in the middle of a read | Was `server.py` updated (`git pull`)? `MAX_REQUESTS_PER_LINK` must be 3. Run `server/link_test.py` to re-check the limit. |
| `Module ... busy (PC or read running)` | Director or another read holds the link; wait or `kick <id>`. |
| PC tool refused | `pipe EC200U_001` set? A read running (`autoread off` first)? |
| Read values show `n/a (Access Error ...)` | The object does not exist on this meter or needs a higher login (client 48 / HLS). |
| Clock shows `????-??-??` | The meter's date is not set; setting it needs the utility login (client 48, HLS keys). |

---

## 8. Test tools

| Where | Script | What it proves |
|---|---|---|
| Module | `device/serial_send_wait_test.py` (DB9 pins 2–3 jumpered) | UART2 / RS-232 hardware path |
| Module | `device/dlms_uart_probe.py` (meter connected) | meter answers SNRM with GPIO4 LOW and HIGH — expect reply only on LOW |
| VM | `python3 server/link_test.py` (with `server.py` **stopped**) | TCP link behaviour; expect `count`, `replay`, `get1-twice`, `get2-third` to RESET at step 4 and `get2-second` OK — that is the known 3-reply limit, not a fault |
| PC | `tools/pc_test.py` + `setmodule TEST ::1 5055` | server logic without hardware |

To run a module script: upload it to `/usr/` in QPYcom, then in the REPL
`import example` and `example.exec('/usr/<script>.py')`. Stop `main.py`
first (or rename it) so both do not use UART2.

---

## 9. Where things are

| Path | Contents |
|---|---|
| `device/main.py` | production module bridge (`/usr/main.py` on the EC200U) |
| `server/server.py` | VM reader, scheduler, console, PC relay, reconnect logic |
| `server/dlms_reader.py` | DLMS session, meter login settings, `READ_LIST` of OBIS objects |
| `server/profile_store.py` | SQLite store for profiles |
| `server/modules.json` | module names and IPv6 addresses (VM only, not in git) |
| `server/bridge_log.txt`, `server/bridge_data.sqlite3` | logs and stored data (VM only) |
