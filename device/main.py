# =========================================
# WINTEK SOLUTIONS
# EC200U-CN IPv6 SERIAL <-> TCP BRIDGE (TCP SERVER MODE)
# =========================================
#
# Same network model AND socket usage as the working network-monitor
# script: the EC200U listens on its own IPv6 address, TCP port 5000, the
# Vultr server connects in, and every exchange is "client sends first,
# module answers" on a plain blocking socket.
#
# DLMS/HDLC is request/response, so the bridge does exactly that:
#   1. blocking recv() of a request from TCP
#   2. write it to the meter UART
#   3. collect the meter's reply until the line goes quiet
#   4. send() the reply back on TCP
# The module never sends unsolicited data, and the socket is never put
# into non-blocking mode or registered with uselect: in testing, all of
# those made the EC200U firmware reset the connection (RST) right after
# it was opened.
#
# Nothing in this file restarts the module: no watchdog, no APN rewrite.

import usocket
import dataCall
import ubinascii
import utime
from machine import Pin, UART

# -----------------------------------------
# Settings
# -----------------------------------------

PORT = 5000
DEVICE_ID = "EC200U_001"

NETWORK_POLL_S = 2      # how often to check for the data call at boot
ACCEPT_TIMEOUT_S = 30   # how often accept() wakes to re-check the address
RECV_TIMEOUT_S = 10     # recv() wakes this often to re-check the address
IDLE_TIMEOUT_S = 120    # drop a client after this long with no request
RETRY_DELAY_S = 5       # pause before rebinding after a server error

# Meter serial port: UART2, 9600 8N1, no flow control
UART_PORT = UART.UART2
UART_BAUD = 9600
UART_DATA_BITS = 8
UART_PARITY = 0         # 0=None, 1=Even, 2=Odd
UART_STOP_BITS = 1
UART_FLOW_CONTROL = 0

# Reply timing from the working dlms_uart_probe.py. Keep the timeout under
# the server's 5 s DLMS WAIT_TIME so a missing reply is retried cleanly.
METER_REPLY_TIMEOUT_MS = 4000   # wait this long for the meter's HDLC frame
FRAME_GAP_MS = 200              # this much silence ends the reply
MAX_CHUNK = 1024
TRACE_HEX = True

# GPIO4 = DTR to the meter. Asserted only while the Vultr server is
# connected (like a modem during a call), released between reads.
DTR_ACTIVE = 1          # GPIO level that asserts DTR; 0 if the line is inverted
DTR_SETTLE_MS = 1000    # meter port wake time after DTR goes active (as the probe)

# Connection LED on GPIO8: ON while a client is connected
LED_ON = 1              # set to 0 if the LED is wired active-low

connection_count = 0


# -----------------------------------------
# GPIO
# -----------------------------------------

try:
    dtr_pin = Pin(Pin.GPIO4, Pin.OUT, Pin.PULL_DISABLE, 1 - DTR_ACTIVE)
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


def set_dtr(active, quiet=False):
    # Drive GPIO4; logs only when the level actually changes
    if dtr_pin is None:
        return
    level = DTR_ACTIVE if active else 1 - DTR_ACTIVE
    try:
        if dtr_pin.read() != level:
            dtr_pin.write(level)
            if not quiet:
                print("DTR GPIO4", "ON" if active else "OFF",
                      "(level %d)" % level)
            elif active:
                print("DTR GPIO4 restored ON")
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
    # Blocking recv with RECV_TIMEOUT_S. Returns bytes, or None on a
    # timeout; raises ClientClosed when the client has gone.
    started = utime.ticks_ms()

    try:
        data = sock.recv(MAX_CHUNK)

    except Exception as e:
        # Same test the working script uses for accept(): an exception
        # after the full timeout is just "nothing arrived"; one that comes
        # straight away is a real socket error.
        elapsed = utime.ticks_diff(utime.ticks_ms(), started)

        if elapsed >= (RECV_TIMEOUT_S - 1) * 1000:
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


