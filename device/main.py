# =========================================
# WINTEK SOLUTIONS
# EC200U-CN IPv6 SERIAL <-> TCP BRIDGE (TCP SERVER MODE)
# =========================================
#
# Same network model AND socket usage as the working network-monitor
# script: the EC200U listens on its own IPv6 address, TCP port 4059, the
# Vultr server connects in, and every exchange is "client sends first,
# module answers" on a plain blocking socket.
#
# The bridge is fully transparent: every byte from TCP is written to the
# meter UART, and every byte from the UART is sent to TCP, with no HDLC
# filtering and no request/response pairing. Unsolicited meter data and
# noise bytes are forwarded too.
#
# The socket stays in blocking mode with a short timeout (RECV_POLL_MS)
# and the UART is checked between recv() calls. It is never put into
# non-blocking mode or registered with uselect: in testing, both made the
# EC200U firmware reset the connection (RST) right after it was opened.
# Note: the module sending data before the server has sent anything was
# also seen to cause a reset, so unsolicited meter bytes may do the same.
#
# Heartbeat: every HEARTBEAT_INTERVAL_S the module sends one small UDP
# packet to the VM (HEARTBEAT_HOST, port HEARTBEAT_PORT) so the server
# knows it is alive and learns its current IPv6 address. It is a separate
# UDP socket, never the bridge connection, and is sent from the main loop
# (no threads). See heartbeat_packet() for the format.
#
# Nothing in this file restarts the module: no watchdog, no APN rewrite.

import usocket
import dataCall
import net
import ubinascii
import uhashlib
import ujson
import utime
from machine import Pin, UART

# -----------------------------------------
# Settings
# -----------------------------------------

PORT = 4059            # DLMS/COSEM registered port
DEVICE_ID = "EC200U_001"

NETWORK_POLL_S = 2      # how often to check for the data call at boot
ACCEPT_TIMEOUT_S = 30   # how often accept() wakes to re-check the address
RECV_POLL_MS = 50       # recv() timeout; the UART is checked between recvs
CHECK_INTERVAL_S = 10   # how often to re-check the address and idle limit
IDLE_TIMEOUT_S = 120    # drop a client after this long with no data either way
RETRY_DELAY_S = 5       # pause before rebinding after a server error

# Meter serial port: UART2, 9600 8N1, no flow control
UART_PORT = UART.UART2
UART_BAUD = 9600
UART_DATA_BITS = 8
UART_PARITY = 0         # 0=None, 1=Even, 2=Odd
UART_STOP_BITS = 1
UART_FLOW_CONTROL = 0

# UART bytes are sent to TCP once the line has been quiet this long, so a
# meter frame normally goes out in one send (the EC200U counts sends
# towards its 3-reply link limit). 200 ms is the gap the probe used.
UART_GAP_MS = 200
MAX_CHUNK = 1024
TRACE_HEX = True

# GPIO4 = DTR to the meter, held LOW (asserted) for the whole program.
# The meter answers SNRM only with GPIO4 LOW (dlms_uart_probe.py).
DTR_LEVEL = 0
DTR_SETTLE_MS = 1000    # meter port wake time after boot (as the probe)

# Connection LED on GPIO8: ON while a client is connected
LED_ON = 1              # set to 0 if the LED is wired active-low

# Heartbeat to the VM (UDP). The key is NOT kept in this file: upload it
# to the module as HEARTBEAT_KEY_FILE (one line, same text as the server's
# heartbeat key). Without it beats are sent unsigned, and the server shows
# them but will not update the module's address from them.
HEARTBEAT_HOST = "2401:c080:2400:1480:5400:06ff:fe3d:7402"   # Vultr VM
HEARTBEAT_PORT = 4060
HEARTBEAT_INTERVAL_S = 300
HEARTBEAT_KEY_FILE = "/usr/heartbeat_key.txt"

connection_count = 0
client_connected = False
last_meter_rx = None        # ticks_ms of the last byte from the meter
boot_ms = utime.ticks_ms()


# -----------------------------------------
# GPIO
# -----------------------------------------

try:
    dtr_pin = Pin(Pin.GPIO4, Pin.OUT, Pin.PULL_DISABLE, DTR_LEVEL)
