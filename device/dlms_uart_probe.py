# -*- coding: UTF-8 -*-
"""Standalone EC200U-to-meter DLMS/HDLC UART probe.

Remove the DB9 pin 2-3 loopback jumper and connect the meter before running.
This test sends one DLMS SNRM frame with GPIO4/DTR LOW, waits for a reply,
then repeats the test with GPIO4/DTR HIGH. It does not use cellular data/TCP.
"""

import ubinascii
import utime
from machine import Pin, UART


UART_PORT = UART.UART2
UART_BAUD = 9600
UART_DATA_BITS = 8
UART_PARITY = 0
UART_STOP_BITS = 1
UART_FLOW_CONTROL = 0

# SNRM used by the reference bridge_server project.
SNRM_FRAME = ubinascii.unhexlify(b"7EA0070341935A647E")

DTR_SETTLE_MS = 1000
REPLY_TIMEOUT_MS = 5000
FRAME_GAP_MS = 200
POLL_INTERVAL_MS = 5
MAX_REPLY_SIZE = 1024


def print_hex(prefix, data):
    print("%s %d bytes: %s" % (
        prefix,
        len(data),
        ubinascii.hexlify(data).upper(),
    ))


def drain_uart(port):
    """Discard bytes left in the UART input buffer from an earlier test."""
    discarded = bytearray()

    while port.any():
        data = port.read(min(port.any(), MAX_REPLY_SIZE))
        if not data:
            break
        discarded.extend(data)
        utime.sleep_ms(10)

    if discarded:
        print_hex("Discarded stale RX", bytes(discarded))


def write_all(port, data):
    """Write the entire frame, including on ports that perform short writes."""
    total = 0

    while total < len(data):
        written = port.write(data[total:])
        if written is None:
            written = len(data) - total
        if written <= 0:
            raise OSError("UART write returned %s" % written)
        total += written

    return total


def wait_for_reply(port):
    """Wait for first RX byte, then collect until a quiet frame gap."""
    reply = bytearray()
    started = utime.ticks_ms()
    last_rx = started

    while True:
        now = utime.ticks_ms()
        available = port.any()

        if available:
            remaining = MAX_REPLY_SIZE - len(reply)
            if remaining <= 0:
                break

            data = port.read(min(available, remaining))
            if data:
                reply.extend(data)
                last_rx = utime.ticks_ms()

        elif reply and utime.ticks_diff(now, last_rx) >= FRAME_GAP_MS:
            break
        elif not reply and utime.ticks_diff(now, started) >= REPLY_TIMEOUT_MS:
            break
        else:
            utime.sleep_ms(POLL_INTERVAL_MS)

    return bytes(reply)


def probe_at_dtr_level(port, dtr, level, label):
    """Set one GPIO4 level, send SNRM and report the raw meter response."""
    print("\n--- DTR GPIO4 %s test ---" % label)
    dtr.write(level)
    print("GPIO4 level:", dtr.read())
    utime.sleep_ms(DTR_SETTLE_MS)

    drain_uart(port)
    print_hex("METER TX SNRM", SNRM_FRAME)
    written = write_all(port, SNRM_FRAME)
    print("UART wrote %d bytes; waiting up to %d seconds..." % (
        written,
        REPLY_TIMEOUT_MS // 1000,
    ))

    reply = wait_for_reply(port)
    if not reply:
        print("RESULT: NO REPLY with DTR GPIO4 %s" % label)
        return False

    print_hex("METER RX", reply)
    if reply[0] == 0x7E and reply[-1] == 0x7E:
        print("RESULT: HDLC-LIKE REPLY with DTR GPIO4 %s" % label)
    else:
        print("RESULT: RX DATA with DTR GPIO4 %s (not a complete HDLC frame)" % label)
    return True


def run():
    print("=== EC200U standalone DLMS UART probe ===")
    print("Remove DB9 loopback jumper; connect meter to DB9")
    print("UART2 settings: 9600 8N1")

    dtr = Pin(Pin.GPIO4, Pin.OUT, Pin.PULL_DISABLE, 1)
    port = UART(
        UART_PORT,
        UART_BAUD,
        UART_DATA_BITS,
        UART_PARITY,
        UART_STOP_BITS,
        UART_FLOW_CONTROL,
    )

    try:
        low_reply = probe_at_dtr_level(port, dtr, 0, "LOW")
        utime.sleep_ms(1000)
        high_reply = probe_at_dtr_level(port, dtr, 1, "HIGH")

        print("\n=== Probe summary ===")
        print("DTR GPIO4 LOW :", "REPLY" if low_reply else "NO REPLY")
        print("DTR GPIO4 HIGH:", "REPLY" if high_reply else "NO REPLY")

        if low_reply and not high_reply:
            print("Use GPIO4 LOW while communicating with this meter.")
        elif high_reply and not low_reply:
            print("Use GPIO4 HIGH while communicating with this meter.")
        elif low_reply and high_reply:
            print("The meter replied at both GPIO4 levels.")
        else:
            print("No meter reply. Check straight/null-modem wiring, meter")
            print("serial settings, power, wake-up method and DLMS addressing.")

    except KeyboardInterrupt:
        print("Probe stopped")

    finally:
        try:
            port.close()
        except Exception:
            pass
        dtr.write(1)
        print("DTR GPIO4 left HIGH")


if __name__ == "__main__":
    run()