def read_meter_reply(uart):
    # Same timing as the working dlms_uart_probe.py: wait up to
    # METER_REPLY_TIMEOUT_MS for data, then collect until FRAME_GAP_MS of
    # silence. A noise byte right after the request (seen as 00/FC/FE in
    # testing) is NOT the reply: bytes before the first 0x7E flag are
    # dropped, and if a burst has no 0x7E at all we keep waiting for the
    # real HDLC frame.
    started = utime.ticks_ms()
    reply = bytearray()
    last_rx = started

    while True:
        now = utime.ticks_ms()
        available = uart.any()

        if available:
            room = MAX_CHUNK - len(reply)
            if room <= 0:
                break

            data = uart.read(min(available, room))

            if data:
                reply.extend(data)
                last_rx = utime.ticks_ms()

        elif reply and utime.ticks_diff(now, last_rx) >= FRAME_GAP_MS:
            if 0x7E in reply:
                break

            # Only noise so far: drop it and keep waiting for the frame
            trace("NOISE (dropped)", bytes(reply))
            reply = bytearray()

        elif utime.ticks_diff(now, started) >= METER_REPLY_TIMEOUT_MS:
            break

        else:
            utime.sleep_ms(5)

    if 0x7E not in reply:
        if reply:
            trace("NOISE (dropped)", bytes(reply))
        return None

    start = reply.index(0x7E)
    if start:
        trace("NOISE (dropped)", bytes(reply[:start]))

    return bytes(reply[start:])


def drain_uart(uart):
    # Throw away stale or noise bytes before each request
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
    # Request/response relay until the client leaves, goes idle,
    # or the IPv6 address changes.
    client.settimeout(RECV_TIMEOUT_S)
    last_request = utime.ticks_ms()

    while True:
        set_dtr(True, quiet=True)   # keep DTR asserted for the whole session

        request = recv_request(client)

        if request is None:
            # recv timed out: check the address and the idle limit
            if get_ipv6() != ipv6:
                raise Exception("IPv6 address changed or PDP lost")

            idle = utime.ticks_diff(utime.ticks_ms(), last_request)

            if idle > IDLE_TIMEOUT_S * 1000:
                print("No request for", IDLE_TIMEOUT_S, "s; closing")
                return

            continue

        last_request = utime.ticks_ms()
        trace("TCP->METER", request)

        discarded = drain_uart(uart)
        if discarded:
            print("Discarded", discarded, "stale UART bytes")

        uart_write_all(uart, request)

        reply = read_meter_reply(uart)

        if reply:
            trace("METER->TCP", reply)
            send_all(client, reply)
        else:
            print("No meter reply within", METER_REPLY_TIMEOUT_MS, "ms")


# -----------------------------------------
# TCP server
# -----------------------------------------

def serve(ipv6, uart):
    # Runs until the socket fails or the IPv6 address changes,
    # then raises so main() can rebind.
    global connection_count

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

        while True:
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
            set_conn_led(True)

            print("CONNECTED:", address, remote_port,
                  "(connection", connection_count, ")")

            try:
                # Assert DTR for this session and let the meter port wake.
                # Requests that arrive meanwhile wait in the TCP buffer.
                set_dtr(True)
                utime.sleep_ms(DTR_SETTLE_MS)

                bridge(client, uart, ipv6)

            except ClientClosed as e:
                print("Session ended:", e)

            except KeyboardInterrupt:
                raise

            except Exception as e:
                print("BRIDGE ERROR:", e)
                if get_ipv6() != ipv6:
                    raise

            finally:
                try:
                    client.close()
                except:
                    pass

                set_dtr(False)
                set_conn_led(False)
                print("CLIENT CLOSED")

    finally:
        set_dtr(False)
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
    set_dtr(False)
    print("DTR GPIO4 OFF until a client connects")

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