except Exception as e:
    dtr_pin = None
    print("GPIO4 DTR init failed:", e)

try:
    conn_led = Pin(Pin.GPIO8, Pin.OUT, Pin.PULL_DISABLE, 1 - LED_ON)
except Exception as e:
    conn_led = None
    print("GPIO8 LED init failed:", e)


def set_conn_led(on):
    if conn_led is None:
        return
    try:
        conn_led.write(LED_ON if on else 1 - LED_ON)
    except Exception as e:
        print("GPIO8 LED write failed:", e)


def hold_dtr():
    # Keep GPIO4 at DTR_LEVEL; logs only if it had drifted
    if dtr_pin is None:
        return
    try:
        if dtr_pin.read() != DTR_LEVEL:
            dtr_pin.write(DTR_LEVEL)
            print("DTR GPIO4 restored LOW")
    except Exception as e:
        print("GPIO4 DTR write failed:", e)


def safe_call(fn):
    try:
        return fn()
    except Exception as e:
        print("Diagnostic error:", e)
        return None


# -----------------------------------------
# Network
# -----------------------------------------

def get_ipv6():
    # Returns the active IPv6 address, or None if the data call is down
    info = safe_call(lambda: dataCall.getInfo(1, 1))

    if isinstance(info, tuple) and len(info) >= 3:
        ip = info[2]

        if ip[0] == 1 and ip[2]:
            return ip[2]

    return None


def wait_for_ipv6():
    # At power-on main.py starts before the modem has registered,
    # so keep polling instead of giving up.
    waited = 0

    while True:
        ipv6 = get_ipv6()

        if ipv6:
            return ipv6

        print("Waiting for IPv6 PDP...", waited, "s")
        utime.sleep(NETWORK_POLL_S)
        waited += NETWORK_POLL_S


class ClientClosed(Exception):
    pass


# -----------------------------------------
# Heartbeat (UDP to the VM)
# -----------------------------------------

def load_heartbeat_key():
    try:
        with open(HEARTBEAT_KEY_FILE) as f:
            key = f.read().strip()
        if key:
            print("Heartbeat key loaded")
            return key.encode()
    except Exception:
        pass
    print("No", HEARTBEAT_KEY_FILE, "- heartbeats will be unsigned")
    return None


def hmac_sha256(key, msg):
    # QuecPython has no hmac module; standard HMAC (RFC 2104) on sha256
    if len(key) > 64:
        key = uhashlib.sha256(key).digest()
    key = key + b"\x00" * (64 - len(key))
    inner = uhashlib.sha256(bytes([b ^ 0x36 for b in key]) + msg).digest()
    return uhashlib.sha256(bytes([b ^ 0x5C for b in key]) + inner).digest()


hb_key = load_heartbeat_key()
hb_sock = None
hb_addr = None
hb_seq = 0
hb_last = None              # ticks_ms of the last beat attempt


def heartbeat_packet(ipv6):
    # One JSON line, then "|" and the hex HMAC-SHA256 of the JSON bytes
    # when a key is loaded. Fields:
    #   id         DEVICE_ID
    #   seq        beat number since boot (starts at 1)
    #   up         seconds since boot
    #   ip         current IPv6 address
    #   csq        signal quality 0..31 (99 = unknown)
    #   client     1 if the VM is connected to the bridge now
    #   meter_age  seconds since the meter last sent a byte (-1 = never)
    up = utime.ticks_diff(utime.ticks_ms(), boot_ms) // 1000

    if last_meter_rx is None:
        meter_age = -1
    else:
        meter_age = utime.ticks_diff(utime.ticks_ms(), last_meter_rx) // 1000

    csq = safe_call(net.csqQueryPoll)

    payload = ujson.dumps({
        "id": DEVICE_ID,
        "seq": hb_seq,
        "up": up,
        "ip": ipv6,
        "csq": csq if isinstance(csq, int) else 99,
        "client": 1 if client_connected else 0,
        "meter_age": meter_age,
    }).encode()

    if hb_key:
        return payload + b"|" + ubinascii.hexlify(hmac_sha256(hb_key, payload))
    return payload


