# EC200U DLMS TCP Bridge

This project connects a DLMS/COSEM electricity meter to a remote Python
server through a Quectel EC200U running QuecPython.

**Day-to-day operation (deploy, run, Gurux Director via the VM,
troubleshooting): see [docs/RUNBOOK.md](docs/RUNBOOK.md).**

## Project layout

- `device/main.py` - production QuecPython UART-to-TCP bridge.
- `device/network_test.py` - SIM, PDP, DNS and TCP diagnostics.
- `device/dlms_uart_probe.py` - standalone DLMS SNRM/UA UART test.
- `device/serial_*` and `device/uart_port_scan_test.py` - UART/DB9 tests.
- `server/server.py` - asyncio client that connects to the modules and
  schedules automatic reads.
- `server/dlms_reader.py` - Gurux DLMS session and OBIS reads.
- `server/profile_store.py` - SQLite storage for discovered Profile Generic
  metadata, rows and incremental synchronization state.
- `tools/` - PC, dummy-meter and early TCP diagnostic utilities.

## Hardware configuration

- Module: Quectel EC200U with QuecPython.
- Meter serial port: `UART.UART2`, 9600 baud, 8N1.
- DB9 RS-232: pin 2 RX, pin 3 TX and pin 5 GND.
- GPIO4 drives the meter DTR and is held LOW for the whole program
  (`DTR_LEVEL` in `device/main.py`). The meter answers SNRM only with
  GPIO4 LOW.
- Cellular PDP context must provide IPv6 connectivity.

## How the link works

The EC200U is the TCP **server** and the Vultr VM is the TCP **client**:

- `device/main.py` listens on the module's own IPv6 address, TCP port 4059,
  and prints that address at boot (`IPv6: ...`).
- `server/server.py` connects out to every module listed in
  `server/modules.json`.
- Every 300 s the module also sends a signed UDP heartbeat to the VM on
  port 4060 (id, uptime, IPv6, signal, whether the meter has talked
  recently). The server shows modules as live/offline and updates a
  module's IPv6 address from a correctly signed beat. Setup: docs/RUNBOOK.md
  section 3.5.
- A password-protected web dashboard on port 8080 shows the devices
  (online / offline), lets you add or edit them, runs "Read now" and shows
  the meter values and stored profile rows. Setup: docs/RUNBOOK.md
  section 3.6.
- The module is a transparent bridge: every byte from TCP goes to the meter
  UART and every byte from the UART goes back on TCP, with no filtering.
  UART bytes are sent once the line has been quiet for 200 ms, so a meter
  frame normally goes out in one TCP send. The module never restarts itself.
- The EC200U firmware resets a TCP connection on the first request after the
  module has sent 3 replies on it. The server therefore opens a new
  connection every `MAX_REQUESTS_PER_LINK` (3) requests, without telling the
  DLMS session; the meter keeps its association because DTR stays LOW.
  `server/link_test.py` reproduces the limit.

## Automatic read cycle

1. The server connects to the module.
2. The server performs the configured DLMS meter reads.
3. The server completes DLMS release and HDLC disconnect, then closes TCP.
4. 15 minutes later it connects again.
5. If the module cannot be reached the server retries after 60 seconds; a
   failed meter read also retries after 60 seconds.

## Server setup

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python server.py
```

On first start the server creates `server/modules.json`:

```json
{"EC200U_001": {"host": "2401:4900:9864:a48c::2", "port": 4059}}
```

When a module gets a new IPv6 address, update it from the server console
(saved to `modules.json`, no restart needed):

```text
setmodule EC200U_001 <new-ipv6>
modules
poll EC200U_001
```

The server needs no inbound port for modules. Allow inbound TCP 4059 only
when the optional PC relay is used.

## Device deployment

Set `PORT` and `DEVICE_ID` near the top of `device/main.py`, upload it to the
EC200U as `/usr/main.py` and reboot. Note the `IPv6:` address it prints and
enter it on the server with `setmodule`.

Test without a meter from the VM: `nc -6 <module-ipv6> 4059`, type a line,
and the module log shows `TCP->METER` (with no `METER->TCP` after it).

Test the server without hardware: `python tools/pc_test.py 5055 ::1` on the
same PC, then `setmodule TEST ::1 5055` and `poll TEST` on the server console.

## Association View and profile data

The server discovers class-ID 7 Profile Generic objects from the meter's
Association View. Known load, daily, billing and event profiles are collected
incrementally; unknown manufacturer-specific profiles are cached but disabled
until explicitly enabled. Profile rows are stored in
`server/bridge_data.sqlite3` by default. Set `BRIDGE_PROFILE_DB` to use a
different path.

Automatic reads are bounded to 50 rows per request, 200 rows and 1 MiB per
connection, and a 120-second profile budget. Full profile buffers are never
downloaded automatically. The first synchronization reads only a recent
window: 96 load rows, seven daily rows, three billing rows and 100 event rows.

Useful server-console commands:

```text
profiles <id>
profile-status <id>
readprofile <id> <obis> last <count>
readprofile <id> <obis> entry <start> <count>
readprofile <id> <obis> range <from-iso-time> <to-iso-time>
enableprofile <id> <obis>
disableprofile <id> <obis>
```

Time-range reads are accepted only for clock-sorted profiles with a non-zero
capture period, and the requested range must fit within the 200-row limit.

## Useful checks

Run `device/serial_send_wait_test.py` with DB9 pins 2 and 3 jumpered to verify
the UART/RS-232 path. Remove that jumper and run `device/dlms_uart_probe.py`
with the meter connected to verify the SNRM/UA exchange without networking.
