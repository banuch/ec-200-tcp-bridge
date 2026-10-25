# -*- coding: UTF-8 -*-
"""Standalone EC200U UART loopback discovery test.

Before running, physically connect the board UART TX pin to its RX pin.
The script tests QuecPython UART2 first and UART1 second. UART0 is
deliberately excluded because it is the module debug/log port.

This test does not start cellular networking and does not modify main.py.
"""

import ubinascii
import utime
from machine import Pin, UART


UART_BAUD = 9600
UART_DATA_BITS = 8
UART_PARITY = 0
UART_STOP_BITS = 1
UART_FLOW_CONTROL = 0

DTR_ACTIVE_LEVEL = 0
DTR_INACTIVE_LEVEL = 1
RX_TIMEOUT_MS = 2000
POLL_INTERVAL_MS = 10


def flush_rx(port):
    """Remove bytes already waiting in the UART receive buffer."""
    while port.any():
        port.read()
        utime.sleep_ms(10)


def write_all(port, data):
    """Write the complete test pattern or raise on a stalled write."""
    total = 0
    while total < len(data):
        written = port.write(data[total:])
        if written is None:
            written = len(data) - total
        if written <= 0:
            raise OSError("UART write returned %s" % written)
        total += written
    return total


def receive_until(port, expected_length, timeout_ms):
    """Collect UART bytes until enough arrive or the timeout expires."""
    received = bytearray()
    started = utime.ticks_ms()

    while len(received) < expected_length:
        available = port.any()
        if available:
            chunk = port.read(available)
            if chunk:
                received.extend(chunk)
        elif utime.ticks_diff(utime.ticks_ms(), started) >= timeout_ms:
            break
        else:
            utime.sleep_ms(POLL_INTERVAL_MS)

    return bytes(received)


def test_port(port_number, port_name, pattern):
    """Open one UART, transmit a pattern and verify its looped-back copy."""
    print("\n--- Testing %s ---" % port_name)
    port = None

    try:
        port = UART(
            port_number,
            UART_BAUD,
            UART_DATA_BITS,
            UART_PARITY,
            UART_STOP_BITS,
            UART_FLOW_CONTROL,
        )
        flush_rx(port)

        print("TX %d bytes: %s" % (
            len(pattern),
            ubinascii.hexlify(pattern).upper(),
        ))
        write_all(port, pattern)

        received = receive_until(port, len(pattern), RX_TIMEOUT_MS)
        print("RX %d bytes: %s" % (
            len(received),
            ubinascii.hexlify(received).upper(),
        ))

        if received == pattern:
            print("RESULT: %s LOOPBACK PASS" % port_name)
            return True

        if not received:
            print("RESULT: %s NO DATA" % port_name)
        else:
            print("RESULT: %s DATA MISMATCH" % port_name)
        return False

    except Exception as error:
        print("RESULT: %s ERROR: %s" % (port_name, error))
        return False

    finally:
        if port is not None:
            try:
                port.close()
            except Exception:
                pass
        utime.sleep_ms(500)


def run():
    print("=== EC200U QuecPython UART port scan ===")
    print("Required wiring: board TX connected directly to board RX")
    print("Settings: 9600 8N1; UART0 excluded (debug port)")

    dtr = Pin(Pin.GPIO4, Pin.OUT, Pin.PULL_DISABLE, DTR_ACTIVE_LEVEL)
    dtr.write(DTR_ACTIVE_LEVEL)
    print("DTR GPIO4 ACTIVE LOW, level:", dtr.read())

    try:
        uart2_ok = test_port(
            UART.UART2,
            "UART2",
            b"EC200U_UART2_LOOPBACK\r\n",
        )
        uart1_ok = test_port(
            UART.UART1,
            "UART1",
            b"EC200U_UART1_LOOPBACK\r\n",
        )

        print("\n=== Scan summary ===")
        print("UART2:", "PASS" if uart2_ok else "FAIL")
        print("UART1:", "PASS" if uart1_ok else "FAIL")

        if uart2_ok:
            print("Use UART.UART2 for the tested board TX/RX pins.")
        elif uart1_ok:
            print("The tested board TX/RX pins appear to use UART.UART1.")
        else:
            print("No loopback found. Check TX-RX jumper, ground, pin labels,")
            print("UART voltage level and whether the correct connector is used.")

    finally:
        dtr.write(DTR_INACTIVE_LEVEL)
        print("DTR GPIO4 deasserted HIGH")


if __name__ == "__main__":
    run()