def send_heartbeat(ipv6):
    # Never raises: a lost beat must not disturb the bridge
    global hb_sock, hb_addr, hb_seq, hb_last
    hb_last = utime.ticks_ms()
    hb_seq += 1

    try:
        if hb_sock is None:
            hb_addr = usocket.getaddrinfo(HEARTBEAT_HOST, HEARTBEAT_PORT)[0][-1]
            hb_sock = usocket.socket(
                usocket.AF_INET6, usocket.SOCK_DGRAM, usocket.IPPROTO_UDP)

        packet = heartbeat_packet(ipv6)
        hb_sock.sendto(packet, hb_addr)
        print("[%d ms] HEARTBEAT #%d sent (%d bytes)" % (
            utime.ticks_ms(), hb_seq, len(packet)))

    except Exception as e:
        print("Heartbeat failed:", e)
        try:
            hb_sock.close()
        except Exception:
            pass
        hb_sock = None


def maybe_heartbeat(ipv6):
    if hb_last is None or utime.ticks_diff(
            utime.ticks_ms(), hb_last) >= HEARTBEAT_INTERVAL_S * 1000:
        send_heartbeat(ipv6)


# -----------------------------------------
# Data transfer
# -----------------------------------------

def trace(direction, data):
    if TRACE_HEX:
        print("[%d ms] %s %d bytes: %s" % (
            utime.ticks_ms(), direction, len(data),
            ubinascii.hexlify(data).upper()))
    else:
        print("[%d ms] %s %d bytes" % (
            utime.ticks_ms(), direction, len(data)))


def send_all(sock, payload):
    total = 0

    while total < len(payload):
        n = sock.send(payload[total:])

        if n is None or n <= 0:
            raise Exception("Socket send failed")

        total += n

    return total


def recv_request(sock):
    # recv with the short RECV_POLL_MS timeout. Returns bytes, or None on
    # a timeout; raises ClientClosed when the client has gone.
    started = utime.ticks_ms()

    try:
        data = sock.recv(MAX_CHUNK)

    except Exception as e:
        # An exception after (most of) the timeout is just "nothing
        # arrived"; one that comes straight away is a real socket error.
        elapsed = utime.ticks_diff(utime.ticks_ms(), started)

        if elapsed >= RECV_POLL_MS // 2:
            return None

        raise ClientClosed("recv error: %s" % e)

    if not data:
        raise ClientClosed("client closed the connection")

    return data


def uart_write_all(uart, data):
    total = 0

    while total < len(data):
        n = uart.write(data[total:])

        if n is None:
            n = len(data) - total

        if n <= 0:
            raise Exception("UART write failed")

        total += n

    return total


def read_uart_burst(uart):
    # Everything the meter sends, collected until UART_GAP_MS of silence
    # (or MAX_CHUNK bytes) so one frame goes out as one TCP send. Nothing
    # is filtered: noise bytes and unsolicited data are forwarded too.
    burst = bytearray()
    last_rx = utime.ticks_ms()

    while len(burst) < MAX_CHUNK:
        available = uart.any()

        if available:
            data = uart.read(min(available, MAX_CHUNK - len(burst)))

            if data:
                burst.extend(data)
                last_rx = utime.ticks_ms()

        elif utime.ticks_diff(utime.ticks_ms(), last_rx) >= UART_GAP_MS:
            break

        else:
            utime.sleep_ms(2)

    return bytes(burst)


def drain_uart(uart):
    # Throw away bytes that arrived while no client was connected
    discarded = 0

    while uart.any():
        data = uart.read(min(uart.any(), MAX_CHUNK))
        if not data:
            break
        discarded += len(data)

    return discarded


# -----------------------------------------
# Bridge session
# -----------------------------------------

