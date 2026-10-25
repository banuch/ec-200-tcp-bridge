# -*- coding: UTF-8 -*-
"""Simple EC200U UART2 transmit-and-wait test.

For a DB9 loopback test, disconnect the meter and jumper DB9 pins 2 and 3.
Run this file on the EC200U. It sends TEST_123456789 once and then waits
continuously for received serial data.
"""

import ubinascii
import utime
from machine import Pin, UART


UART_PORT = UART.UART2
UART_BAUD = 9600
TEST_DATA = b"TEST_123456789\r\n"

DTR_ACTIVE_LEVEL = 0
DTR_INACTIVE_LEVEL = 1


def run():
    print("=== EC200U UART2 send-and-wait test ===")
    print("UART settings: 9600 8N1")

    dtr = Pin(Pin.GPIO4, Pin.OUT, Pin.PULL_DISABLE, DTR_ACTIVE_LEVEL)
    dtr.write(DTR_ACTIVE_LEVEL)
    print("DTR GPIO4 ACTIVE LOW, level:", dtr.read())

    port = UART(UART_PORT, UART_BAUD, 8, 0, 1, 0)

    try:
        # Remove old data before starting this test.
        while port.any():
            port.read()
            utime.sleep_ms(10)

        written = port.write(TEST_DATA)
        if written is None:
            written = len(TEST_DATA)
        print("TX %d bytes: %s" % (written, TEST_DATA))
        print("TX HEX:", ubinascii.hexlify(TEST_DATA).upper())
        print("Waiting for RX data; press Ctrl+C to stop...")

        received = bytearray()
        while True:
            available = port.any()
            if available:
                data = port.read(available)
                if data:
                    received.extend(data)
                    print("RX %d bytes: %s" % (len(data), data))
                    print("RX HEX:", ubinascii.hexlify(data).upper())

                    if bytes(received) == TEST_DATA:
                        print("LOOPBACK PASS: transmitted data returned correctly")
                    elif len(received) >= len(TEST_DATA):
                        print("RX complete, but it does not match TEST_DATA")
            else:
                utime.sleep_ms(10)

    except KeyboardInterrupt:
        print("Test stopped")

    finally:
        try:
            port.close()
        except Exception:
            pass
        dtr.write(DTR_INACTIVE_LEVEL)
        print("DTR GPIO4 deasserted HIGH")


if __name__ == "__main__":
    run()
