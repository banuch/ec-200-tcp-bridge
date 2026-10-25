# EC200U DLMS TCP Bridge

This project connects a DLMS/COSEM electricity meter to a remote Python
server through a Quectel EC200U running QuecPython.

## Project layout

- `device/main.py` - production QuecPython UART-to-TCP bridge.
- `device/network_test.py` - SIM, PDP, DNS and TCP diagnostics.
- `device/dlms_uart_probe.py` - standalone DLMS SNRM/UA UART test.
- `device/serial_*` and `device/uart_port_scan_test.py` - UART/DB9 tests.
- `server/server.py` - asyncio TCP server and automatic reading scheduler.
- `server/dlms_reader.py` - Gurux DLMS session and OBIS reads.
- `tools/` - PC, dummy-meter and early TCP diagnostic utilities.

## Hardware configuration

- Module: Quectel EC200U with QuecPython.
- Meter serial port: `UART.UART2`, 9600 baud, 8N1.
- DB9 RS-232: pin 2 RX, pin 3 TX and pin 5 GND.
- GPIO4/DTR is configured as an output and held HIGH.
- Cellular PDP context must provide IPv4 connectivity.

## Automatic read cycle

1. EC200U connects to the server and sends its bridge identity.
2. The server automatically performs the configured DLMS meter reads.
3. The server completes DLMS release and HDLC disconnect.
4. The server closes TCP gracefully.
5. EC200U waits 15 minutes while feeding its watchdog, then reconnects.
6. Network failures retry after five seconds; failed meter reads retry on the
   server after 60 seconds.

## Server setup

```bash
cd server
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python server.py
```

The default ports are TCP 5000 for field modules and TCP 4059 for the
optional PC relay. Update the VM firewall when these defaults are changed.

## Device deployment

Set `SERVER_HOST`, `SERVER_PORT` and `DEVICE_ID` near the top of
`device/main.py`, then upload it to the EC200U as `/usr/main.py` and reboot.

The server automatically starts reading after a valid bridge hello; the
manual `read <id>` command remains available for diagnostics.

## Useful checks

Run `device/serial_send_wait_test.py` with DB9 pins 2 and 3 jumpered to verify
the UART/RS-232 path. Remove that jumper and run `device/dlms_uart_probe.py`
with the meter connected to verify the SNRM/UA exchange without networking.
