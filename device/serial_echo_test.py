# -*- coding: UTF-8 -*-
"""Standalone EC200U UART2 echo test; does not start cellular networking.

Connect a serial test device to the modem board's UART connector:
    test-device TX -> modem RX
    test-device RX <- modem TX
    test-device GND -- modem GND

Send bytes at 9600 8N1. This script prints the bytes and echoes them unchanged.
Stop with Ctrl+C.
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

DTR_ACTIVE_LEVEL = 0
DTR_INACTIVE_LEVEL = 1
READ_SIZE = 256


def write_all(port, data):
    """Write all bytes to UART or raise if no progress is made."""
    written_total = 0

    while written_total < len(data):
        written = port.write(data[written_total:])
        if written is None:
            written = len(data) - written_total
        if written <= 0:
            raise OSError("UART write returned %s" % written)
        written_total += written

    return written_total


def run():
    """Echo every received UART2 byte until interrupted."""
    print("=== EC200U UART2 echo test ===")

    dtr = Pin(Pin.GPIO4, Pin.OUT, Pin.PULL_DISABLE, DTR_ACTIVE_LEVEL)
    dtr.write(DTR_ACTIVE_LEVEL)
    print("DTR GPIO4 ACTIVE LOW, level:", dtr.read())

    port = UART(
        UART_PORT,
        UART_BAUD,
        UART_DATA_BITS,
        UART_PARITY,
        UART_STOP_BITS,
        UART_FLOW_CONTROL,
    )
    print("UART2 ready: 9600 8N1")
    print("Waiting for serial bytes; press Ctrl+C to stop...")

    try:
        while True:
            available = port.any()
            if available:
                data = port.read(min(available, READ_SIZE))
                if data:
                    print("RX %d bytes: %s" % (
                        len(data),
                        ubinascii.hexlify(data).upper(),
                    ))
                    written = write_all(port, data)
                    print("TX echo %d bytes" % written)
            else:
                utime.sleep_ms(5)

    except KeyboardInterrupt:
        print("UART echo test stopped")

    finally:
        try:
            port.close()
        except Exception:
            pass
        dtr.write(DTR_INACTIVE_LEVEL)
        print("DTR GPIO4 deasserted HIGH")


if __name__ == "__main__":
    run()