def bridge(client, uart, ipv6):
    # Transparent relay: every TCP byte goes to the meter UART and every
    # UART byte goes to TCP, in both directions, until the client leaves,
    # goes idle, or the IPv6 address changes.
    global last_meter_rx
    client.settimeout(RECV_POLL_MS / 1000)
    last_activity = utime.ticks_ms()
    last_check = last_activity

    # Bytes from before the connection have nowhere to go; sending them
    # straight away would also make the module talk first.
    discarded = drain_uart(uart)
    if discarded:
        print("Discarded", discarded, "UART bytes from before the connection")

    while True:
        hold_dtr()   # GPIO4 stays LOW

        # TCP -> meter
        request = recv_request(client)

        if request:
            last_activity = utime.ticks_ms()
            trace("TCP->METER", request)
            uart_write_all(uart, request)

        # Meter -> TCP
        if uart.any():
            burst = read_uart_burst(uart)

            if burst:
                last_activity = utime.ticks_ms()
                last_meter_rx = last_activity
                trace("METER->TCP", burst)
                send_all(client, burst)

        # Periodic checks: address and idle limit
        now = utime.ticks_ms()

        if utime.ticks_diff(now, last_check) >= CHECK_INTERVAL_S * 1000:
            last_check = now
            maybe_heartbeat(ipv6)

            if get_ipv6() != ipv6:
                raise Exception("IPv6 address changed or PDP lost")

            if utime.ticks_diff(now, last_activity) > IDLE_TIMEOUT_S * 1000:
                print("No data either way for", IDLE_TIMEOUT_S, "s; closing")
                return


# -----------------------------------------
# TCP server
# -----------------------------------------

def serve(ipv6, uart):
    # Runs until the socket fails or the IPv6 address changes,
    # then raises so main() can rebind.
    global connection_count, client_connected

    server = usocket.socket(
        usocket.AF_INET6,
        usocket.SOCK_STREAM,
        usocket.IPPROTO_TCP_SER
    )

    try:
        server.bind((ipv6, PORT))
        server.listen(1)
        server.settimeout(ACCEPT_TIMEOUT_S)

        print("IPv6:", ipv6)
        print("Port:", PORT)
        print("BRIDGE READY")
        send_heartbeat(ipv6)    # at once: the server learns the address

        while True:
            maybe_heartbeat(ipv6)
            print("\nWaiting for Vultr client...")

            started = utime.ticks_ms()

            try:
                client, address, remote_port = server.accept()

            except Exception as e:
                # accept() wakes up periodically so a changed or lost
                # address is noticed even when no client connects.
                if get_ipv6() != ipv6:
                    raise Exception("IPv6 address changed or PDP lost")

                elapsed = utime.ticks_diff(utime.ticks_ms(), started)

                if elapsed < (ACCEPT_TIMEOUT_S - 1) * 1000:
                    # Failed straight away: a real socket error
                    raise e

                continue

            connection_count += 1
            client_connected = True
            set_conn_led(True)

            print("CONNECTED:", address, remote_port,
                  "(connection", connection_count, ")")

            try:
                bridge(client, uart, ipv6)

            except ClientClosed as e:
                print("[%d ms] Session ended: %s" % (utime.ticks_ms(), e))

            except KeyboardInterrupt:
                raise

            except Exception as e:
                print("BRIDGE ERROR:", type(e).__name__, e)
                if get_ipv6() != ipv6:
                    raise

            finally:
                try:
                    client.close()
                except:
                    pass

                client_connected = False
                set_conn_led(False)
                print("CLIENT CLOSED")

    finally:
        set_conn_led(False)
        try:
            server.close()
        except:
            pass


def main():
    print("\n===================================")
    print(" WINTEK IPv6 SERIAL-TCP BRIDGE")
    print(" Device:", DEVICE_ID)
    print("===================================")

    uart = UART(
        UART_PORT,
        UART_BAUD,
        UART_DATA_BITS,
        UART_PARITY,
        UART_STOP_BITS,
        UART_FLOW_CONTROL
    )
    print("Meter UART2: 9600 8N1")
    hold_dtr()
    print("DTR GPIO4 held LOW, level:",
          dtr_pin.read() if dtr_pin else "n/a")
    utime.sleep_ms(DTR_SETTLE_MS)

    while True:
        try:
            ipv6 = wait_for_ipv6()
            serve(ipv6, uart)

        except KeyboardInterrupt:
            print("Stopped")
            return

        except Exception as e:
            print("SERVER ERROR:", e)

        print("Restarting server in", RETRY_DELAY_S, "s")
        utime.sleep(RETRY_DELAY_S)


main()
